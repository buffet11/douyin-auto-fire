from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from app.browser import (
    AuthenticationError,
    RiskControlError,
    SearchBoxNotReadyError,
    apply_stealth,
    open_private_messages,
)


DOUYIN_URL = "https://www.douyin.com/"

def _browser_path() -> str | None:
    """本机装了 Chrome / Edge 时直接用它，省掉 150MB 的 Chromium 下载。

    与 `run.py` 共用同一个 `BROWSER_PATH` 环境变量（`.env` 或系统环境变量都认），
    而且用真实的 Chrome 比 Playwright 自带 Chromium 的指纹更自然。
    """
    load_dotenv()
    value = os.getenv("BROWSER_PATH")
    return value.strip() if value and value.strip() else None


async def login() -> None:
    browser_path = _browser_path()
    launch_args: dict = {"headless": False}
    if browser_path:
        launch_args["executable_path"] = browser_path
        print(f"使用本机浏览器: {browser_path}")
    else:
        print("未配置 BROWSER_PATH，使用 Playwright 自带 Chromium")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_args)
        context = await browser.new_context(locale="zh-CN")
        page = await context.new_page()
        await apply_stealth(page)
        await page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        await _open_login(page)
        print("请在浏览器中扫码登录。登录完成并看到抖音首页后，回到终端按 Enter。")
        await asyncio.to_thread(input)
        await page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        await _verify_home_login(page)
        # 首页能看 ≠ 私信页能用：抖音对私信页的登录校验更严，只验首页会导出一份
        # "看着登上了、跑起来却报登录失效"的登录态。这里必须真进一次私信页。
        await _verify_chat_login(page)
        await context.storage_state(path="storage-state.json.tmp")
        await browser.close()
        Path("storage-state.json.tmp").replace("storage-state.json")
        print("登录状态已保存到 storage-state.json")
        print("本地运行时程序会自动优先使用它；也可以整份填进 GitHub Secret DOUYIN_STORAGE_STATE。")


async def _open_login(page) -> None:
    login = page.get_by_text("登录", exact=True)
    if await login.count():
        try:
            await login.first.click(timeout=10_000)
        except Exception:
            pass

    qr_login = page.get_by_text("扫码登录", exact=True)
    if await qr_login.count():
        try:
            await qr_login.first.click(timeout=5_000)
        except Exception:
            pass


async def _verify_home_login(page) -> None:
    login = page.get_by_text("登录", exact=True)
    if await login.count() and await login.first.is_visible():
        raise RuntimeError("未检测到登录成功，请重新运行并完成扫码确认")


async def _verify_chat_login(page) -> None:
    """确认这份登录态真的能进私信页（搜索框可见）。"""
    try:
        await open_private_messages(page)
    except (AuthenticationError, RiskControlError, SearchBoxNotReadyError) as exc:
        raise RuntimeError(f"登录态无法进入抖音私信页，请重新登录后再试: {exc}") from exc
    print("已确认可以进入私信页（好友搜索框可见）。")


if __name__ == "__main__":
    asyncio.run(login())
