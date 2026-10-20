# -*- coding: utf-8 -*-
"""QQ 驱动：读取用 UI Automation(RawView)，写入用 SendInput 模拟键盘。

控件识别不写死某一版 QQ：先按进程认窗口，再按结构认输入框/消息列表，
中文控件名只当加分项（详见下面「版本适配层」）。已实测的版本是
QQ 9.7.25（老 DirectUI 内核）；QQ NT（9.9+，Chromium）和 TIM 走结构兜底，
换版本后请先跑 `python auto_reply.py doctor`。

已经实测确认的三条硬约束：
1. 会话/消息列表项只在 RawView 里可见（ControlView 是空壳），必须 FindAll(Subtree)。
2. 输入框不支持 ValuePattern.SetValue（返回 E_NOTIMPL），只能模拟键盘。
3. SendInput 只发给前台窗口，所以发送前必须把窗口抢到前台。
"""
import ctypes
import hashlib
import io
import os
import re
import tempfile
import time
from ctypes import wintypes

import comtypes.client
import uiautomation as auto
import win32clipboard
import win32con
import win32gui
import win32process
import win32ui

try:
    from PIL import Image
except Exception:
    Image = None      # 没装 Pillow 就退回"只支持 bmp / 文件拖放"的模式

try:
    from comtypes.gen import UIAutomationClient as UIA
except ImportError:
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 让本进程 DPI 感知：UIA 返回的矩形本来就是物理像素，只有这样 SendInput /
# SetCursorPos 用的坐标才同样是物理像素，不会差一个缩放倍率（这台机器 200%）。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_DELETE = 0x2E
VK_A = 0x41
VK_V = 0x56

# 图片放大到剪贴板用的两种格式
CF_DIB = 8        # 设备无关位图：QQ 输入框 Ctrl+V 能直接吃下
CF_HDROP = 15     # 相当于在资源管理器里复制了文件，GIF 走这条能保住动画

CLSID_CUIAUTOMATION = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
UIA_ControlTypePropertyId = 30003
UIA_NamePropertyId = 30005
UIA_HasKeyboardFocusPropertyId = 30008
LIST_ITEM = 50007
LIST = 50008
PANE = 50033          # 窗口标题栏里那块区域就是个 Pane
TAB_ITEM = 50019      # 合并会话窗口右侧的会话 Tab
EDIT = 50004          # 单行/多行输入框
DOCUMENT = 50030      # 文档型输入区（网页/Chromium 内核的输入框常是它）
DATA_ITEM = 50029     # 部分版本（QQ NT）的列表项是 DataItem 而不是 ListItem
UIA_ValuePatternId = 10002
UIA_TextPatternId = 10014

# QQ 的「合并会话窗口」：一个窗口装多个会话，标题写成 "小明等2个会话"。
# 各版本的标题格式不完全一样，另外两种是用 " - QQ" / "QQ - " 分隔的写法。
MERGED_TITLE_RE = re.compile(r"^(?P<name>.+?)等\d+个会话\s*$")
TITLE_PATTERNS = (
    MERGED_TITLE_RE,
    re.compile(r"^(?P<name>.+?)\s*[-–—]\s*(?:QQ|TIM|腾讯QQ)\s*$"),
    re.compile(r"^(?:QQ|TIM|腾讯QQ)\s*[-–—]\s*(?P<name>.+?)\s*$"),
)
APP_TITLES = ("QQ", "TIM", "腾讯QQ", "qq", "tim")

SW_MINIMIZE = 6
SW_SHOWNOACTIVATE = 4      # 显示窗口但不抢焦点：截图前用它，尽量不打断用户
PW_RENDERFULLCONTENT = 2   # PrintWindow 的标志，抓完整内容而不是分层抓
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    return user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))


def _kb(vk=0, scan=0, flags=0):
    it = INPUT()
    it.type = INPUT_KEYBOARD
    it.u.ki = KEYBDINPUT(vk, scan, flags, 0, None)
    return it


