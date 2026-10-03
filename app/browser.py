from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.config import ConfigError, parse_auth_json
from app.models import Settings
from app.selectors import (
    DOUYIN_CHAT_URL,
    DOUYIN_HOME_URL,
    LOGIN_MARKERS,
    LOGIN_REQUIRED_MARKERS,
    RISK_MARKERS,
    SEARCH_INPUTS,
)


LOGGER = logging.getLogger("douyin_sender")


class AuthenticationError(RuntimeError):
    pass


class RiskControlError(RuntimeError):
    pass


class SearchBoxNotReadyError(RuntimeError):
    """私信页已打开但搜索框未就绪；说明渲染慢，而非登录失效。"""


# 私信页是 SPA，domcontentloaded 之后搜索框由 JS 异步挂载，冷启动时可能超过
# 单轮等待窗口。这里做有限次数重试，并在需要时 reload，避免把慢渲染误判为认证失效。
SEARCH_BOX_RETRIES = 3
_SEARCH_RETRY_DELAY_MS = 1_500

# 进私信页前先访问首页并稍作停留，让站点种下 ttwid / s_v_web_id 之类的指纹值。
WARM_UP_SETTLE_MS = 4_000
_WARM_UP_NAV_TIMEOUT_MS = 45_000

# 关掉 Chromium 的自动化开关，避免 navigator.webdriver 等特征把无头浏览器
# 直接暴露给抖音风控（无头模式下被判定为脚本是最常见的"登录态失效"来源）。
_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
)

# 无头 UA 会被替换成同版本的常规 Windows Chrome UA。
# 实测证据：CI 上抓到的请求里，User-Agent 和 sec-ch-ua 都写着
# `HeadlessChrome/153.0.8010.12`，而且前端还会把 navigator.userAgent /
# navigator.platform 当成参数**上报进发送请求体**（browser_version /
# browser_platform / user_agent 字段）——服务端一眼就能看出是无头机器人。
# 所以这里三处要一起改，保持一致：HTTP UA、client hints、navigator.platform。
_UA_WINDOWS_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version} Safari/537.36"
)
_UA_VERSION_RE = re.compile(r"HeadlessChrome/(\d+(?:\.\d+)*)")

# 在页面脚本执行前抹掉最明显的自动化指纹。只做无副作用的覆盖，
# 不改写任何业务对象，避免干扰抖音自身的前端逻辑。
_STEALTH_INIT_SCRIPT = """(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
  } catch (e) {}
  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  } catch (e) {}
  try {
    if (navigator.permissions && navigator.permissions.query) {
      const originalQuery = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: 'default', onchange: null })
          : originalQuery(params);
    }
  } catch (e) {}
})();"""

# 只在**我们真的改写了 UA** 时才隐藏 navigator.userAgentData。
# 覆盖 UA 不会同步 userAgentData，留着会自相矛盾；但真实 Chrome 本来就有它，
# 在没改 UA 的场景（用本机 Chrome 有头运行）删掉反而成了破绽。
_HIDE_UA_DATA_SCRIPT = """(() => {
  try {
    if (navigator.userAgentData) {
      Object.defineProperty(navigator, 'userAgentData', { get: () => undefined });
    }
  } catch (e) {}
})();"""


