from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class IntentType(str, Enum):
    SCREENSHOT = "screenshot"
    ANALYZE_SCREEN = "analyze_screen"
    OPEN_URL = "open_url"
    OPEN_APP = "open_app"
    DAILY_REPORT = "daily_report"
    TIMELINE = "timeline"
    STATS = "stats"
    OPEN_WEB_UI = "open_web_ui"
    END_SESSION = "end_session"
    CLOSE_APP = "close_app"
    VOLUME = "volume"
    CLIPBOARD = "clipboard"
    FIND_FILES = "find_files"
    FILE_OP = "file_op"          # 文件写操作（新建/移动/复制/重命名/删除/归档/撤销）
    LIST_WINDOWS = "list_windows"
    FOCUS_WINDOW = "focus_window"
    LOCK_SCREEN = "lock_screen"
    GUI_TASK = "gui_task"
    CHAT = "chat"
    UNKNOWN = "unknown"


@dataclass
class Intent:
    type: IntentType
    target: str = ""
    raw_command: str = ""
    tool: str = ""                       # 工具层路由名（如 app.close），非空时 executor 走 registry
    params: dict = field(default_factory=dict)


_URL_RE = re.compile(r"https?://[^\s]+", re.I)


def parse_intent(command: str, app_aliases: dict[str, str], url_aliases: dict[str, str]) -> Intent:
    text = command.strip()
    if not text:
        return Intent(IntentType.UNKNOWN, raw_command=text)

    if re.search(r"截图|截屏|截个图|截下图", text):
        return Intent(IntentType.SCREENSHOT, raw_command=text)

    if re.search(r"分析(?:一下)?屏幕|看看屏幕|理解(?:一下)?屏幕|我在干什么|看看我在|屏幕理解", text):
        return Intent(IntentType.ANALYZE_SCREEN, raw_command=text)

    # ---- 工具层意图（CLOSE_APP 需在 END_SESSION 前，避免"退出微信"被截胡）----
    close_m = re.search(r"(?:关闭|关掉|杀掉|退出)\s*([\u4e00-\u9fffA-Za-z][^\s]*)", text)
    if close_m and close_m.group(1) not in ("对话", "会话"):
        return Intent(
            IntentType.CLOSE_APP, target=close_m.group(1), raw_command=text,
            tool="app.close", params={"target": close_m.group(1)},
        )

    if re.search(r"日报|今日总结|每日总结|今天总结|总结一下", text):
        return Intent(IntentType.DAILY_REPORT, raw_command=text)

    if re.search(r"时间线|活动记录", text):
        return Intent(IntentType.TIMELINE, raw_command=text)

    if re.search(r"退出|没事了|再见|结束对话|退下吧|先这样", text):
        return Intent(IntentType.END_SESSION, raw_command=text)

    if re.search(r"统计|运行状态|状态", text):
        return Intent(IntentType.STATS, raw_command=text)

    if re.search(r"打开面板|打开网页面板|打开时间线网页|打开界面", text):
        return Intent(IntentType.OPEN_WEB_UI, raw_command=text)

    if re.search(r"音量(?:调)?(?:大|高)一?点?", text):
        return Intent(IntentType.VOLUME, raw_command=text, tool="volume.up", params={"steps": 3})
    if re.search(r"音量(?:调)?(?:小|低)一?点?", text):
        return Intent(IntentType.VOLUME, raw_command=text, tool="volume.down", params={"steps": 3})
    if re.search(r"(?:静音|取消静音|闭麦)", text):
        return Intent(IntentType.VOLUME, raw_command=text, tool="volume.mute")

    if re.search(r"剪贴板(?:里|中)?(?:有|是)?(?:什么|内容)", text):
        return Intent(IntentType.CLIPBOARD, raw_command=text, tool="clip.get")
    clip_set = re.search(r"(?:复制一下|复制到剪贴板|帮我复制)(?:这?段?)[:：]?(.+)", text)
    if clip_set:
        return Intent(
            IntentType.CLIPBOARD, target=clip_set.group(1).strip(), raw_command=text,
            tool="clip.set", params={"text": clip_set.group(1).strip()},
        )

    find_m = re.search(r"(?:找一下|找|搜索|查找)(?:文件|文档)\s*(.+)", text)
    if find_m:
        return Intent(
            IntentType.FIND_FILES, target=find_m.group(1).strip(), raw_command=text,
            tool="files.find", params={"pattern": find_m.group(1).strip()},
        )

    # ---- 文件写操作（必须排在 open / chat 之前，否则「整理桌面文件」会掉进闲聊）----
    if re.search(r"^(?:撤销|撤回|撤销上一步|撤销刚才|回退|undo)$", text, re.I):
        return Intent(IntentType.FILE_OP, raw_command=text, tool="files.undo")

    if re.search(r"^(?:确认|确定|动手)(?:归档|整理)?$|^(?:执行归档|开始整理|确认归档|执行整理)$", text):
        return Intent(
            IntentType.FILE_OP, raw_command=text,
            tool="files.organize", params={"apply": True},
        )

    if re.search(r"算了|先不动|不用了|不整理了|别整理|取消(?:整理|归档)?", text):
        return Intent(
            IntentType.FILE_OP, raw_command=text,
            tool="files.organize", params={"mode": "cancel"},
        )

    if re.search(r"按(?:修改)?(?:时间|日期|月份)归档|按日期归档", text):
        return Intent(
            IntentType.FILE_OP, raw_command=text,
            tool="files.organize", params={"mode": "date"},
        )

    if re.search(r"按(?:文件)?(?:类型|格式)归档", text):
        return Intent(
            IntentType.FILE_OP, raw_command=text,
            tool="files.organize", params={"mode": "type"},
        )

    organize_m = re.search(
        r"(?:整理|归类|归置|收拾)(?:一下|下)?"
        r"(?:(桌面|下载|文档|图片|视频|音乐)(?:上|里|里面)?|(?:这些)?文件)",
        text,
    )
    if organize_m:
        folder = organize_m.group(1) or ""
        return Intent(
            IntentType.FILE_OP, target=folder, raw_command=text,
            tool="files.organize", params={"path": folder},
        )

    mkdir_m = re.search(
        r"(?:新建|创建|建一个|建个|加个)(?:一个)?(?:文件夹|目录)\s*(?:叫|名字叫|名为|：|:)?\s*(.*)$",
        text,
    )
    if mkdir_m:
        name = mkdir_m.group(1).strip()
        return Intent(
            IntentType.FILE_OP, target=name, raw_command=text,
            tool="files.mkdir", params={"path": name},
        )

    mv_m = re.search(r"把\s*(.+?)\s*(?:移动到|移到|挪到|搬到|移至)\s*(.+)", text)
    if mv_m:
        return Intent(
            IntentType.FILE_OP, target=mv_m.group(1).strip(), raw_command=text,
            tool="files.move",
            params={"src": mv_m.group(1).strip(), "dst": mv_m.group(2).strip()},
        )

    cp_m = re.search(r"把\s*(.+?)\s*(?:复制到|拷贝到|复制至|复制一份到)\s*(.+)", text)
    if cp_m:
        return Intent(
            IntentType.FILE_OP, target=cp_m.group(1).strip(), raw_command=text,
            tool="files.copy",
            params={"src": cp_m.group(1).strip(), "dst": cp_m.group(2).strip()},
        )

    rn_m = re.search(
        r"把\s*(.+?)\s*(?:重命名为|改名为|更名为|重命名|改名)\s*(?:为|成|叫)?\s*(.+)", text
    )
    if rn_m:
        return Intent(
            IntentType.FILE_OP, target=rn_m.group(1).strip(), raw_command=text,
            tool="files.rename",
            params={"src": rn_m.group(1).strip(), "new_name": rn_m.group(2).strip()},
        )

    rm_m = re.search(r"(?:删除|删掉|删了|清掉|丢掉)\s*(.+)", text)
    if rm_m:
        return Intent(
            IntentType.FILE_OP, target=rm_m.group(1).strip(), raw_command=text,
            tool="files.delete", params={"path": rm_m.group(1).strip()},
        )

    if re.search(r"(?:列出|看看|有哪些)(?:打开的)?窗口", text):
        return Intent(IntentType.LIST_WINDOWS, raw_command=text, tool="win.list")
    focus_m = re.search(r"(?:切换|切)(?:到|至)\s*(.+?)(?:的)?(?:窗口|界面)?$", text)
    if focus_m and focus_m.group(1).strip():
        return Intent(
            IntentType.FOCUS_WINDOW, target=focus_m.group(1).strip(), raw_command=text,
            tool="win.focus", params={"title": focus_m.group(1).strip()},
        )
    if re.search(r"(?:锁屏|锁定(?:电脑|屏幕))", text):
        return Intent(IntentType.LOCK_SCREEN, raw_command=text, tool="sys.lock")

    url_match = _URL_RE.search(text)
    if url_match:
        return Intent(IntentType.OPEN_URL, target=url_match.group(0), raw_command=text)

    open_web = re.search(r"打开网页\s*(.+)", text)
    if open_web:
        target = open_web.group(1).strip()
        if not target.startswith("http"):
            target = "https://" + target
        return Intent(IntentType.OPEN_URL, target=target, raw_command=text)

    open_m = re.search(r"打开\s*(.+)", text)
    if open_m:
        target = open_m.group(1).strip()
        # 复合指令：「打开 X 然后/给我/顺便 Y」—— 只取前半段的应用名。
        # 不截断的话整句会被当成应用名去解析（"打开qq给我的小号 发个消息"报错的根因）
        head = re.split(r"然后|接着|并且|顺便|再帮|再给|给我|帮我|，|,", target)[0].strip()
        if head:
            target = head
        for alias, url in url_aliases.items():
            if alias in target:
                return Intent(IntentType.OPEN_URL, target=url, raw_command=text)
        for alias, app in app_aliases.items():
            if alias.lower() in target.lower() or target.lower() in alias.lower():
                return Intent(IntentType.OPEN_APP, target=app, raw_command=text)
        if "." in target and " " not in target:
            return Intent(IntentType.OPEN_URL, target=f"https://{target}", raw_command=text)
        return Intent(IntentType.OPEN_APP, target=target, raw_command=text)

    gui_m = re.search(r"帮我(?:点|点击|输入|按|选)\s*(.+)", text)
    if gui_m:
        return Intent(IntentType.GUI_TASK, target=gui_m.group(1).strip(), raw_command=text)

    help_m = re.search(r"帮(?:我|忙)?(.+)", text)
    if help_m:
        inner = help_m.group(1).strip()
        return parse_intent(inner, app_aliases, url_aliases)

    return Intent(IntentType.CHAT, raw_command=text)