def send_unicode_text(text):
    seq = []
    for ch in text:
        code = ord(ch)
        seq.append(_kb(scan=code, flags=KEYEVENTF_UNICODE))
        seq.append(_kb(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return _send(seq)


def send_vk(vk):
    return _send([_kb(vk=vk), _kb(vk=vk, flags=KEYEVENTF_KEYUP)])


def send_combo(mod_vk, key_vk):
    return _send([_kb(vk=mod_vk), _kb(vk=key_vk),
                  _kb(vk=key_vk, flags=KEYEVENTF_KEYUP), _kb(vk=mod_vk, flags=KEYEVENTF_KEYUP)])


_UIA = {"obj": None}


def uia():
    """整个进程复用一个 CUIAutomation 对象（创建一次要几十毫秒，主循环调得很密）。"""
    obj = _UIA.get("obj")
    if obj is None:
        obj = comtypes.client.CreateObject(CLSID_CUIAUTOMATION,
                                          interface=UIA.IUIAutomation)
        _UIA["obj"] = obj
    return obj


# ————————————————————————————————————————————————
# 版本适配层：认「QQ 的进程」和「元素的结构」，不认写死的窗口类名/控件名
# ————————————————————————————————————————————————
# 实测（2026-10，本机 QQ 9.7.25.29411，窗口类 TXGuiFoundation）：
#   输入框 = Edit，Name="输入"；消息列表 = List，Name="消息"；
#   会话项 = ListItem，Name=会话名，LegacyIAccessible.Value=最后一条消息预览；
#   主窗口里**没有** List 控件，会话项直接挂在窗口下面。
# 这些都不是公开接口，换一版 QQ 就会变：
#   QQ NT（9.9 起，Chromium 内核）窗口类是 Chrome_WidgetWin_1，控件树是网页 DOM，
#     默认还不建无障碍树，UIA 只能看到空壳；英文环境下名字是 Input/Messages。
#   TIM 同样是 TXGuiFoundation，但控件层次和 QQ 不完全一样。
# 所以这里先用进程名认窗口，再按结构（控件类型、位置、能否输入）认元素，
# 中文控件名只作为加分项；找不到时还会「戳」一下无障碍树再试一次。
QQ_PROCESS_EXES = ("qq.exe", "qqnt.exe", "qqapp.exe", "tim.exe", "timwp.exe",
                   "txplatform.exe")
LEGACY_WINDOW_CLASSES = ("txguifoundation", "txgflayermask")

_INPUT_NAME_HINTS = ("输入", "发送消息", "输入框", "说点什么", "写点什么",
                     "enter", "input", "message", "type")
_MSG_LIST_NAME_HINTS = ("消息", "聊天记录", "消息记录", "聊天内容",
                        "message", "messages", "history")
_CONV_LIST_NAME_HINTS = ("会话", "联系人", "好友", "最近", "conversation",
                         "contact")

_PROC_CACHE = {}

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def process_path(pid):
    """进程可执行文件的完整路径（拿不到返回空串）。按 pid 缓存。"""
    if pid is None:
        return ""
    try:
        pid = int(pid)
    except Exception:
        return ""
    if pid in _PROC_CACHE:
        return _PROC_CACHE[pid]
    path, h = "", None
    try:
        h = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(32768)
            n = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                path = buf.value or ""
    except Exception:
        path = ""
    finally:
        try:
            if h:
                kernel32.CloseHandle(h)
        except Exception:
            pass
    _PROC_CACHE[pid] = path
    return path


def process_name(pid):
    return os.path.basename(process_path(pid)).lower()


def is_qq_process(pid):
    """这个 pid 是不是 QQ / TIM 的进程（不限版本）。"""
    return process_name(pid) in QQ_PROCESS_EXES


def window_pid(hwnd):
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return None


_VER_CACHE = {}


def process_version(pid):
    """可执行文件版本号，形如 '9.7.25.29411'；拿不到返回空串。按 pid 缓存。"""
    if pid is not None:
        try:
            pid = int(pid)
        except Exception:
            pid = None
    if pid is not None and pid in _VER_CACHE:
        return _VER_CACHE[pid]
    path = process_path(pid)
    ver = ""
    if path:
        try:
            import win32api
            info = win32api.GetFileVersionInfo(path, "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            ver = "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
        except Exception:
            ver = ""
    if pid is not None:
        _VER_CACHE[pid] = ver
    return ver


def qq_flavor(pid=None, hwnd=None):
    """粗分 QQ 的形态：QQNT / QQ / TIM / ?。只用于日志、自检和排错。

    版本号的判断依据：QQ NT 从 9.9 开始，9.x 及以前是老的 DirectUI 客户端。
    """
    if pid is None and hwnd:
        pid = window_pid(hwnd)
    name = process_name(pid)
    if "tim" in name:
        return "TIM"
    if name == "qqnt.exe":
        return "QQNT"
    ver = process_version(pid)
    try:
        parts = [int(x) for x in ver.split(".")[:2]]
    except Exception:
        parts = []
    if len(parts) >= 2 and (parts[0] > 9 or (parts[0] == 9 and parts[1] >= 9)):
        return "QQNT"
    return "QQ" if name.startswith("qq") else "?"


def _window_class(hwnd):
    try:
        return win32gui.GetClassName(hwnd) or ""
    except Exception:
        return ""


def _is_qq_window(hwnd, cls=None):
    """是不是 QQ/TIM 的顶层窗口：类名对得上，或窗口属于 QQ 的进程。"""
    if cls is None:
        cls = _window_class(hwnd)
    low = (cls or "").lower()
    if low in LEGACY_WINDOW_CLASSES:
        return True
    return is_qq_process(window_pid(hwnd))


def top_windows():
    """返回 QQ/TIM 的顶层窗口 [(title, hwnd)]。

    用 Win32 EnumWindows 而不是 UIA 遍历根元素：UIA 根元素只包含**可见**窗口，
    而 QQ 主窗口经常处于隐藏状态（关闭到托盘 / 被隐藏），UIA 会看不到它，
    这曾经导致我误判"主窗口不存在"并去强行唤醒，进而让窗口反复弹出。

    以前这里写死类名 TXGuiFoundation，只能认老版 QQ；现在改成「类名或进程」，
    QQ NT（Chrome_WidgetWin_1）和 TIM 也能一起枚举出来。
    """
    result = []

    def cb(hwnd, _):
        try:
            if _is_qq_window(hwnd):
                result.append((win32gui.GetWindowText(hwnd) or "", hwnd))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return result


def window_title(hwnd):
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return ""


def _pid_match(hwnd, pid):
    if pid is None:
        return True
    try:
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        return False
    return wpid == pid


# —— UIA 小工具 ——

def _elem_from(hwnd):
    return uia().ElementFromHandle(hwnd)


def _find_all_ctl(elem, control_type):
    cond = uia().CreatePropertyCondition(UIA_ControlTypePropertyId, control_type)
    return elem.FindAll(UIA.TreeScope_Subtree, cond)


def _elements(collection):
    try:
        return [collection.GetElement(i) for i in range(collection.Length)]
    except Exception:
        return []


def _name_of(elem):
    try:
        return (elem.CurrentName or "").strip()
    except Exception:
        return ""


def _rect_of(elem):
    try:
        r = elem.CurrentBoundingRectangle
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return (0, 0, 0, 0)


def _window_rect(hwnd):
    try:
        return win32gui.GetWindowRect(hwnd)
    except Exception:
        return (0, 0, 0, 0)


def elem_value(elem):
    """Value 模式的值；控件不支持 Value 模式返回 None（注意：空串是有效值）。"""
    try:
        pat = elem.GetCurrentPattern(UIA_ValuePatternId)
        if pat:
            return pat.QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue
    except Exception:
        pass
    return None


def elem_text(elem):
    """Text 模式里的全文；网页类输入框（Document）通常只有这个能用。"""
    try:
        pat = elem.GetCurrentPattern(UIA_TextPatternId)
        if pat:
            rng = pat.QueryInterface(UIA.IUIAutomationTextPattern).GetDocumentRange()
            return rng.GetText(-1) or ""
    except Exception:
        pass
    return None


def poke_accessibility(hwnd):
    """给窗口戳一个 WM_GETOBJECT，让 Chromium/Electron 系（QQ NT）建立无障碍树。

    Chromium 默认不建树，UIA 只能看到空壳（没有输入框、没有消息列表）。
    它收到无障碍客户端查询后会临时把树建起来。带 300ms 超时，窗口卡死也不会拖住我们。
    """
    try:
        res = ctypes.c_size_t(0)
        user32.SendMessageTimeoutW(wintypes.HWND(hwnd), 0x003D, 0,
                                   ctypes.c_void_p(-4), 0x0002, 300,
                                   ctypes.byref(res))
    except Exception:
        pass


def _discover_input(hwnd):
    """在窗口里找一个「能打字」的输入控件，返回元素或 None。

    判据（按权重）：
      名字像输入框 +120；位于窗口下半部分 +60；有键盘焦点能力 +15；
      够宽 +10。没有任何一条像样的证据就不认 —— 把搜索框当输入框的后果是
      往搜索框里发消息，对方永远收不到，而"发送成功"的判断还会被骗过。
    """
    wl, wt, wr, wb = _window_rect(hwnd)
    height = max(1, wb - wt)
    root = _elem_from(hwnd)
    best, best_score = None, 0
    for ct in (EDIT, DOCUMENT):
        for e in _elements(_find_all_ctl(root, ct)):
            try:
                if e.CurrentIsReadOnly:
                    continue
            except Exception:
                pass
            name = _name_of(e).lower()
            r = _rect_of(e)
            score = 0
            if any(h in name for h in _INPUT_NAME_HINTS):
                score += 120
            try:
                if e.CurrentIsEnabled:
                    score += 5
                if e.CurrentIsKeyboardFocusable:
                    score += 15
            except Exception:
                pass
            if r[2] - r[0] >= 150:
                score += 10
            if r[1] > wt + height * 0.45:
                score += 60
            if score > best_score:
                best, best_score = e, score
    return best if best_score >= 60 else None


def _discover_message_list(hwnd):
    """在聊天窗口里找消息列表 List。返回元素或 None。

    主窗口里没有 List（会话项直接挂在窗口下），所以这里只在聊天窗口里用。
    判据：名字像消息列表 +120、条目多 +（最多 40）、位于窗口右侧 +20；
    名字像会话列表的减分，避免把合并窗口左侧的会话条当成消息区。
    """
    wl, _wt, wr, _wb = _window_rect(hwnd)
    width = max(1, wr - wl)
    root = _elem_from(hwnd)
    best, best_score = None, 0
    for e in _elements(_find_all_ctl(root, LIST)):
        name = _name_of(e).lower()
        r = _rect_of(e)
        named = sum(1 for it in _elements(_find_all_ctl(e, LIST_ITEM)) if _name_of(it))
        score = 0
        if any(h in name for h in _MSG_LIST_NAME_HINTS):
            score += 120
        if any(h in name for h in _CONV_LIST_NAME_HINTS):
            score -= 60
        score += min(named * 2, 40)
        if (r[0] - wl) > width * 0.2:
            score += 20
        if r[2] > r[0] and r[3] > r[1]:
            score += 5
        if score > best_score:
            best, best_score = e, score
    return best if best_score >= 30 else None


_ROLE_CACHE = {}
_ROLE_TTL = 30.0        # 认准的元素缓存这么久
_ROLE_NEG_TTL = 8.0     # "这个窗口里没有输入框"这种否定结论缓存得短一些


def _roles(hwnd, refresh=False):
    """这个窗口里的关键控件：{"input": 输入框, "msglist": 消息列表}。

    UIA 遍历一次要几十到几百毫秒，而主循环每秒要读好几次，所以按 hwnd 缓存。
    真读失败时调用方会带 refresh=True 再来一次（窗口重建后旧元素会失效）。
    """
    now = time.time()
    cls = _window_class(hwnd)
    ent = _ROLE_CACHE.get(hwnd)
    if ent and not refresh:
        # 窗口句柄会被系统回收再利用：类名/进程变了说明是另一个窗口，缓存作废
        if ent.get("cls") == cls and ent.get("pid") == window_pid(hwnd):
            has_any = ent["input"] is not None or ent["msglist"] is not None
            if now - ent["at"] < (_ROLE_TTL if has_any else _ROLE_NEG_TTL):
                return ent
    ent = {"at": now, "cls": cls, "pid": window_pid(hwnd),
           "input": None, "msglist": None}
    try:
        ent["input"] = _discover_input(hwnd)
        ent["msglist"] = _discover_message_list(hwnd)
        if ent["input"] is None and ent["msglist"] is None:
            poke_accessibility(hwnd)          # QQ NT：先把无障碍树戳起来
            time.sleep(0.2)
            ent["input"] = _discover_input(hwnd)
            ent["msglist"] = _discover_message_list(hwnd)
    except Exception:
        pass
    _ROLE_CACHE[hwnd] = ent
    return ent


def prune_role_cache(now=None):
    """清掉早就没再出现过的窗口缓存（QQ 关掉聊天窗口后 hwnd 就作废了）。"""
    now = now if now is not None else time.time()
    for h in [h for h, e in _ROLE_CACHE.items() if now - e.get("at", 0.0) > 900]:
        _ROLE_CACHE.pop(h, None)


def input_elem(hwnd):
    return _roles(hwnd).get("input")


def message_list_elem(hwnd):
    return _roles(hwnd).get("msglist")


def has_input_box(hwnd):
    """窗口里有没有「输入」框 —— 有就是聊天窗口（最小化的也算）。"""
    try:
        return input_elem(hwnd) is not None
    except Exception:
        return False


def _titlebar_name(hwnd, band=170):
    """读窗口标题栏区域里那个 Pane 的名字 —— QQ 把**当前会话名**放在这里。

    这是判断合并窗口"现在显示的是谁"最可靠的依据：TabItem 的 IsSelected
    在 QQ 里读出来恒为 False，靠不住。

    要排除标题栏里的搜索框／按钮文字（它们也带名字），否则会把
    "搜索：联系人、群聊、企业" 当成会话名，发送前的校验就永远对不上。
    """
    try:
        u = uia()
        root = u.ElementFromHandle(hwnd)
        top = win32gui.GetWindowRect(hwnd)[1]      # 最小化时是 -32000，相对关系仍然成立
        cond = u.CreatePropertyCondition(UIA_ControlTypePropertyId, PANE)
        els = root.FindAll(UIA.TreeScope_Subtree, cond)
        for i in range(min(els.Length, 250)):
            e = els.GetElement(i)
            nm = (e.CurrentName or "").strip()
            if not nm or len(nm) > 60:
                continue
            low = nm.lower()
            if low in APP_TITLES or "搜索" in nm or "search" in low:
                continue
            try:
                r = e.CurrentBoundingRectangle
            except Exception:
                continue
            if r.bottom <= r.top:
                continue
            dy = r.top - top
            if -30 <= dy <= band:                  # 只认标题栏附近那一块
                return nm
    except Exception:
        pass
    return None


def active_conversation(hwnd):
    """这个聊天窗口当前正在显示哪个会话。"""
    nm = _titlebar_name(hwnd)
    if nm:
        return nm
    title = (window_title(hwnd) or "").strip()
    for pat in TITLE_PATTERNS:
        m = pat.match(title)
        if m:
            cand = (m.group("name") or "").strip()
            if cand:
                return cand
    # 标题就是 "QQ"/"TIM" 说明这不是聊天窗口，别把程序名当会话名
    if title and title not in APP_TITLES:
        return title
    # 再退一步：合并窗口里的第一个 Tab（切 Tab 时标题栏可能读不到）
    tabs = [t for t in tab_names(hwnd) if t]
    if len(tabs) == 1:
        return tabs[0]
    return ""


def tab_names(hwnd):
    """合并窗口里已打开的会话 Tab 名字列表（普通窗口返回空）。"""
    try:
        u = uia()
        root = u.ElementFromHandle(hwnd)
        cond = u.CreatePropertyCondition(UIA_ControlTypePropertyId, TAB_ITEM)
        items = root.FindAll(UIA.TreeScope_Subtree, cond)
        return [(items.GetElement(i).CurrentName or "") for i in range(items.Length)]
    except Exception:
        return []


def _chat_candidates(contact, pid=None):
    """可能承载 contact 的窗口 hwnd（先按标题粗筛，避免对 20 多个 QQ 子窗口逐个做 UIA）。"""
    hits, maybe = [], []
    for _name, hwnd in top_windows():
        if not _pid_match(hwnd, pid):
            continue
        title = window_title(hwnd)
        if not title:
            continue
        if title == contact or (contact and contact in title):
            hits.append(hwnd)
        elif MERGED_TITLE_RE.search(title):
            maybe.append(hwnd)                     # 合并窗口：名字可能在 Tab 里，标题上看不出来
    seen, out = set(), []
    for h in hits + maybe:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def locate_chat(contact, pid=None):
    """定位 contact 的聊天窗口。返回 (hwnd, state)：

      "active" —— 窗口当前正显示这个会话，可以直接发（含最小化的，发送前会被恢复）
      "tab"    —— 窗口里有这个会话的 Tab，但当前显示的是别人，需要先切换
      None     —— 没有

    以前只按「窗口标题 == 联系人名」精确匹配，QQ 开了合并会话窗口之后标题变成
    "小明等2个会话"，于是这类窗口永远找不到 —— 这就是「打不开聊天窗口」的主因。
    """
    fallback = (None, None)
    for hwnd in _chat_candidates(contact, pid):
        if not has_input_box(hwnd):
            continue
        if active_conversation(hwnd) == contact:
            return hwnd, "active"
        if fallback[0] is None and contact in tab_names(hwnd):
            fallback = (hwnd, "tab")
    return fallback


def activate_tab(hwnd, contact, settle=0.8):
    """把合并窗口切到 contact 这个会话。切完用标题栏名字验证，没切成功就返回 False。

    两个坑：
    - 窗口最小化时 UIA 的动作常常是空操作，所以先还原再切；
    - QQ 还原合并窗口时经常直接把它拆成独立的单会话窗口，拆完 tab 就没了
      （那种情况由 ensure_chat_window 在还原后重新 locate 命中，不必切）。
    """
    try:
        if win32gui.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.7)
    except Exception:
        pass
    try:
        u = uia()
        root = u.ElementFromHandle(hwnd)
        cond = u.CreateAndCondition(
            u.CreatePropertyCondition(UIA_ControlTypePropertyId, TAB_ITEM),
            u.CreatePropertyCondition(UIA_NamePropertyId, contact),
        )
        items = root.FindAll(UIA.TreeScope_Subtree, cond)
        for i in range(items.Length):
            e = items.GetElement(i)
            for how in ("select", "legacy", "invoke", "click"):
                try:
                    if how == "select":
                        pat = e.GetCurrentPattern(UIA.UIA_SelectionItemPatternId)
                        if pat:
                            pat.QueryInterface(UIA.IUIAutomationSelectionItemPattern).Select()
                    elif how == "legacy":
                        _do_default_action(e)
                    elif how == "invoke":
                        _invoke_item(e)
                    else:
                        r = e.CurrentBoundingRectangle
                        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                        if r.right > r.left and point_owner_root(cx, cy) == hwnd:
                            click(cx, cy)
                except Exception:
                    pass
                time.sleep(settle)
                if active_conversation(hwnd) == contact:
                    return True
    except Exception:
        pass
    return False


def find_chat_window(title=None, pid=None):
    """找一个带'输入'框的聊天窗口（旧接口，行为已扩展到合并窗口）。

    title 给定时按窗口标题（联系人名）匹配；pid 给定时限定为该 QQ 实例的窗口，
    多开时必须靠它区分，否则可能把消息发到另一个账号的窗口里。
    """
    if title is None:
        for name, hwnd in top_windows():
            if not _pid_match(hwnd, pid):
                continue
            if has_input_box(hwnd):
                return name, hwnd
        return None, None
    hwnd, state = locate_chat(title, pid)
    if hwnd and state == "active":
        return window_title(hwnd), hwnd
    return None, None


def force_foreground(hwnd, blockers=()):
    """把窗口抢到前台。返回 (是否成功, 原前台窗口)。

    三级递进，因为 SendInput 只发给前台窗口，抢不到前台=这条消息发不出去：
    1. AttachThreadInput 绕过前台锁定（老办法，多数情况够用）
    2. 临时置顶（HWND_TOPMOST）再抢，随后立刻降回 HWND_NOTOPMOST ——
       这样窗口会压在其它非置顶窗口之上（包括挡住它的 QQ 主界面），
       又不会永久变成"总在最前"烦人
    3. 还不行才把挡路的 QQ 窗口（blockers）最小化，这是最后的保底
    """
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True, fg

    try:                                   # 最小化的窗口要先还原，否则抢了也没用
        if win32gui.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.3)
    except Exception:
        pass

    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_t = user32.GetWindowThreadProcessId(hwnd, None)
    tid_self = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(tid_self, tid_fg, True)
    user32.AttachThreadInput(tid_self, tid_t, True)
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    user32.AttachThreadInput(tid_self, tid_t, False)
    user32.AttachThreadInput(tid_self, tid_fg, False)
    time.sleep(0.35)
    if user32.GetForegroundWindow() == hwnd:
        return True, fg

    # 2. 临时置顶
    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        time.sleep(0.15)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE)
    except Exception:
        pass
    if user32.GetForegroundWindow() == hwnd:
        return True, fg

    # 3. 保底：把挡路的 QQ 窗口最小化
    moved = False
    for h in blockers or ():
        if not h or h == hwnd:
            continue
        try:
            if win32gui.IsIconic(h) or not win32gui.IsWindowVisible(h):
                continue
            user32.ShowWindow(h, SW_MINIMIZE)
            moved = True
        except Exception:
            pass
    if moved:
        time.sleep(0.4)
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.35)
    return user32.GetForegroundWindow() == hwnd, fg