def _client_hint_headers(version: str) -> dict[str, str]:
    """按伪装后的 Chrome 版本拼一套自洽的 client hints。"""
    major = version.split(".")[0] or version
    return {
        "sec-ch-ua": f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


# Collects only safe, whitelisted attributes. It deliberately reads no
# innerText / innerHTML / outerHTML / value, so page content, chat messages
# and friend nicknames can never enter the public diagnostic output.
_DOM_SNAPSHOT_JS = """() => {
  const attrs = el => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    placeholder: el.getAttribute('placeholder'),
    role: el.getAttribute('role'),
    aria_label: el.getAttribute('aria-label'),
  });
  return {
    inputs: Array.from(document.querySelectorAll('input')).map(attrs),
    textareas: Array.from(document.querySelectorAll('textarea')).map(attrs),
    contenteditable_count: document.querySelectorAll('[contenteditable="true"]').length,
    role_textbox_count: document.querySelectorAll('[role="textbox"]').length,
  };
}"""

_SAFE_ELEMENT_KEYS = ("tag", "type", "placeholder", "role", "aria_label")


@dataclass
class BrowserSession:
    page: Page
    context: BrowserContext


@asynccontextmanager
async def open_douyin(settings: Settings) -> AsyncIterator[BrowserSession]:
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    try:
        playwright = await async_playwright().start()
        launch_args: dict[str, Any] = {
            "headless": settings.headless,
            "args": list(_LAUNCH_ARGS),
        }
        if settings.browser_path:
            launch_args["executable_path"] = settings.browser_path
        browser = await playwright.chromium.launch(**launch_args)

        context_args: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 1000},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "extra_http_headers": {"Accept-Language": "zh-CN,zh;q=0.9"},
        }
        if settings.storage_state:
            state = parse_auth_json(settings.storage_state, "DOUYIN_STORAGE_STATE")
            if not isinstance(state, dict):
                raise ConfigError("DOUYIN_STORAGE_STATE 必须是 JSON 对象")
            context_args["storage_state"] = state
        context = await browser.new_context(**context_args)

        cookies: list[dict[str, Any]] = []
        if not settings.storage_state and settings.cookie:
            raw_cookies = parse_auth_json(settings.cookie, "DOUYIN_COOKIE")
            if not isinstance(raw_cookies, list):
                raise ConfigError("DOUYIN_COOKIE 必须是 Cookie 数组")
            cookies = _normalize_cookies(raw_cookies)
            await context.add_cookies(cookies)

        page = await context.new_page()
        await apply_stealth(page)

        # 无头 Chromium 默认 UA 里带 "HeadlessChrome"，是最容易被风控识别的
        # 特征之一。这里不猜版本号：从真实页面读一次 UA，取出其中的 Chrome 版本，
        # 换成同版本的常规 Windows Chrome UA 重建 context —— UA、client hints、
        # navigator.platform 三者保持一致，不留自相矛盾的破绽。
        spoofed = await _spoofed_user_agent(page)
        if spoofed is not None:
            user_agent, version = spoofed
            LOGGER.info("检测到无头 UA，改用同版本常规 Windows Chrome UA 重建上下文")
            await context.close()
            context_args["user_agent"] = user_agent
            context_args.setdefault("extra_http_headers", {}).update(_client_hint_headers(version))
            context = await browser.new_context(**context_args)
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            await apply_stealth(page, hide_ua_data=True)

        await _warm_up(page)

        if settings.trace:
            await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        yield BrowserSession(page=page, context=context)
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()


async def apply_stealth(page: Page, *, hide_ua_data: bool = False) -> None:
    """给页面打上反自动化指纹。

    抽成公开函数是为了让自建流程（如 `scripts/login.py` 的扫码登录）也能复用同一套
    伪装，避免登录脚本用一套、正式运行用另一套指纹。

    ``hide_ua_data`` 只在调用方**确实改写了 UA** 时才传 True：此时
    ``navigator.userAgentData`` 会跟新 UA 矛盾，藏掉比留着自相矛盾好。
    """
    await page.add_init_script(_STEALTH_INIT_SCRIPT)
    if hide_ua_data:
        await page.add_init_script(_HIDE_UA_DATA_SCRIPT)


async def _spoofed_user_agent(page: Page) -> tuple[str, str] | None:
    """无头 UA 就返回 (去掉 Headless 标记的 Windows Chrome UA, 版本号)，否则 None。"""
    try:
        user_agent = await page.evaluate("() => navigator.userAgent")
    except Exception:
        return None
    if not isinstance(user_agent, str):
        return None
    match = _UA_VERSION_RE.search(user_agent)
    if match is None:
        return None
    version = match.group(1)
    return _UA_WINDOWS_TEMPLATE.format(version=version), version


