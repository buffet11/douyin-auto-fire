DOUYIN_CHAT_URL = "https://www.douyin.com/chat"
# 进私信页之前先落一次首页：站点会在这里写好 ttwid / s_v_web_id 一类
# 指纹 Cookie，直接打 /chat 时这些值可能还没种下，容易被判成"未登录"。
DOUYIN_HOME_URL = "https://www.douyin.com/"

# Ordered alternatives keep page-specific changes isolated from the workflow.
#
# 这组标记的语义是「已登录」的证据，两处会用到：
#   1. open_private_messages 的安全诊断（private_marker）
#   2. verify_login —— 每发一条消息前都会调用
# 第 2 种场景里会话已经打开，搜索框可能已被替换/收起，只靠"私信 + 搜索框"
# 会误判成登录失效并中断整轮任务；所以这里补上"会话列表 / 聊天面板 / 输入框"
# 这类已登录才有的强信号。加在这里只会让"判定为已登录"更宽松，不会削弱风控检测
# （风控/登录页是另外两组标记）。
LOGIN_MARKERS = (
    'text=私信',
    'input[placeholder*="搜索"]',
    '[role="textbox"][placeholder*="搜索"]',
    '[data-e2e="conversation-item"]',
    '[class*="conversationConversationItem"]',
    '[class*="RightPanelHeader"]',
    '[class*="messageMessageList"]',
    '.DraftEditor-root',
)
# 登录页证据。前三条是文案，后三条是登录页特有的输入控件 —— 实测抖音在
# 机房/异地 IP 下会把 /chat 直接换成带「国家/地区 + 手机号 + 验证码」的登录页，
# 控件比文案更稳定，所以一并纳入。
LOGIN_REQUIRED_MARKERS = (
    'text=扫码登录',
    'text=验证码登录',
    'text=登录后',
    'text=请登录',
    'input[placeholder*="手机号"]',
    'input[placeholder*="验证码"]',
)
RISK_MARKERS = (
    'text=安全验证',
    'text=完成验证',
    'text=验证身份',
)
SEARCH_INPUTS = (
    'input[placeholder*="搜索"]',
    'input[placeholder="搜索"]',  # 精确匹配备用 selector，兼容慢渲染时属性值变化
    '[role="textbox"][placeholder*="搜索"]',
    'input[aria-label*="搜索"]',
    '[role="textbox"][aria-label*="搜索"]',
)
CHAT_PANEL_MARKERS = (
    '[class*="RightPanelHeader"]',
    '[class*="chatHeader"]',
    '[class*="ChatHeader"]',
    '[class*="messageContent"]',
    '[class*="chatContent"]',
    '[class*="MessagePanel"]',
)
MESSAGE_INPUTS = (
    '[data-contents="true"]',
    '.DraftEditor-editor [contenteditable="true"]',
    '.DraftEditor-root [contenteditable="true"]',
    '[contenteditable="true"][data-placeholder*="发送消息"]',
    '[contenteditable="true"][aria-label*="消息"]',
    '[contenteditable="true"]',
    'textarea[placeholder*="消息"]',
)
IMAGE_INPUTS = ('input[type="file"][accept*="image"]', 'input[type="file"]')
STICKER_BUTTONS = (
    'svg.messageMsgInputiconAction',
    'button[aria-label*="表情"]',
    '[role="button"][aria-label*="表情"]',
    '[title*="表情"]',
)
STICKER_PANELS = (
    '.componentsemojiemojiPanel',
    '[class*="emojiPanel"]',
    '[role="dialog"]',
    '[class*="sticker"]',
)