def point_owner_root(x, y):
    """屏幕坐标处最上层窗口所属的顶层窗口 hwnd（用来判断点击会不会打到别的窗口上）。"""
    try:
        h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        return user32.GetAncestor(h, 2) if h else 0     # GA_ROOT
    except Exception:
        return 0


def uia_elem(hwnd):
    return uia().ElementFromHandle(hwnd)


def list_items_named(elem, list_name):
    """取名为 list_name 的 ListControl 下的全部 ListItem（RawView）。"""
    u = uia()
    cond_list = u.CreatePropertyCondition(UIA_ControlTypePropertyId, LIST)
    lists = elem.FindAll(UIA.TreeScope_Subtree, cond_list)
    target = None
    for i in range(lists.Length):
        le = lists.GetElement(i)
        if (le.CurrentName or "") == list_name:
            target = le
            break
    if target is None:
        return []
    cond_item = u.CreatePropertyCondition(UIA_ControlTypePropertyId, LIST_ITEM)
    items = target.FindAll(UIA.TreeScope_Descendants, cond_item)
    return [items.GetElement(i) for i in range(items.Length)]


def read_message_texts(hwnd):
    """读取聊天窗口消息列表里每条消息的文本（含空项）。"""
    el = message_list_elem(hwnd)
    if el is None:
        return []
    out = []
    for it in _elements(_find_all_ctl(el, LIST_ITEM)):
        try:
            out.append((it.CurrentName or "").replace("\r\n", "").strip())
        except Exception:
            out.append("")
    return out