async def _warm_up(page: Page) -> None:
    """进入私信页之前先访问一次首页。

    直接冷启动打 /chat 时，抖音的前端还没在本地种下 ttwid / s_v_web_id
    这类指纹值，容易被服务端按"未登录"处理（表现为 /chat 渲染出登录页）。
    预热失败不致命：只记日志，后续照常访问私信页。
    """
    try:
        await page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=_WARM_UP_NAV_TIMEOUT_MS)
        await page.wait_for_timeout(WARM_UP_SETTLE_MS)
    except Exception:
        LOGGER.warning("预热访问抖音首页失败，直接进入私信页", exc_info=True)


async def verify_login(page: Page, timeout_ms: int = 15_000) -> None:
    if await _any_visible(page, RISK_MARKERS, timeout_ms=2_000):
        raise RiskControlError("抖音要求进行安全验证，任务已停止")
    if await _any_visible(page, LOGIN_REQUIRED_MARKERS, timeout_ms=2_000):
        raise AuthenticationError("抖音登录状态已失效")
    if not await _any_visible(page, LOGIN_MARKERS, timeout_ms=timeout_ms):
        raise AuthenticationError("未检测到抖音私信页面，登录状态可能失效或页面结构已变化")


async def open_private_messages(page: Page, timeout_ms: int = 15_000) -> None:
    await page.goto(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
    # 1. Explicit risk-control page takes priority, independently of login state.
    if await _any_visible(page, RISK_MARKERS, timeout_ms=2_000):
        raise RiskControlError("抖音私信页面要求进行安全验证，任务已停止")
    # 2. An explicit login page is the only signal that lets us attribute to
    #    expired credentials. Marker absence does not imply the credentials are
    #    valid, so search-box detection (steps 3/4) is kept separate.
    if await _any_visible(page, LOGIN_REQUIRED_MARKERS, timeout_ms=2_000):
        await _log_login_diagnostic(page, "进入私信页即发现登录页")
        raise AuthenticationError("进入抖音私信页面后登录状态失效")

    # 3. Detect the friend search box. The chat page is a SPA whose search box is
    #    mounted asynchronously after domcontentloaded; a single detection round
    #    occasionally misses it on a cold runner. Retry a few times, reloading the
    #    page when the first round fails, before concluding anything.
    for attempt in range(1, SEARCH_BOX_RETRIES + 1):
        matched = await _first_visible_selector(page, SEARCH_INPUTS, timeout_ms)
        if matched is not None:
            LOGGER.info("检测到好友搜索框: selector=%s, 第 %d 次尝试", matched, attempt)
            await page.wait_for_timeout(3_000)
            return
        # The search box is missing; a freshly shown login prompt may only have
        # appeared during the wait, so re-check before deciding to retry.
        if await _any_visible(page, RISK_MARKERS, timeout_ms=2_000):
            raise RiskControlError("抖音私信页面要求进行安全验证，任务已停止")
        if await _any_visible(page, LOGIN_REQUIRED_MARKERS, timeout_ms=2_000):
            await _log_login_diagnostic(page, "等待搜索框期间出现登录页")
            raise AuthenticationError("进入抖音私信页面后登录状态失效")
        if attempt < SEARCH_BOX_RETRIES:
            LOGGER.warning("未检测到好友搜索框，第 %d/%d 次尝试，准备重试", attempt, SEARCH_BOX_RETRIES)
            if attempt == 1:
                # Reload once: a fresh load usually mounts the SPA search box.
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    LOGGER.exception("reload 失败，改为重新访问私信页面")
                    await page.goto(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
            else:
                await page.wait_for_timeout(_SEARCH_RETRY_DELAY_MS)

    # 4. Search box is still missing after all attempts: emit a safe structural
    #    diagnostic and choose the exception type based on evidence. Only an
    #    explicit login marker justifies AuthenticationError; a page that is
    #    already on /chat merely failed to render the search box in time.
    diagnostic = await _collect_safe_diagnostic(page, LOGIN_REQUIRED_MARKERS, RISK_MARKERS)
    LOGGER.error("多次重试后仍未检测到好友搜索框，页面安全诊断:\n%s", diagnostic)
    raise SearchBoxNotReadyError(f"私信页面已打开，但搜索框在 {SEARCH_BOX_RETRIES} 次重试后仍未就绪")


async def save_trace(session: BrowserSession, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await session.context.tracing.stop(path=path)


async def _log_login_diagnostic(page: Page, reason: str) -> None:
    """登录页出现时把页面安全诊断写进日志。

    原来只有"搜索框始终没出现"才会打诊断，而"确实被换成登录页"这条路径
    什么都不留 —— 结果 CI 里只能看到一行"登录状态失效"，无法判断是 Cookie
    真过期、还是被风控拦了。这里补上诊断，且任何失败都不得影响主流程判断。
    """
    try:
        diagnostic = await _collect_safe_diagnostic(page, LOGIN_REQUIRED_MARKERS, RISK_MARKERS)
        LOGGER.error("%s，页面安全诊断:\n%s", reason, diagnostic)
    except Exception:
        LOGGER.debug("收集登录失败诊断时出错，已忽略", exc_info=True)


async def _any_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int) -> bool:
    per_selector = max(250, timeout_ms // max(1, len(selectors)))
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=per_selector)
            return True
        except Exception:
            continue
    return False


async def _first_visible_selector(
    page: Page,
    selectors: tuple[str, ...],
    timeout_ms: int,
) -> str | None:
    """Return the first selector whose element becomes visible, or None.

    Unlike ``_any_visible`` this also reports *which* selector matched, so the
    diagnostic can distinguish a slow render from a structural change.
    """
    per_selector = max(250, timeout_ms // max(1, len(selectors)))
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=per_selector)
            return selector
        except Exception:
            continue
    return None


async def _collect_safe_diagnostic(
    page: Page,
    login_markers: tuple[str, ...],
    risk_markers: tuple[str, ...],
) -> str:
    url = _safe_url(page.url)
    try:
        title = (await page.title()).strip()
    except Exception:
        title = ""

    try:
        snapshot = await page.evaluate(_DOM_SNAPSHOT_JS) or {}
    except Exception:
        snapshot = {}

    inputs = [_safe_element(item) for item in snapshot.get("inputs", [])]
    textareas = [_safe_element(item) for item in snapshot.get("textareas", [])]
    login_marker = await _any_visible(page, login_markers, timeout_ms=1_000)
    risk_marker = await _any_visible(page, risk_markers, timeout_ms=1_000)
    private_marker = await _any_visible(page, LOGIN_MARKERS, timeout_ms=1_000)

    parts = [
        f"url={url}",
        f"title={title}",
        f"inputs={json.dumps(inputs, ensure_ascii=False)}",
        f"textareas={json.dumps(textareas, ensure_ascii=False)}",
        f"role_textbox_count={snapshot.get('role_textbox_count', 0)}",
        f"contenteditable_count={snapshot.get('contenteditable_count', 0)}",
        f"login_marker={str(login_marker).lower()}",
        f"risk_marker={str(risk_marker).lower()}",
        f"private_marker={str(private_marker).lower()}",
    ]
    return "\n".join(parts)


def _safe_element(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: raw.get(key) for key in _SAFE_ELEMENT_KEYS}


def _safe_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _normalize_cookies(cookies: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for index, cookie in enumerate(cookies):
        if not isinstance(cookie, dict):
            raise ConfigError(f"DOUYIN_COOKIE[{index}] 必须是对象")

        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        if name == "":
            continue
        if not isinstance(name, str) or not isinstance(value, str):
            raise ConfigError(f"DOUYIN_COOKIE[{index}] 缺少有效的 name 或 value")
        if not isinstance(domain, str) or not domain:
            raise ConfigError(f"DOUYIN_COOKIE[{index}] 缺少有效的 domain")

        expires = cookie.get("expires", cookie.get("expirationDate", -1))
        if cookie.get("session") is True:
            expires = -1
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            expires = -1

        normalized.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cookie.get("path") if isinstance(cookie.get("path"), str) else "/",
                "expires": expires,
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": bool(cookie.get("secure", False)),
                "sameSite": _normalize_same_site(cookie.get("sameSite")),
            }
        )
    if not normalized:
        raise ConfigError("DOUYIN_COOKIE 没有有效 Cookie")
    return normalized


def _normalize_same_site(value: Any) -> str:
    mapping = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
    }
    return mapping.get(str(value).lower(), "Lax")