def read_input_text(hwnd):
    """读聊天窗口输入框里的内容（用于确认消息是否真的发出去了）。

    两种读法：老客户端（Edit）给 Value；QQ NT 的网页输入框只有 Text 模式。
    读不到返回 None（空串是"框是空的"，两者含义完全不同）。
    """
    el = input_elem(hwnd)
    if el is None:
        return None
    v = elem_value(el)
    if v is not None:
        return v
    t = elem_text(el)
    if t is not None:
        return t
    # 元素可能已经失效（聊天窗口重建过），刷新一次再试
    el = _roles(hwnd, True).get("input")
    if el is None:
        return None
    v = elem_value(el)
    if v is not None:
        return v
    return elem_text(el)


def input_has_focus(hwnd):
    """输入框是否真的拿到了键盘焦点。抢到窗口前台 ≠ 焦点在输入框上。"""
    el = input_elem(hwnd)
    if el is None:
        return None
    try:
        return bool(el.CurrentHasKeyboardFocus)
    except Exception:
        return None


def focus_input(hwnd, retries=3):
    """把键盘焦点明确放到输入框，并确认真的放上了（最多试 retries 次）。

    抢到窗口前台不等于焦点在输入框上。焦点若在消息列表，后面的 Ctrl+A / Delete
    就会选中并试图删除聊天记录（弹确认框、触发 QQ 提示）；焦点没到位时
    发送键也会打空，表现为"回复发不出去"。
    """
    for i in range(retries):
        el = input_elem(hwnd)
        if el is not None:
            try:
                el.SetFocus()
            except Exception:
                # 有的版本（网页输入框）不实现 SetFocus，用点击兜底
                try:
                    r = _rect_of(el)
                    if r[2] - r[0] > 4 and r[3] - r[1] > 4:
                        click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                except Exception:
                    pass
        time.sleep(0.2 + 0.2 * i)
        if input_has_focus(hwnd) is True:
            return True
    return False

def _nz(s):
    """归一化：去掉所有空白。输入框/消息列表读回来跟原文的空格换行对不齐。"""
    return re.sub(r"\s+", "", s or "")


def _wait_input_empty(hwnd, timeout=1.5, poll=0.25):
    """等输入框变空。返回 True=已空 / False=还有内容 / None=自始至终读不到。

    刚按下发送就读输入框，UIA 经常还返回旧值。拿这个旧值判"没发出去"，就会把
    已经发出去的消息重排再发一遍 —— 对方收到两条措辞不同的回复。所以多等一会儿
    再下结论。正常情况第一次读就是空的，不会增加延迟。
    """
    deadline = time.time() + timeout
    saw_value = False
    while True:
        v = read_input_text(hwnd)
        if v == "":
            return True
        if v is not None:
            saw_value = True
        if time.time() >= deadline:
            return False if saw_value else None
        time.sleep(poll)


def _last_message_text(hwnd):
    """聊天窗口消息列表里最后一条有文字的条目（读不到返回 None）。"""
    try:
        items = [t for t in read_message_texts(hwnd) if t]
        return items[-1] if items else None
    except Exception:
        return None


def _looks_sent(hwnd, text):
    """消息列表末条是不是刚发出去的这句 —— 不依赖输入框的第二证据。

    输入框的读值会滞后，消息列表不会：11:45 的日志里有一条明明已经进了会话
    （预览里能看到），却被输入框判成"没发出去"。群里自己发的消息在列表里带昵称
    前缀，所以用包含关系而不是相等；太短的文本容易撞车，要求完全相等。
    """
    t = _nz(text)
    if not t:
        return False
    last = _nz(_last_message_text(hwnd))
    if not last:
        return False
    if len(t) < 4:
        return last == t
    if t in last:
        return True
    return len(last) >= 4 and last in t


# 发送键：Enter 还是 Ctrl+Enter 是 QQ 的设置项，各版本默认值不同（实测本机是
# Ctrl+Enter 发送、Enter 换行）。配置里可以写死；auto 时先按上次成功的组合按，
# 不成功就换另一个，成功后记住 —— 换版本/换设置后第一次会自动学会。
SEND_KEY = "auto"
_SEND_OK = {"key": ""}


def set_send_key(mode):
    """配置里的 send_key。显式指定时以配置为准，清掉自动学到的结果。"""
    global SEND_KEY
    mode = (mode or "auto").strip().lower().replace(" ", "")
    if mode not in ("enter", "ctrl+enter"):
        mode = "auto"
    if SEND_KEY != mode:
        _SEND_OK["key"] = ""
    SEND_KEY = mode
    return SEND_KEY


def _press_send(primary=None):
    """按一次发送键，返回实际用的组合（"enter" 或 "ctrl+enter"）。

    优先级：本次指定的 > 配置写死的 > 之前自动学到的 > 默认先试 Ctrl+Enter。
    上一版把"学到的"排在配置前面，于是在 auto 学会之后，配置里改成固定
    Enter 也不生效 —— 这属于配置被静默忽略，不能忍。
    """
    key = primary
    if not key:
        key = _SEND_OK["key"] if SEND_KEY == "auto" else SEND_KEY
    if not key:
        key = "ctrl+enter"
    if key == "enter":
        send_vk(VK_RETURN)
        return "enter"
    send_combo(VK_CONTROL, VK_RETURN)
    return "ctrl+enter"


def send_message(hwnd, text, settle=0.35, blockers=()):
    """把 text 发到指定聊天窗口（会抢前台）。返回 (ok, 原前台窗口, 说明)。

    blockers：真抢不到前台时，允许被最小化的挡路窗口（一般传 QQ 主窗口 hwnd）。
    """
    ok, prev = force_foreground(hwnd, blockers=blockers)
    if not ok:
        return False, prev, "could not bring window to foreground"

    # 焦点没落到输入框就绝不开打：字会打进消息列表或别的控件，而最后的
    # 发送校验读的是输入框 —— 它是空的，会被误判成"发送成功"，对方永远
    # 收不到。失败就返回 False，让调用方把消息重排。
    if focus_input(hwnd) is not True:
        return False, prev, "input focus failed"
    time.sleep(0.15)

    # 输入框本来就是空的就别按 Ctrl+A / Delete，那组键在焦点不对时是有副作用的
    if read_input_text(hwnd):
        send_combo(VK_CONTROL, VK_A)
        send_vk(VK_DELETE)
        time.sleep(0.15)

    notes = []
    send_unicode_text(text)
    time.sleep(settle)

    # 按下发送键之前先确认打进去的确实是要发的那句。原先只检查"发送后输入框里
    # 还剩不剩前 8 个字"，打字被 QQ 吞掉一截、只发出去半句是发现不了的。
    typed = read_input_text(hwnd)
    if typed is None:
        notes.append("typed unreadable")
    elif _nz(typed) != _nz(text):
        notes.append("typed mismatch")
        send_combo(VK_CONTROL, VK_A)
        send_vk(VK_DELETE)
        time.sleep(0.15)
        send_unicode_text(text)
        time.sleep(settle)
        again = read_input_text(hwnd)
        if again is not None and _nz(again) != _nz(text):
            notes.append("still mismatch")

    # Enter 和 Ctrl+Enter 哪个是"发送"是 QQ 的设置项，各版本默认值也不一样
    # （实测本机是 Ctrl+Enter）。先按配置/上次成功的组合按一次，不行换另一个。
    pressed = _press_send()
    notes.append("send key: " + pressed)
    time.sleep(settle + 0.4)

    tail = (" (%s)" % ", ".join(notes)) if notes else ""
    state = _wait_input_empty(hwnd)
    if state is True:
        _SEND_OK["key"] = pressed
        return True, prev, "sent" + tail
    # 输入框里还有内容：可能真没发出去，也可能只是读到了旧值。用消息列表核一次，
    # 避免把已经发出去的消息判成失败、重排后再发一遍。
    if _looks_sent(hwnd, text):
        _SEND_OK["key"] = pressed
        return True, prev, "sent (message list confirmed)" + tail
    if state is None:
        # 输入框一直读不到 = 没有任何证据。宁可当已发出：重排重发会让对方收到
        # 两条，而漏掉一次重排最多是这一句没出去，日志里留了痕。
        return True, prev, "unverified: input box unreadable" + tail
    # 真的还留在输入框里：换成另一个发送组合再试一次
    alt = "enter" if pressed == "ctrl+enter" else "ctrl+enter"
    _press_send(alt)
    time.sleep(settle + 0.4)
    if _wait_input_empty(hwnd, timeout=1.0) is True or _looks_sent(hwnd, text):
        _SEND_OK["key"] = alt
        return True, prev, "sent (%s fallback)" % alt
    return False, prev, "send failed: text still in input box" + tail


def click(x, y):
    """在屏幕物理坐标处单击（调用前需确保目标窗口在前台）。"""
    import win32api
    import win32con as _wcon
    win32api.SetCursorPos((int(x), int(y)))
    time.sleep(0.05)
    win32api.mouse_event(_wcon.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(_wcon.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.05)


def double_click(x, y):
    """在屏幕物理坐标处双击（调用前需确保目标窗口在前台）。"""
    import win32api
    import win32con
    win32api.SetCursorPos((int(x), int(y)))
    time.sleep(0.06)
    for _ in range(2):
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.07)


def conversation_items(hwnd=None):
    """会话列表项的 UIA 元素列表。

    只能「先按 ControlType 取出全部 ListItem，再逐个读 Name 比对」——
    实测本机 QQ NT 上 UIA 的属性条件查询（Name 等于某人）一律返回 0 条，
    而同一个元素的 CurrentName 却读得出来。以前这里用 CreatePropertyCondition
    (UIA_NamePropertyId, contact) 找会话项，于是「这个会话明明在列表里，
    却怎么也定位不到」，连带 open_chat 的双击兜底也失效。
    """
    u = uia()
    if hwnd is None:
        _, hwnd = find_main_window()
    if not hwnd:
        return []
    try:
        win = u.ElementFromHandle(hwnd)
        cond = u.CreatePropertyCondition(UIA_ControlTypePropertyId, LIST_ITEM)
        items = win.FindAll(UIA.TreeScope_Subtree, cond)
        return [items.GetElement(i) for i in range(items.Length)]
    except Exception:
        return []


def find_conversation_item(contact, hwnd=None):
    """按名字找会话列表项元素（遍历比对，不用属性条件）。"""
    try:
        for el in conversation_items(hwnd):
            if (el.CurrentName or "") == contact:
                return el
    except Exception:
        pass
    return None


def conversation_item_rect(contact, hwnd=None):
    """会话列表项在屏幕上的矩形（物理像素）。找不到返回 None。"""
    item = find_conversation_item(contact, hwnd)
    if item is None:
        return None
    try:
        r = item.CurrentBoundingRectangle
    except Exception:
        return None
    if r.right - r.left <= 0:
        return None
    return (r.left, r.top, r.right, r.bottom)


def open_chat(contact, timeout=6.0, pid=None, main_hwnd=None):
    """确保与 contact 的聊天窗口存在；不在则打开它。

    返回聊天窗口 hwnd 或 None。多开时用 pid / main_hwnd 指定是哪个 QQ 实例。

    两个阶段：
    1. 先试 UIA 的 Invoke / DoDefaultAction —— 它不依赖窗口可见性，也就不用把
       整个 QQ 主窗口 ShowWindow 出来。原来一上来就 SW_RESTORE 主窗口，不但会让它
       反复在屏幕上弹，还会连带把积压的通知刷出来（每回复一次响一声）。
    2. 实在不行才退回「恢复主窗口 + 屏幕坐标双击」，并重试两遍。
    """
    _, chwnd = find_chat_window(contact, pid=pid)
    if chwnd:
        return chwnd

    if main_hwnd is None:
        _, main_hwnd = find_main_window()
    if not main_hwnd:
        return None

    try:
        item = _conversation_item_elem(contact, main_hwnd)
        if item is not None and (_invoke_item(item) or _do_default_action(item)):
            hw = _wait_chat_window(contact, pid, timeout)
            if hw:
                return hw
    except Exception:
        pass

    for _ in range(2):
        user32.ShowWindow(main_hwnd, SW_RESTORE)
        time.sleep(0.6)
        force_foreground(main_hwnd)
        rect = conversation_item_rect(contact, main_hwnd)
        if rect:
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2
            # 会话列表被别的窗口（常是聊天窗口）压住时，这一下双击会打到别人身上，
            # 于是"打开了但显示的根本不是它"。先把挡路的那个 QQ 窗口最小化再点。
            owner = point_owner_root(cx, cy)
            if owner and owner != main_hwnd and _is_qq_window(owner):
                try:
                    user32.ShowWindow(owner, SW_MINIMIZE)
                    time.sleep(0.4)
                    force_foreground(main_hwnd)
                    rect = conversation_item_rect(contact, main_hwnd)
                    if not rect:
                        continue
                    cx = (rect[0] + rect[2]) // 2
                    cy = (rect[1] + rect[3]) // 2
                except Exception:
                    pass
            double_click(cx, cy)
            hw = _wait_chat_window(contact, pid, timeout)
            if hw:
                return hw
        time.sleep(0.8)
    return None


def _is_qq_window(hwnd):
    try:
        return "TXGuiFoundation" in win32gui.GetClassName(hwnd)
    except Exception:
        return False


def ensure_chat_window(contact, pid=None, main_hwnd=None, timeout=6.0):
    """拿到一个「当前正显示 contact、可以马上发消息」的窗口 hwnd，失败返回 None。

    按代价从小到大试：
      1. 已经有窗口正显示这个会话（哪怕最小化了）→ 直接用，发送时会还原
      2. 合并窗口里有它的 Tab，只是当前显示的是别人 → 切过去（切完校验，防止发错人）
      3. 实在没有 → 走 open_chat（UIA Invoke，不行才双击），同样等它出现并校验
    """
    hwnd, state = locate_chat(contact, pid)
    if state == "active":
        return hwnd
    if state == "tab":
        # 合并窗口里装着它、但当前显示的是别人：
        # ① 最小化的合并窗口一还原，QQ 常常直接拆成独立窗口，拆完就能命中；
        # ② 没拆就试着切 Tab（对已打开的合并窗口，UIA 切换经常不生效，会失败）
        try:
            if win32gui.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.9)
        except Exception:
            pass
        h2, s2 = locate_chat(contact, pid)
        if s2 == "active":
            return h2
        if h2 and activate_tab(h2, contact):
            return h2

    # 都不行就从主窗口的会话列表里打开/切过去 —— 这才是 QQ 自己的"打开会话"入口，
    # 比在合并窗口里硬切 Tab 可靠得多
    hwnd = open_chat(contact, timeout=timeout, pid=pid, main_hwnd=main_hwnd)
    if hwnd:
        if active_conversation(hwnd) == contact:
            return hwnd
        if activate_tab(hwnd, contact):
            return hwnd

    # 窗口可能刚被异步创建出来，再扫两秒
    deadline = time.time() + 2.0
    while time.time() < deadline:
        h, s = locate_chat(contact, pid)
        if s == "active":
            return h
        time.sleep(0.5)
    return None


def _wait_chat_window(contact, pid, timeout=6.0):
    """轮询等待"某个窗口当前显示的就是 contact"。

    只等"窗口出现"是不够的：QQ 会把新开的会话并进已有的合并窗口，
    窗口是找到了，但显示的可能是别人 —— 那就等着发错人。所以这里等的是
    active_conversation() == contact。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd, state = locate_chat(contact, pid)
        if hwnd and state == "active":
            return hwnd
        time.sleep(0.4)
    return None


def _conversation_item_elem(contact, hwnd):
    """返回名为 contact 的会话列表项元素，找不到返回 None。

    走 find_conversation_item()（遍历比对）；属性条件查询在 QQ NT 上不返回东西。
    """
    return find_conversation_item(contact, hwnd)


def _invoke_item(elem):
    try:
        pat = elem.GetCurrentPattern(UIA_InvokePatternId)
        if not pat:
            return False
        pat.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
        return True
    except Exception:
        return False


def _do_default_action(elem):
    try:
        pat = elem.GetCurrentPattern(UIA_LegacyIAccessiblePatternId)
        if not pat:
            return False
        pat.QueryInterface(UIA.IUIAutomationLegacyIAccessiblePattern).DoDefaultAction()
        return True
    except Exception:
        return False


UIA_InvokePatternId = 10000
UIA_LegacyIAccessiblePatternId = 10018


def find_main_windows():
    """所有 QQ 主窗口（多开时每个实例一个，标题通常都叫 'QQ'）。

    以前写死标题 == "QQ"，新版/TIM/英文版的标题可能带版本号或干脆是空的。
    这里先按标题认，认不到再按结构认：**没有输入框、却有一串带预览的列表项**
    的窗口就是主窗口。聊天窗口有输入框，所以不会被误认。
    """
    result = []
    for name, hwnd in top_windows():
        n = (name or "").strip().upper()
        if n in ("QQ", "TIM") or n.startswith(("QQ ", "TIM ")):
            result.append((name, hwnd))
    if result:
        return result
    for name, hwnd in top_windows():
        if has_input_box(hwnd):
            continue
        try:
            convs = all_conversations(hwnd)
        except Exception:
            convs = None
        if not convs or len(convs) < 3:
            continue
        # 会话项的 LegacyIAccessible.Value 是消息预览；消息列表的条目没有这个值。
        # 要求大多数条目带预览，避免把"只有一堆消息文本"的窗口当成主窗口。
        with_preview = sum(1 for _n, v in convs if v)
        if with_preview >= max(3, len(convs) * 0.6):
            result.append((name, hwnd))
    return result


def find_main_window():
    """兼容旧调用：返回第一个主窗口。多开场景请用 find_main_windows。"""
    wins = find_main_windows()
    return wins[0] if wins else (None, None)


def legacy_value(elem):
    """MSAA 兼容层的 Value —— 会话列表项的最后一条消息预览在这里。"""
    try:
        pat = elem.GetCurrentPattern(UIA_LegacyIAccessiblePatternId)
        if not pat:
            return None
        lp = pat.QueryInterface(UIA.IUIAutomationLegacyIAccessiblePattern)
        return lp.CurrentValue
    except Exception:
        return None


def _conversation_elements(hwnd):
    """会话项元素：正常的 QQ 是 ListItem；有的版本（网页内核）用 DataItem。"""
    win = _elem_from(hwnd)
    for ct in (LIST_ITEM, DATA_ITEM):
        good = []
        for it in _elements(_find_all_ctl(win, ct)):
            nm = _name_of(it)
            if nm and len(nm) <= 120:      # 会话名不会太长，长的是消息正文
                good.append(it)
        if good:
            return good
    return []


def all_conversations(hwnd=None, with_preview=True):
    """读取会话列表的全部条目，返回 [(name, legacy_value), ...]。

    hwnd 省略时用第一个 QQ 主窗口；多开时必须显式传入。
    with_preview=False 时不读预览（每个条目省一次跨进程 COM 调用）——
    多开认账号时只需要名字，而另一个实例可能有上百个会话，全读一遍很贵。
    只读、不抢焦点；读不到时返回 None。
    """
    if hwnd is None:
        _, hwnd = find_main_window()
    if not hwnd:
        return None
    try:
        els = _conversation_elements(hwnd)
        if with_preview:
            return [(_name_of(it), legacy_value(it)) for it in els]
        return [(_name_of(it), None) for it in els]
    except Exception:
        return None


def conversation_preview(contact, hwnd_main=None):
    """读指定联系人的最后一条消息预览（后台读取，不抢焦点）。

    注意：QQ 主窗口的 hwnd 会变（关闭再打开后是新的），所以默认每次重新查找。
    走 all_conversations 的遍历结果比对名字 —— 属性条件查询在 QQ NT 上不返回东西。
    """
    for nm, lv in (all_conversations(hwnd_main) or []):
        if nm == contact:
            return lv
    return None


def chat_windows(pid=None, with_draft=False):
    """当前所有已打开的聊天窗口。

    with_draft=False -> [(当前显示的会话名, hwnd), ...]
    with_draft=True  -> [(当前显示的会话名或 None, hwnd, 输入框内容), ...]

    只读：不打开新窗口、不改窗口状态。靠 active_conversation() 认窗口里现在是谁
    —— 它是标题栏那块 Pane，比窗口标题可靠（标题在「合并会话窗口」下会变成
    「小明等2个会话」）。

    先用 IsWindowVisible 粗筛是必须的：QQ 的顶层窗口绝大多数是隐藏的辅助窗口和右键
    菜单窗口（实测 18 个里 17 个 visible=0），而 has_input_box / read_input_text 对
    一个"不是聊天窗口"的窗口要等满 0.8 秒超时才返回 —— 实测 17 个候选一轮要花 14 秒，
    主循环直接被拖成 15 秒一轮。聊天窗口即使最小化也是 visible=1，筛不掉它们。

    with_draft=True 时顺序是特意颠倒的：先读输入框（一次 UIA 查找），只有框里真有字时
    才去解析会话名（那一步要遍历上百个 Pane，贵得多）。人工打字检测只需要知道"哪个
    窗口的框里有东西"，而框绝大多数时候是空的。
    """
    mains = {h for _n, h in find_main_windows()}
    out = []
    for title, hwnd in top_windows():
        if hwnd in mains or not _pid_match(hwnd, pid):
            continue
        try:
            if not win32gui.IsWindowVisible(hwnd):
                continue
        except Exception:
            continue
        if not with_draft:
            if not has_input_box(hwnd):
                continue
            who = active_conversation(hwnd)
            out.append((who or title or "", hwnd))
            continue
        txt = read_input_text(hwnd)
        if txt is None:                 # 没有输入框 -> 不是聊天窗口
            continue
        who = active_conversation(hwnd) if _nz(txt) else None
        out.append((who, hwnd, txt))
    return out


# ————————————————————————————————————————————————
# 识图：把聊天窗口的消息列表截一块给模型看
# ————————————————————————————————————————————————
# 实测结论（2026-09-24，本机 QQ 9.x，屏幕缩放 200%）：
# 1. 消息列表里每条消息的 ListItem 不给 BoundingRectangle —— 最小化、显示、激活
#    三种状态下读出来都是 0，定位不到"图片那一条"，只能整块截列表；好在外层那个
#    名为「消息」的 List 有矩形，用它把标题栏、输入框、侧栏都裁掉。
# 2. 窗口最小化时 PrintWindow 抓到的是近乎空白的图（唯一颜色 63，正常两万多种），
#    所以必须先显示出来，抓完再收回最小化。全程 SW_SHOWNOACTIVATE，不抢焦点。

def _print_window(hwnd, width, height):
    """PrintWindow 把整个窗口渲染成位图，返回 (是否成功, PIL 图)。
    不要求窗口在前台，但要求它不是最小化的。"""
    hwnd_dc = mfc_dc = save_dc = bmp = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bmp)
        ret = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = bmp.GetInfo()
        # 注意 orientation：这里实测（QQ NT + PW_RENDERFULLCONTENT，2026-09-24）
        # 拿到的位图数据是顶行在前，要用 1；写成 -1（bottom-up 的老习惯）会把
        # 整张截图上下颠倒 —— 标题栏跑到最底下、文字全部倒置，模型基本读不了。
        img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                               bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        return bool(ret), img
    except Exception:
        return False, None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            if save_dc is not None:
                save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def message_list_rect(hwnd):
    """消息列表的屏幕矩形；拿不到返回 None。"""
    el = message_list_elem(hwnd)
    if el is None:
        return None
    r = _rect_of(el)
    if r[2] > r[0] and r[3] > r[1]:
        return r
    return None


def capture_messages(hwnd, keep_ratio=1.0, keep_max=0, max_side=0, settle=0.7):
    """截一张聊天窗口的消息列表回来，返回 (PIL 图, 说明)；失败时图是 None。

    默认不裁不缩，因为实测这两件事都会毁识别率：同一张写着"杀鲸霸拳"的表情包，
    完整贴进截图里模型读对了，只露出下半截时读成"吃霸拳"；缩到 1024 长边也会把
    图里的字压糊。参数是留给想省钱时收紧用的：keep_ratio / keep_max 控制保留列表
    底部多高（0 表示不限），max_side 控制最长边（0 表示不缩）。图片 token 按像素算，
    约等于 宽×高÷1024，所以不裁不缩一次大概 600-1000 token。
    调用期间会把最小化的窗口显示出来、抓完收回去，不抢焦点。
    """
    if Image is None:
        return None, "没装 Pillow"
    was_iconic = False
    try:
        was_iconic = bool(win32gui.IsIconic(hwnd))
    except Exception:
        pass
    try:
        if was_iconic:
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            time.sleep(settle)
        rect = message_list_rect(hwnd)
        if not rect:
            return None, "没找到消息列表"
        wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
        ok, img = _print_window(hwnd, wr - wl, wb - wt)
        if not ok or img is None:
            return None, "PrintWindow 失败"
        left, top = max(0, rect[0] - wl), max(0, rect[1] - wt)
        right = min(img.size[0], rect[2] - wl)
        bottom = min(img.size[1], rect[3] - wt)
        if right - left < 40 or bottom - top < 40:
            return None, "消息列表矩形不可用"
        crop = img.crop((left, top, right, bottom))
        h = crop.size[1]
        keep = max(int(h * keep_ratio), 240)                    # 至少留 240 像素
        if keep_max:
            keep = min(keep, keep_max)
        keep = min(keep, h)
        crop = crop.crop((0, h - keep, crop.size[0], h))        # 从底部往上取
        if max_side and max(crop.size) > max_side:
            scale = float(max_side) / float(max(crop.size))
            crop = crop.resize((max(1, int(crop.size[0] * scale)),
                                max(1, int(crop.size[1] * scale))))
        return crop, "%dx%d" % crop.size
    except Exception as e:
        return None, repr(e)[:120]
    finally:
        if was_iconic:
            try:
                user32.ShowWindow(hwnd, SW_MINIMIZE)
            except Exception:
                pass


# ————————————————————————————————————————————————
# 图片 / 表情包
# ————————————————————————————————————————————————

class _DROPFILES(ctypes.Structure):
    _fields_ = [("pFiles", wintypes.DWORD),
                ("pt", wintypes.POINT),
                ("fNC", wintypes.BOOL),
                ("fWide", wintypes.BOOL)]


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def _set_clipboard(fmt, data):
    """往剪贴板写数据，成功返回 True。"""
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(fmt, data)
            return True
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        return False


def _dib_from_image(path, max_side=640):
    """任意格式 -> CF_DIB 字节流。需要 Pillow；失败返回 None。"""
    if Image is None:
        return None
    try:
        img = Image.open(path)
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side))
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "BMP")
        return buf.getvalue()[14:]          # 去掉 14 字节 BITMAPFILEHEADER 才是 DIB
    except Exception:
        return None


def _dib_from_bmp_file(path):
    """bmp 文件直接剥掉文件头，不需要 Pillow。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:2] == b"BM":
            return raw[14:]
    except Exception:
        pass
    return None


def _dib_from_gif_frame(path, max_side=640, which="first"):
    """取 GIF 的一帧做成 CF_DIB。

    这条路发出去一定是"内联显示的图片"，只是不动。它有两个用途：
    1. gif_mode="frame" 时的唯一通道（保证一定能显示）；
    2. gif_mode="auto" 时先放一张静态位图，让 QQ 认出"这是图片而不是文件"，
       后面再补上 GIF 文件路径，QQ 就会当动图发出去。
    """
    if Image is None:
        return None
    try:
        img = Image.open(path)
        n = getattr(img, "n_frames", 1)
        idx = 0 if which != "mid" else min(n - 1, n // 2)
        img.seek(idx)
        img = img.convert("RGBA")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side))
        bg = Image.new("RGB", img.size, (255, 255, 255))   # 透明底填白，QQ 里不会糊成黑块
        bg.paste(img, mask=img.split()[3])
        buf = io.BytesIO()
        bg.save(buf, "BMP")
        return buf.getvalue()[14:]
    except Exception:
        return None


GIF_CACHE_DIR = os.path.join(tempfile.gettempdir(), "qq_auto_reply_gif")


def shrink_gif(path, max_side=640, max_bytes=900 * 1024):
    """GIF 太大时重编码成更小的动图，返回可直接使用的路径（失败则原样返回）。

    QQ 有个没写在明面上的阈值：动图体积太大时会降级成"文件传输"，
    对方就必须下载查看原图了。你目录里那张 3MB 的 GIF 正好踩在这条线上。
    压缩结果按 (文件名+大小+参数) 做缓存，同一张图只压一次。
    """
    if Image is None or not path or not os.path.exists(path):
        return path
    try:
        src_size = os.path.getsize(path)
        im = Image.open(path)
        w, h = im.size
        if src_size <= max_bytes and max(w, h) <= max_side:
            return path
    except Exception:
        return path

    try:
        os.makedirs(GIF_CACHE_DIR, exist_ok=True)
        key = "%s|%d|%d|%d" % (os.path.basename(path), src_size, max_side, max_bytes)
        out = os.path.join(GIF_CACHE_DIR,
                           hashlib.md5(key.encode("utf-8", "ignore")).hexdigest()[:16] + ".gif")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        return path

    # 压缩阶梯：先减颜色（几乎不损失清晰度），实在不行才降分辨率。
    # 一上来就缩尺寸的话，240x426 的动图会被压到 117 宽，糊得没法看。
    cap = min(1.0, float(max_side) / float(max(w, h)))
    ladder = [(cap, 128), (cap, 64), (cap * 0.75, 64), (cap * 0.55, 64), (cap * 0.4, 64)]
    try:
        data = None
        for scale, colors in ladder:
            scale = max(scale, 0.2)
            tw, th = max(1, int(w * scale)), max(1, int(h * scale))
            im = Image.open(path)
            n = getattr(im, "n_frames", 1)
            frames, durations = [], []
            for i in range(n):
                im.seek(i)
                fr = im.convert("RGBA")
                flat = Image.new("RGBA", fr.size, (255, 255, 255, 255))
                flat.alpha_composite(fr)              # 透明帧先铺白底
                fr = flat.convert("RGB")
                if fr.size != (tw, th):
                    fr = fr.resize((tw, th))
                frames.append(fr)
                durations.append(int(im.info.get("duration", 100)))
            # 全部帧共用第一帧的调色板，避免逐帧抖动、也更好压
            pal = frames[0].convert("P", palette=Image.ADAPTIVE, colors=colors)
            seq = [pal] + [f.quantize(palette=pal, dither=Image.FLOYDSTEINBERG)
                           for f in frames[1:]]
            buf = io.BytesIO()
            seq[0].save(buf, "GIF", save_all=True, append_images=seq[1:],
                        duration=durations, loop=0, disposal=2, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                break
        with open(out, "wb") as f:
            f.write(data)
        return out
    except Exception:
        return path


def _hdrop_bytes(path):
    """构造 CF_HDROP：等价于在资源管理器里复制了这个文件。"""
    try:
        hdr = _DROPFILES()
        hdr.pFiles = ctypes.sizeof(_DROPFILES)
        hdr.pt = wintypes.POINT(0, 0)
        hdr.fNC = False
        hdr.fWide = True
        # CF_HDROP 规范要求路径列表以双 NUL 结尾（路径本身一个 + 列表终止
        # 再一个，UTF-16 下共 4 字节）。以前只补了 2 字节，QQ 容忍了，
        # 但别的解析器（资源管理器粘贴、拖放 API）可能读越界。
        return bytes(hdr) + path.encode("utf-16-le") + b"\x00\x00\x00\x00"
    except Exception:
        return None


def _set_clipboard_multi(items):
    """按顺序往剪贴板里写多个格式。返回实际写进去的格式名列表。

    顺序是有讲究的：对 GIF 要先放位图再放文件路径，QQ 才会把它当图片
    （而不是当文件）来处理，同时又能从文件路径拿到动画帧。
    """
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            done = []
            for fmt, data, tag in items:
                if not data:
                    continue
                try:
                    win32clipboard.SetClipboardData(fmt, data)
                    done.append(tag)
                except Exception:
                    pass
            return done
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        return []


def put_image_on_clipboard(path, max_side=640, gif_mode="auto", gif_frame="first",
                           gif_max_bytes=900 * 1024):
    """把图片放上剪贴板，返回实际使用的格式（"dib+hdrop" / "dib" / "hdrop"），失败返回 None。

    gif_mode:
      auto  —— 剪贴板里同时放"静态首帧位图"和"GIF 文件"，QQ 会按动图内联发送（推荐）
      frame —— 只放静态首帧位图，必定内联显示，但不会动
      file  —— 只放文件，旧行为，对方要下载查看原图
    太大的动图会自动压缩（gif_max_bytes=0 关闭压缩），否则 QQ 会降级成文件传输。
    """
    if not path or not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    mode = (gif_mode or "auto").strip().lower()

    if ext == ".gif":
        dib = _dib_from_gif_frame(path, max_side, gif_frame)
        src = shrink_gif(path, max_side, gif_max_bytes) if gif_max_bytes > 0 else path
        hdrop = _hdrop_bytes(src)
        if mode == "frame":
            return "dib" if (dib and _set_clipboard(CF_DIB, dib)) else None
        if mode == "file":
            return "hdrop" if (hdrop and _set_clipboard(CF_HDROP, hdrop)) else None
        # auto：位图在前、文件在后
        done = _set_clipboard_multi([(CF_DIB, dib, "dib"), (CF_HDROP, hdrop, "hdrop")])
        if done:
            return "+".join(done)
        return None

    if ext == ".bmp":
        data = _dib_from_bmp_file(path)
        if data and _set_clipboard(CF_DIB, data):
            return "dib"

    data = _dib_from_image(path, max_side)
    if data and _set_clipboard(CF_DIB, data):
        return "dib"

    data = _hdrop_bytes(path)
    if data and _set_clipboard(CF_HDROP, data):
        return "hdrop"
    return None


def send_image(hwnd, path, settle=0.6, max_side=640, gif_mode="auto",
               gif_frame="first", gif_max_bytes=900 * 1024, blockers=()):
    """往聊天窗口贴一张图片并发送。返回 (ok, info)。

    诚实说明：图片有没有真的附上，UIA 读不出来（输入框的 Value 还是空字符串），
    所以这里只能保证"剪贴板放好了 + Ctrl+V 和发送键都按下去了"。
    首次使用请自己看一眼聊天窗口确认。
    """
    fmt = put_image_on_clipboard(path, max_side=max_side, gif_mode=gif_mode,
                                 gif_frame=gif_frame, gif_max_bytes=gif_max_bytes)
    if not fmt:
        return False, "clipboard failed (Pillow? 文件格式?)"

    ok, prev = force_foreground(hwnd, blockers=blockers)
    if not ok:
        return False, "could not bring window to foreground"

    # 同 send_message：焦点不在输入框时 Ctrl+V 可能贴到别处，宁可不发
    if focus_input(hwnd) is not True:
        return False, "input focus failed"
    time.sleep(0.2)
    send_combo(VK_CONTROL, VK_V)
    time.sleep(settle)
    # 发送键和文字走同一套（Enter / Ctrl+Enter 因 QQ 设置而异）
    _press_send()
    time.sleep(settle + 0.3)
    return True, "paste(%s)" % fmt


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "h"
    msg = sys.argv[2] if len(sys.argv) > 2 else "【脚本测试】你好"
    name, hwnd = find_chat_window(target)
    print("window:", name, hwnd)
    if hwnd:
        before = read_message_texts(hwnd)
        print("messages before: %d" % len(before))
        print("last 3:", before[-3:])
        ok, prev, info = send_message(hwnd, msg)
        print("send:", ok, info, "prev_fg=", prev)
        time.sleep(1.2)
        after = read_message_texts(hwnd)
        print("messages after: %d" % len(after))
        print("last 3:", after[-3:])
