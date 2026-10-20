# -*- coding: utf-8 -*-
"""QQ 自动回复（可监听一个或多个联系人，本机账号被动接收）。

配置：同目录下的 auto_reply_config.json（环境变量可覆盖同名项）
    targets     要监听的联系人列表，例如 ["小明", "某某"]；留空且 listen_all=false 则监听 target 单值
    target      单联系人写法（兼容旧配置）
    listen_all  为 true 且 targets 为空时，监听全部会话（风险高，慎用）
    ignore      黑名单，优先级最高
    api_key / base_url / model / system_prompt

设计原则（都是实测踩出来的）：
- 只做两件事：后台读取（不抢焦点）、需要回复时短暂抢前台发送。
- 不做任何自动唤醒/前置窗口的动作；主窗口隐藏或最小化照样能读到会话列表。
- 找窗口用 Win32 EnumWindows（能看到隐藏窗口），不用 UIA 根元素遍历。
- 聊天窗口不在时双击会话项打开（已验证有效）。
- 发送用 Ctrl+Enter，并在发送后校验输入框是否清空。
- system_prompt 每次回复前重读，改完提示词不用重启。
- 生成回复（LLM 请求）在后台线程跑，主循环等模型期间照常读会话列表；
  对 QQ 的实际写入（开窗、抢前台、按键）仍然只在主线程串行进行。
"""
import base64
import ctypes
import datetime
import io
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

import win32process

import qq_driver as qq

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "auto_reply_config.json")

DEFAULT_SYSTEM_PROMPT = (
    "你正在用 QQ 和朋友聊天，你的身份就是账号本人。"
    "回复要短、口语化、像真人随手打的，通常一到两句，不要超过 50 字。"
    "不要使用列表、不要书面语、不要提到自己是 AI。"
)

# 自己发出的消息会回显到会话预览里，读不到消息方向，只能靠内容比对。
# 发送成功时登记一条「预期回显」，见到匹配的预览就消费掉（一次有效）；
# 超过这个时限的预期作废。每条预期只认领一次，对方后续发的相似消息不会被误吞。
SELF_ECHO_WINDOW = 90.0
# 循环熔断：回显识别万一失效，机器人会在几秒内把聊天刷爆。
# 这个窗口内回复次数超上限就强制冷却，宁可少回也不能刷屏。
LOOP_WINDOW = 180.0
# 窗口内的回复数达到这个量，而且期间几乎没收到对方的新消息 → 判定自我循环
LOOP_MAX_REPLIES = 15
LOOP_MAX_REAL_INCOMING = 2
LOOP_BREAK_COOLDOWN = 120.0
MAX_HISTORY_TURNS = 10
HEARTBEAT_SECONDS = 60.0
# 一轮对话占两条消息（user + assistant）。发给模型时按轮数截断，本地留存的也必须同步截断，
# 否则开着几天不重启就会一路涨。
MAX_HISTORY_MESSAGES = MAX_HISTORY_TURNS * 2
# state 用 (主窗口 hwnd, 联系人) 当 key，而 QQ 重启后 hwnd 是新值：
# 旧 key 既不会再被读到、也不会被删掉，所以要按「多久没再见到」清理。
STALE_STATE_SECONDS = 3600.0
# 单实例互斥体名（会话级，不需要管理员权限）
SINGLE_INSTANCE_MUTEX = "Local\\QQAutoReply_SingleInstance_v1"
# 崩溃自愈：进程内重启的上限与间隔
MAX_RESTARTS = 5
RESTART_BACKOFF_SECONDS = 10
# 稳定运行超过这个时长后，崩溃计数归零，避免历史故障一直累计
RESTART_RESET_SECONDS = 600.0

STATUS_PATH = os.path.join(HERE, "auto_reply_status.json")


LOG_PATH = os.path.join(HERE, "auto_reply.log")


def log(msg):
    # 带上日期：日志是长期追加的，多天的记录混在一个文件里，
    # 没有日期根本分不清「21:15 的成功回复」是今天还是三天前。
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if sys.stdout is not None:      # pythonw 下 stdout 是 None
            print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def rotate_log(max_bytes=2000000):
    """日志超过约 2MB 就轮转一次，避免开机自启后无限增长。"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > max_bytes:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except Exception:
        pass


def write_json_atomic(path, obj):
    """先写临时文件再 os.replace 换上去，避免进程崩在写一半时留下坏 JSON。

    history / status 都是直接 open("w") 覆盖写，写坏一次就是永久损坏：
    history 坏了重启后上下文全丢，status 坏了 stop 脚本读 pid 也失效。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def wait_for_main_window(timeout=300):
    """等待 QQ 主窗口出现（开机自启时 QQ 可能还没启动/登录完）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _, hwnd = qq.find_main_window()
        except Exception:
            hwnd = None
        if hwnd:
            return hwnd
        time.sleep(3)
    return None


# 上一次成功读到的配置。配置写坏时（多一个逗号之类）不能退回默认值：
# DEFAULT_LLM 的 api_key 是空的，一路走下去就变成"回声模式"，对着真人回
# "收到：xxx"。这里改成沿用上一份可用配置，并记住"坏了"，由 make_reply
# 在确实没有 key 时拒绝回声。
_LAST_GOOD_CFG = {}
_CFG_STATE = {"broken": False, "logged": False}


def load_config(path=None):
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        # 文件不存在是另一回事（就是没配 key），保持原来的回声模式语义
        _CFG_STATE["broken"] = False
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("配置根节点不是 JSON 对象")
    except Exception as e:
        _CFG_STATE["broken"] = True
        if not _CFG_STATE["logged"]:
            # 这个函数每次请求前都会被调用，坏了就会一直失败：只在状态翻转时
            # 记一行，否则同一行错误会把日志刷爆。
            log("配置文件读取失败（%s）：%r —— 沿用上一次成功读取的配置，"
                "修好之前不会退回回声模式" % (path, e))
            _CFG_STATE["logged"] = True
        return dict(_LAST_GOOD_CFG)
    _CFG_STATE["broken"] = False
    _CFG_STATE["logged"] = False
    _LAST_GOOD_CFG.clear()
    _LAST_GOOD_CFG.update(cfg)
    return cfg


CFG = load_config()


def setting(key, env_name, default="", cfg=None):
    v = os.environ.get(env_name)
    if v is not None and str(v).strip():
        return str(v).strip()
    v = (cfg if cfg is not None else CFG).get(key)
    if v is None:
        return default
    # JSON 里的数字和布尔也要认。以前只认字符串，结果 config 里写
    # "max_tokens": 2000 会被静默忽略、悄悄落回代码默认值 —— 改了等于没改。
    # 注意 bool 是 int 的子类，必须先判。
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return default


def setting_list(key):
    v = CFG.get(key)
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


def setting_bool(key, default=False):
    v = CFG.get(key)
    return v if isinstance(v, bool) else default


def setting_int(key, env_name, default, cfg=None):
    v = setting(key, env_name, "", cfg=cfg)
    try:
        return int(v) if str(v).strip() else default
    except Exception:
        return default


def setting_float(key, env_name, default, cfg=None):
    v = setting(key, env_name, "", cfg=cfg)
    try:
        return float(v) if str(v).strip() else default
    except Exception:
        return default


def prompt_from_lines(cfg):
    """支持按行写提示词：config 里的 system_prompt_lines 是个数组，一行一条，
    这里拼成一个多行字符串。比在 JSON 里手写 \\n 好改得多。没配就返回 None。"""
    lines = cfg.get("system_prompt_lines")
    if isinstance(lines, list):
        got = [str(x).strip() for x in lines if str(x).strip()]
        if got:
            return "\n".join(got)
    return None


# 多开时用来认账号：只有会话列表里同时包含这些会话名的 QQ 实例才会被处理。
# 例如小号的列表里有"小明"（大号的备注），而大号自己没有这个会话。
ACCOUNT_MARKERS = setting_list("account_markers")

TARGET = setting("target", "AUTO_REPLY_TARGET", "")
LISTEN_ALL = setting_bool("listen_all", False)
IGNORE = setting_list("ignore")
_targets = setting_list("targets")
# 之前的写法 `setting_list("targets") or [TARGET]` 会让空 targets 回退成单联系人，
# 导致 listen_all 永远不生效。只有既没配 targets、也没开 listen_all 时才回退。
TARGETS = _targets if _targets else ([] if (LISTEN_ALL or not TARGET) else [TARGET])
TARGET_UIN = setting("target_uin", "AUTO_REPLY_UIN", "")
# 严格账号：account_markers 对不上时宁可完全不回，也不用"只有一个实例"的兜底。
# 多开、且担心回错号时打开。
STRICT_ACCOUNT = setting_bool("strict_account", False)

# —— 名单热更新 ——
# targets / ignore / listen_all / skip_group_like / send_key 以前只在启动时读一次，
# 改完必须重启（重启会丢消息队列和人工接管状态）。现在每轮比对配置文件的
# mtime：用 `python auto_reply.py pick` 改完，正在跑的脚本下一个轮询就生效。
_WATCH = {"sig": None, "first": True, "targets": TARGETS, "ignore": IGNORE,
          "listen_all": LISTEN_ALL, "skip_group": True, "send_key": "auto",
          "markers": ACCOUNT_MARKERS, "strict": STRICT_ACCOUNT}


def _config_sig():
    try:
        st = os.stat(CONFIG_PATH)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _as_name_list(value):
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def refresh_watch_settings(force=False):
    """配置里的名单/开关变了就重读，返回是否有变化。

    listen_all 与 targets 的优先级：配了 targets 就只回名单里的人；
    targets 为空才看 listen_all。这一条和旧版一致，所以老配置不会变味。
    """
    global TARGETS, IGNORE, LISTEN_ALL, SKIP_GROUP_LIKE, ACCOUNT_MARKERS, STRICT_ACCOUNT
    sig = _config_sig()
    if not force and sig is not None and sig == _WATCH["sig"]:
        return False
    cfg = load_config()
    targets_raw = cfg.get("targets")
    targets = _as_name_list(targets_raw) if isinstance(targets_raw, list) else []
    listen_all = (cfg.get("listen_all") if isinstance(cfg.get("listen_all"), bool)
                  else LISTEN_ALL)
    # 老配置可能只有 target（单联系人）、没有 targets 键 —— 那种才回退。
    # 显式写成 "targets": [] 表示"谁都不回"，不能再被 target 悄悄顶回来，
    # 否则在 pick 里选 none 会看起来没生效。
    if not isinstance(targets_raw, list) and not listen_all and TARGET:
        targets = [TARGET]
    ignore = (_as_name_list(cfg.get("ignore"))
              if isinstance(cfg.get("ignore"), list) else IGNORE)
    skip_group = (cfg.get("skip_group_like")
                  if isinstance(cfg.get("skip_group_like"), bool) else SKIP_GROUP_LIKE)
    send_key = str(cfg.get("send_key") or "auto").strip().lower() or "auto"
    markers = (_as_name_list(cfg.get("account_markers"))
               if isinstance(cfg.get("account_markers"), list) else ACCOUNT_MARKERS)
    strict = (cfg.get("strict_account") if isinstance(cfg.get("strict_account"), bool)
              else STRICT_ACCOUNT)
    new = {"targets": targets, "ignore": ignore, "listen_all": bool(listen_all),
           "skip_group": bool(skip_group), "send_key": send_key,
           "markers": markers, "strict": bool(strict)}
    changed = any(_WATCH.get(k) != v for k, v in new.items())
    first = _WATCH["first"]
    _WATCH.update(new)
    _WATCH["first"] = False
    _WATCH["sig"] = sig
    TARGETS, IGNORE, LISTEN_ALL, SKIP_GROUP_LIKE, ACCOUNT_MARKERS = (
        targets, ignore, bool(listen_all), bool(skip_group), markers)
    STRICT_ACCOUNT = bool(strict)
    qq.set_send_key(send_key)
    if changed and not first:
        log("配置热更新：targets=%s ignore=%s listen_all=%s account_markers=%s send_key=%s"
            % (targets, ignore, bool(listen_all), markers, send_key))
    return changed


def _norm_name(s):
    """会话名归一化：去掉空格、零宽字符，忽略大小写。

    QQ 的会话名里常混着全角空格和不可见字符，手工抄进配置基本对不上；
    归一化之后「小明」「素 士」「小明\\u200b」都能匹配上。
    """
    return re.sub(r"[\s\u3000\u200b\ufeff]+", "", s or "").lower()


def name_matches(name, target):
    """会话名和配置里写的名字是不是同一个人。

    只做「相等/去空白后相等」，不做包含匹配：配置里写「牛」时，
    「牛马一家亲」这种群也会被包含匹配命中，那是对着群刷屏。
    """
    if not name or not target:
        return False
    if name == target:
        return True
    return _norm_name(name) == _norm_name(target)

_env_prompt = os.environ.get("AUTO_REPLY_SYSTEM_PROMPT", "").strip()
SYSTEM_PROMPT = (
    _env_prompt
    or prompt_from_lines(CFG)
    or setting("system_prompt", "AUTO_REPLY_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
)

# 模型接口相关全部改为每次请求前重读（llm_settings()），和 system_prompt 一样
# 热更新：换模型、换服务商、填 key、调参数，改完 config 保存就生效，不用重启脚本。
DEFAULT_LLM = {
    "api_key": "",
    "base_url": "https://api.xiaomimimo.com/v1",
    "model": "mimo-v2.6-pro",
    "max_tokens": 1000,
    "temperature": 1.0,
    # LLM 单次请求超时。聊天场景关思考后约 3 秒出结果；45 秒足够覆盖慢模型，
    # 也不至于让一次卡死的请求把派发流程拖住太久。
    "llm_timeout": 45.0,
    # 推理模型会先把 token 预算用于思考，聊天场景需要关掉或压低。
    # 实测（deepseek-v4-pro）："none" 可完全关闭思考，耗时从 31s 降到 3s。
    # 换到的服务若不认这个参数，删掉 config 里的 reasoning_effort 即可不发送。
    "reasoning_effort": "",
}


def llm_settings():
    """当前生效的模型接口配置（每次调用重读 config + 环境变量）。"""
    cfg = load_config()
    s = dict(DEFAULT_LLM)
    s["api_key"] = setting("api_key", "AUTO_REPLY_API_KEY", "", cfg=cfg)
    s["base_url"] = setting("base_url", "AUTO_REPLY_BASE_URL", s["base_url"], cfg=cfg).rstrip("/")
    s["model"] = setting("model", "AUTO_REPLY_MODEL", s["model"], cfg=cfg)
    s["max_tokens"] = setting_int("max_tokens", "AUTO_REPLY_MAX_TOKENS", s["max_tokens"], cfg=cfg)
    s["temperature"] = setting_float("temperature", "AUTO_REPLY_TEMPERATURE", s["temperature"], cfg=cfg)
    s["llm_timeout"] = setting_float("llm_timeout", "AUTO_REPLY_LLM_TIMEOUT", s["llm_timeout"], cfg=cfg)
    s["reasoning_effort"] = setting("reasoning_effort", "AUTO_REPLY_REASONING_EFFORT", "", cfg=cfg)
    return s

# —— 节流 ——
# 轮询间隔：主循环多久看一次会话列表。原来写死 1.0 秒，现在放配置里 —— 它直接决定
# "发现新消息"最快能多快，代价是 CPU 成比例上升。
POLL_SECONDS = setting_float("poll_seconds", "AUTO_REPLY_POLL_SECONDS", 0.5)
# 拟人延迟：模型回好了之后先随机等这么久再发出去，免得秒回得像机器人。
# 原来是写死的 (1.5, 4.0)，现在也放配置里。
_delay_lo = setting_float("reply_delay_min", "AUTO_REPLY_DELAY_MIN", 0.5)
_delay_hi = setting_float("reply_delay_max", "AUTO_REPLY_DELAY_MAX", 1.5)
REPLY_DELAY = (min(_delay_lo, _delay_hi), max(_delay_lo, _delay_hi))
# 同一个会话两次回复的最小间隔。群聊一刷屏就会连续甩出一串回复，而且每次都要抢一次前台。
MIN_REPLY_INTERVAL = setting_float("min_reply_interval", "AUTO_REPLY_MIN_REPLY_INTERVAL", 5.0)
# 全局限速：GLOBAL_WINDOW 秒内最多回 GLOBAL_REPLY_LIMIT 条（防止异常时失控刷屏）
GLOBAL_REPLY_LIMIT = setting_int("global_reply_limit", "AUTO_REPLY_GLOBAL_REPLY_LIMIT", 12)
GLOBAL_WINDOW = setting_float("global_window", "AUTO_REPLY_GLOBAL_WINDOW", 300.0)
# 免打扰时段，形如 "23:00-08:00"（支持跨零点）。留空表示不启用。
QUIET_HOURS = setting("quiet_hours", "AUTO_REPLY_QUIET_HOURS", "")

# listen_all 模式下自动跳过「疑似群聊」。实测（QQ NT，本机 2026-09）：
# 群消息预览带「发送者: 内容」前缀，单聊预览是纯消息文本；QQ空间动态是
# 「xx发表了说说」。凭这个把群聊拦在外面，防止机器人对着群刷人设。
# 误伤面：对方消息本身以「短词: 」开头（如 "PS: 明天带伞"）会被静默跳过
# 一条，之后的预览不像群聊会自动恢复监听。介意就设 false。
SKIP_GROUP_LIKE = setting_bool("skip_group_like", True)
GROUP_PREVIEW_RE = re.compile(r"^[^:：\n]{1,20}[:：](?!//)")
FEED_PREVIEW_RE = re.compile(r"发表了(说说|动态)|加入了群聊")

# —— 表情包 ——
# 开关。关掉之后模型就算写了标记也不会发图。
IMAGE_ENABLED = setting_bool("image_enabled", True)
# 图片目录，相对脚本目录；下面按"类别"分子目录，目录名就是模型能填的类别
IMAGE_DIR = setting("image_dir", "AUTO_REPLY_IMAGE_DIR", "images")
IMAGE_DIR_ABS = IMAGE_DIR if os.path.isabs(IMAGE_DIR) else os.path.join(HERE, IMAGE_DIR)
# 同一个会话两次发表情包的最小间隔，防止它每句话都甩一张
IMAGE_MIN_INTERVAL = setting_float("image_min_interval", "AUTO_REPLY_IMAGE_MIN_INTERVAL", 120.0)
# 超过这个边长会被缩放后再放进剪贴板，太大的图 QQ 会压得很难看
IMAGE_MAX_SIDE = setting_int("image_max_side", "AUTO_REPLY_IMAGE_MAX_SIDE", 640)


def gif_settings():
    """每次发图前重读 GIF 相关配置，改完不用重启脚本。"""
    cfg = load_config()
    mode = str(cfg.get("gif_mode") or "auto").strip().lower()
    if mode not in ("auto", "frame", "file"):
        mode = "auto"
    frame = str(cfg.get("gif_frame") or "first").strip().lower()
    if frame not in ("first", "mid"):
        frame = "first"
    try:
        mb = cfg.get("gif_max_bytes")
        mb = int(mb) if not isinstance(mb, bool) and mb is not None else 2 * 1024 * 1024
    except Exception:
        mb = 2 * 1024 * 1024
    return mode, frame, max(0, mb)
# QQ 会话预览里图片的显示形态。发出去的图会变成这种预览，
# 跟已发文字对不上，如果不识别就会被当成新消息 -> 自己回自己。
IMAGE_PREVIEWS = ("[图片]", "[动画表情]", "[表情]", "[照片]", "[视频]", "[文件]")

# —— 识图 ——
# 图片消息在会话预览里只剩「[图片]」这种占位符，模型看不到内容就只能瞎编（日志里
# 出现过"这啥呀？乌漆嘛黑的，你拍得也太随便了吧"这种凭空描述）。
# 两个信息来源，按清晰度排：
# 1. QQ 收图缓存里的原图（Image\C2C\Image2）：对方发来的完整文件，最清楚；
# 2. 聊天窗口截图（capture_messages）：原图找不到时的兜底。注意截图里表情包只有
#    缩略图大小，还混着气泡、头像、输入框，识别率明显不如原图。
# 两个代价要清楚：图会被上传到模型服务商；图片按像素计费（实测 image_tokens 约等于
# 宽×高÷1024）。VISION_MAX_SIDE 只影响截图这条路；原图另由 vision_original_max_side
# 控制。设 0 表示不缩放 —— 实测裁切和缩放都会明显拉低识别率。
VISION_ENABLED = setting_bool("vision_enabled", False)
VISION_MAX_SIDE = setting_int("vision_max_side", "AUTO_REPLY_VISION_MAX_SIDE", 0)
VISION_USE_ORIGINAL = setting_bool("vision_use_original", True)
VISION_ORIGINAL_MAX_SIDE = setting_int("vision_original_max_side", "AUTO_REPLY_VISION_ORIG_MAX_SIDE", 1600)
# QQ 消息数据目录（含 Image\C2C\...）。留空自动发现；自动发现找不到时手动填，
# 形如 D:\qq聊天消息\Tencent Files（下面是各个 QQ 号的目录）。
QQ_DATA_DIR = setting("qq_data_dir", "AUTO_REPLY_QQ_DATA_DIR", "")
# 把发给模型的图另存一份到临时目录，方便自己核对识别不准是图的问题还是模型的问题
VISION_SAVE_DEBUG = setting_bool("vision_save_debug", True)


# —— 人工代打：你在聊天窗口里自己打字时 ——
# 会话预览读不出消息方向：自己发的和对方发的在预览里长得一模一样（群里才带
# 「发送者:」前缀，单聊没有）。所以你手动在聊天窗口里代替 AI 说话时，脚本会
# 把你说的当成对方的新消息，然后自己回自己 —— 表现为自言自语。
# 唯一能读到的「我这边」的信号是**聊天窗口输入框里的内容**（草稿）。据此做两件事：
#   1. 草稿发出之后预览变成它 -> 认领成「我方人工发出的消息」，不触发回复；
#   2. 输入框里有你打的字  -> 这段时间算人工接管，暂停该会话的自动回复。
MANUAL_TYPING_ENABLED = setting_bool("manual_typing_enabled", True)
# 检测到人工打字后暂停回复的秒数（停止打字后再过这段时间自动恢复）
MANUAL_TYPING_PAUSE = setting_float("manual_typing_pause", "AUTO_REPLY_MANUAL_PAUSE", 90.0)
# 草稿发出之后，多久之内可以认领成「我方消息」
MANUAL_DRAFT_WINDOW = setting_float("manual_draft_window", "AUTO_REPLY_MANUAL_WINDOW", 25.0)
# 认领成功时清掉该会话积压的待回复（你替它回过了，AI 就不必再回一次）
MANUAL_CLEARS_QUEUE = setting_bool("manual_clears_queue", True)
# 完全手动模式：只听不回（想自己全程掌控时打开，比杀进程省事）
MANUAL_MODE = setting_bool("manual_mode", False)

# 上面这些每轮重新读一次（见 refresh_manual_settings），改完配置立刻生效，
# 不用重启脚本 —— 「我正聊着呢别插嘴」这种需求等不起重启。
_MANUAL = {"enabled": MANUAL_TYPING_ENABLED, "pause": MANUAL_TYPING_PAUSE,
           "window": MANUAL_DRAFT_WINDOW, "clears": MANUAL_CLEARS_QUEUE,
           "mode": MANUAL_MODE}


def refresh_manual_settings():
    """重新读一遍人工代打相关配置，返回更新后的设置字典。"""
    cfg = load_config()

    def _b(key, default):
        v = cfg.get(key)
        return v if isinstance(v, bool) else default

    def _f(key, default):
        try:
            v = cfg.get(key)
            return float(v) if v not in (None, "") else default
        except Exception:
            return default

    _MANUAL["enabled"] = _b("manual_typing_enabled", MANUAL_TYPING_ENABLED)
    _MANUAL["pause"] = max(0.0, _f("manual_typing_pause", MANUAL_TYPING_PAUSE))
    _MANUAL["window"] = max(0.0, _f("manual_draft_window", MANUAL_DRAFT_WINDOW))
    _MANUAL["clears"] = _b("manual_clears_queue", MANUAL_CLEARS_QUEUE)
    _MANUAL["mode"] = _b("manual_mode", MANUAL_MODE)
    return _MANUAL


def image_preview(text):
    """这条预览是不是图片类消息（内容读不到，预览里只剩占位符）。"""
    t = text or ""
    return any(p in t for p in IMAGE_PREVIEWS)


# —— 识图：从 QQ 收图缓存里拿原图 ——
# 实测（2026-09-24，QQ NT）：收到的图片会以原分辨率落在
#   <QQ消息目录>\<QQ号>\Image\C2C\Image2\*.jpg（旧消息在 Image\C2C\ 根下）
# 自己发出的图也会写进同一个目录，所以这里按「消息到达时刻之后的新文件 + 已消费
# 记录」来筛，只取最新的一张。
_VISION_DATA_DIRS = []
_VISION_DATA_SCANNED_AT = 0.0
_IMG_CACHE_MAX_AGE = 180.0        # 只认 N 秒内落盘的图，太老的不敢认领


def _qq_data_dirs():
    r"""QQ NT 消息数据根目录（每账号一个 <根>\<QQ号>）。结果缓存 10 分钟。"""
    global _VISION_DATA_DIRS, _VISION_DATA_SCANNED_AT
    if QQ_DATA_DIR:
        p = os.path.abspath(os.path.expanduser(QQ_DATA_DIR))
        return [p] if os.path.isdir(p) else []
    now = time.time()
    if _VISION_DATA_DIRS and now - _VISION_DATA_SCANNED_AT < 600:
        return _VISION_DATA_DIRS
    found = []
    bases = [os.path.expanduser(r"~\Documents\Tencent Files")]
    for letter in "CDEFGH":
        drive = letter + ":\\"
        if not os.path.isdir(drive):
            continue
        try:
            for top in os.listdir(drive):
                low = top.lower()
                if "tencent files" in low or ("tencent" in low or "qq" in low):
                    bases.append(os.path.join(drive, top))
        except OSError:
            continue
    for base in bases:
        if not os.path.isdir(base):
            continue
        for root, dirs, _f in os.walk(base):
            depth = root[len(base):].count(os.sep)
            if os.path.basename(root).lower() == "tencent files":
                for d in dirs:
                    if d.isdigit():
                        found.append(os.path.join(root, d))
                dirs[:] = []
                continue
            if depth >= 2:
                dirs[:] = []
    _VISION_DATA_DIRS = found
    _VISION_DATA_SCANNED_AT = now
    return found


def _image_cache_dirs():
    """所有账号的收图缓存目录。"""
    out = []
    for acct in _qq_data_dirs():
        for sub in (r"Image\C2C\Image2", r"Image\C2C"):
            p = os.path.join(acct, *sub.split("\\"))
            if os.path.isdir(p):
                out.append(p)
    return out


# 全局（跨会话）记录「这个文件已经被拿去用过了」。放模块级而不是每个会话自己的状态里：
# 缓存目录是所有会话共用的，按会话记账的话，两个会话几乎同时来图时会各自认领同一张，
# A 的回复配上 B 的图。
_USED_IMAGES = {}
_USED_IMAGES_MAX = 60
# 上次扫描的时刻，当水位线用。
_SCAN_WATERMARK = {"at": 0.0}


def find_cached_original(now, max_age=_IMG_CACHE_MAX_AGE):
    """找缓存里最新、还没被任何会话用过的图片。返回 (路径, 说明)；没有就 (None, 原因)。

    认不出图片属于哪个会话，只能按时间猜。两条防错配的规则：
    1. 只看「上次扫描之后落盘」的文件（水位线），而不是把本轮看到的所有候选都标成
       已消费 —— 后者会让对方连发两张图时第二张找不到原图（第一轮就把它一起吞了）；
    2. 用过的文件全局记账，不让两个会话认领同一张。
    猜不准的时候宁可不认领，调用方会退回聊天窗口截图那条路。
    """
    dirs = _image_cache_dirs()
    if not dirs:
        return None, "没找到 QQ 收图缓存目录（qq_data_dir 可能需要手动填）"
    cutoff = max(now - max_age, _SCAN_WATERMARK["at"])
    candidates = []
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            if not f.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
                continue
            if f in _USED_IMAGES:
                continue
            fp = os.path.join(d, f)
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            if mt < cutoff or mt > now + 5:
                continue
            candidates.append((mt, fp, f))
    if not candidates:
        return None, "缓存 %.0f 秒内没有新图片" % max_age
    candidates.sort(reverse=True)
    mt, fp, f = candidates[0]
    _USED_IMAGES[f] = mt
    _SCAN_WATERMARK["at"] = now          # 只有真认领到了才推进水位线
    while len(_USED_IMAGES) > _USED_IMAGES_MAX:
        del _USED_IMAGES[min(_USED_IMAGES, key=_USED_IMAGES.get)]
    return fp, "%s（%.0f 秒前落盘）" % (os.path.basename(fp), now - mt)


def load_original(path, max_side):
    """读缓存原图成 PIL 图（GIF 取第一帧），超过 max_side 先缩。"""
    from PIL import Image as PILImage
    im = PILImage.open(path)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    im = im.convert("RGB")
    if max_side and max(im.size) > max_side:
        scale = float(max_side) / float(max(im.size))
        im = im.resize((max(1, int(im.size[0] * scale)),
                        max(1, int(im.size[1] * scale))))
    return im

# 收到新消息后先等这么久再回：对方一句话拆成三段发时只回一次，
# 也顺便把连续刷屏合并成一条。
REPLY_DEBOUNCE_SECONDS = setting_float("reply_debounce", "AUTO_REPLY_DEBOUNCE", 1.2)
# 排队消息的最长等待时间，超时丢弃。免得半夜问的话等到早上才回一句。
PENDING_MAX_AGE = setting_float("pending_max_age", "AUTO_REPLY_PENDING_MAX_AGE", 600.0)
# 因为窗口打不开等原因发送失败时，最多重排几次
MAX_PENDING_RETRIES = 2
MAX_PENDING_MESSAGES = 6
# 同时在后台生成回复的会话数上限。LLM 在线程里跑之后，理论上可以多个会话
# 并行生成，但生成完终究要逐个抢前台发送，攒太多没有意义还烧 token。
MAX_IN_FLIGHT = 3


def refresh_prompt():
    """每次回复前重读 system_prompt：改完提示词不必重启脚本。"""
    global SYSTEM_PROMPT
    env_v = os.environ.get("AUTO_REPLY_SYSTEM_PROMPT", "").strip()
    if env_v:
        SYSTEM_PROMPT = env_v
        return
    cfg = load_config()
    v = prompt_from_lines(cfg)
    if v is None:
        s = cfg.get("system_prompt")
        v = s if isinstance(s, str) else ""
    if v.strip():
        SYSTEM_PROMPT = v.strip()


def should_listen(name):
    if not name:
        return False
    if any(name_matches(name, x) for x in _WATCH["ignore"]):
        return False
    if _WATCH["targets"]:
        return any(name_matches(name, x) for x in _WATCH["targets"])
    return bool(_WATCH["listen_all"])


def is_group_like(preview):
    """预览像不像群聊/系统会话。判据来自本机实测（QQ NT，2026-09）：
    群消息预览是「发送者: 内容」，QQ空间动态是「xx发表了说说」，
    单聊预览是纯消息文本、不带任何前缀。误伤面见 SKIP_GROUP_LIKE 的注释。"""
    p = (preview or "").strip()
    if not p:
        return False
    if FEED_PREVIEW_RE.search(p):
        return True
    return bool(GROUP_PREVIEW_RE.match(p))


# 模型想发表情包时，在回复末尾写的标记，例如 [表情:无语]
IMAGE_MARKER_RE = re.compile(r"[\[【]\s*表情\s*[:：]\s*([^\]】]+?)\s*[\]】]")

# 图片目录不会频繁变动，缓存 60 秒，免得每次回复都扫一遍磁盘
_IMG_CACHE = {"at": 0.0, "cats": {}}


def split_image_marker(text):
    """从模型回复里拆出发图标记。返回 (正文, 类别 或 None)。

    标记可能出现在中间或结尾，全部摘掉；只取第一个类别（一次最多一张）。
    """
    if not text:
        return "", None
    cats = IMAGE_MARKER_RE.findall(text)
    body = IMAGE_MARKER_RE.sub("", text)
    # 摘掉标记后可能留下空行，压一下
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, (cats[0].strip() if cats else None)


def image_categories():
    """扫描图片目录，返回 {类别名: [文件路径, ...]}。"""
    now = time.time()
    if now - _IMG_CACHE["at"] < 60.0 and _IMG_CACHE["cats"]:
        return _IMG_CACHE["cats"]
    cats = {}
    try:
        if os.path.isdir(IMAGE_DIR_ABS):
            for name in sorted(os.listdir(IMAGE_DIR_ABS)):
                d = os.path.join(IMAGE_DIR_ABS, name)
                if not os.path.isdir(d):
                    continue
                files = [os.path.join(d, f) for f in sorted(os.listdir(d))
                         if os.path.splitext(f)[1].lower() in qq.IMAGE_EXTS]
                if files:
                    cats[name] = files
    except Exception:
        pass
    _IMG_CACHE["at"] = now
    _IMG_CACHE["cats"] = cats
    return cats


def files_sig(files):
    """目录内容指纹：加图、删图、改名都会变，用来判断洗牌袋要不要重洗。"""
    return "\n".join(files)


def pick_image(cat, st):
    """按类别随机挑一张，用「洗牌袋」保证一轮之内不重复。没有就返回 None。

    纯 random.choice 在图片少的时候会来回横跳（A B A B 或 A C A C），
    这里改成：把整个目录洗一遍顺序，一张一张发完，再重新洗一轮 ——
    既随机，又不会连着重复同一张。

    每个类别各有一个袋子，存在 st 里，所以不同会话互不干扰；
    目录内容变了（加图/删图）会自动重洗，新图立刻能进轮换。

    挑中的图会立刻记进 st["last_image"] —— 不记的话同一张会连着被挑中，
    而调用方不一定每次都回写（例如发送失败的路径）。
    """
    files = image_categories().get(cat)
    if not files:
        return None

    bags = st.get("image_bags")
    if not isinstance(bags, dict):
        bags = {}
        st["image_bags"] = bags

    sig = files_sig(files)
    bag = bags.get(cat)
    if (not isinstance(bag, dict) or bag.get("sig") != sig
            or not bag.get("queue")):
        queue = list(files)
        random.shuffle(queue)
        # 新一轮的第一张如果正好是刚发过的那张，挪到队尾，避免背靠背重复
        if len(queue) > 1 and queue[0] == st.get("last_image"):
            queue.append(queue.pop(0))
        bag = {"sig": sig, "queue": queue}

    chosen = bag["queue"].pop(0)
    bags[cat] = bag
    st["last_image"] = chosen
    return chosen


def norm_text(s):
    """归一化：去掉所有空白（含换行、连续空格）。

    QQ 的会话预览会把换行吃掉 —— 我们发出去的是「…。  \\n我…」，
    预览读回来却是「…。  我…」。不归一化的话前缀比对正好卡在 \\n 上失败，
    机器人就把自己刚发的话当成对方的新消息，开始无限复读。
    """
    return re.sub(r"\s+", "", s or "")


def register_expect_echo(st, kind, text=""):
    """发送成功后登记一条「预期回显」。

    QQ 读不出消息方向，只能靠内容比对认领自己的回显。以前是「90 秒内任何
    相似预览都算自己的」，对方在我们发完消息后顺势回一个表情包（预览也是
    [图片]）、或引用我们的话开头，就会被误吞。改成消费制：发的时候就知道
    预览大概长什么样，登记下来，见到一条匹配的预览消费一次，之后不再认领。
    真正失控（认领逻辑整体失效）由循环熔断兜底，它不依赖内容比对。
    """
    exp = st.setdefault("expect_echo", [])
    exp.append({"kind": kind, "text": text or "", "at": time.time()})
    del exp[:-20]


def consume_own_echo(st, preview):
    """这条预览是不是我们自己刚发出去的。是 -> 消费掉对应预期并返回 True。"""
    now = time.time()
    p = (preview or "").strip()
    if not p:
        return False
    exp = [e for e in (st.get("expect_echo") or [])
           if now - e["at"] < SELF_ECHO_WINDOW]
    st["expect_echo"] = exp
    if not exp:
        return False
    np = norm_text(p)
    if not np:
        return False
    for i, e in enumerate(exp):
        # 自己发的图：预览会变成「[图片]」之类，跟文字对不上
        if e["kind"] == "image":
            if p in IMAGE_PREVIEWS:
                del exp[i]
                return True
            continue
        nt = norm_text(e["text"])
        if not nt:
            continue
        # 群里的预览带「发送者: 」前缀，自己发的消息也带。日志实测：在群里发出
        # 「有屁快放。」后预览是「纯爱战士:有屁快放。」。不剥掉前缀的话，5 字的
        # 短消息凑不满 6 字，三条规则全不成立，机器人会把自己的话当成对方的新
        # 消息回，一路自问自答到熔断。（长消息靠子串规则勉强能认出来，所以症状
        # 是"短的自己回自己、长的不会"。）
        for cand in echo_candidates(np):
            if _echo_match(cand, nt):
                del exp[i]
                return True
    return False


def echo_candidates(np):
    """回显比对的候选文本：原样，以及剥掉群预览「发送者: 」前缀的版本。"""
    out = [np]
    if GROUP_PREVIEW_RE.match(np):
        stripped = GROUP_PREVIEW_RE.sub("", np, count=1).strip()
        if stripped and stripped != np:
            out.append(stripped)
    return out


def _echo_match(np, nt):
    """某条预览 np 是不是我们刚发出的文本 nt 的回显。"""
    if np == nt:
        return True
    # 预览可能被截断，取公共前缀比（归一化后 12 字够了）
    n = min(len(np), len(nt), 12)
    if n >= 6 and np[:n] == nt[:n]:
        return True
    # 子串：预览只截了一段，或反过来预览里夹了别的东西
    if len(nt) >= 6 and nt in np:
        return True
    if len(np) >= 6 and np in nt:
        return True
    return False


# —— 人工代打：认领「用户手动发出去的消息」 ——
# 上面的 echo 认领只认得机器人自己发的（发出时登记过）。用户手工键入的消息没有
# 登记，而会话预览又不带方向，于是被当成对方的新消息 —— 这就是自言自语的开端。
# 输入法 indirectly 留了痕迹：发出之前，那串字必然先在输入框里出现过。

def note_manual_draft(st, text, now=None):
    """记下一份「人正在输入框里写的内容」。同一份内容只更新时刻。"""
    now = now if now is not None else time.time()
    nt = norm_text(text)
    if not nt:
        return
    drafts = st.setdefault("manual_drafts", [])
    for d in drafts:
        if d["norm"] == nt:
            d["at"] = now
            return
    drafts.append({"text": text, "norm": nt, "at": now})
    del drafts[:-10]


def claim_manual_send(st, preview, now=None):
    """这条预览是不是用户手动键入、从我们这个号发出去的？是则消费掉草稿并返回 True。

    容错取舍：宁可漏认（多发一句），不可误认（对方的话被吞掉，表现为不回复）。
    所以要求归一化后相等，或一方包含另一方且有足够长度。
    """
    now = now if now is not None else time.time()
    np = norm_text(preview)
    if not np:
        return False
    for d in st.get("manual_drafts", []):
        if now - d["at"] > _MANUAL["window"]:
            continue
        nd = d["norm"]
        if not nd:
            continue
        hit = (np == nd
               or (len(nd) >= 4 and nd in np)
               or (len(np) >= 4 and np in nd))
        if hit:
            d["at"] = 0.0                       # 一份草稿只认领一次
            return True
    return False


def scan_manual_typing(state, now=None):
    """每轮扫一次：哪些会话的聊天窗口里正有人（用户）在打字。

    只读已打开的聊天窗口，不主动开窗口、不碰焦点。按当前显示的会话名把草稿
    归到对应会话上。做两件事：登记草稿（供 claim_manual_send 用）和给「人工
    接管」计时（供 flush_pending 推迟发送用）。
    """
    now = now if now is not None else time.time()

    # 打字停了（或聊天窗口关了）-> 到点自动恢复自动回复。这一段只看内存，放在最前面：
    # 既不依赖开关（关掉检测也得能把接管状态解开），也不受下面任何短路影响。
    for key, st in state.items():
        if st.get("manual_typing") and now > st.get("manual_until", 0.0):
            st["manual_typing"] = False
            log("[%s] 人工接管结束，恢复自动回复" % (key[1] if isinstance(key, tuple) else key,))

    # 刷新必须放在所有 return 之前：manual_mode 的热更新也走这个字典，而配置里
    # 那条说明写的是「立刻生效」。以前是先判 MANUAL_TYPING_ENABLED 再刷新，于是把
    # manual_typing_enabled 关掉之后，manual_mode 改配置就不生效了。
    ms = refresh_manual_settings()
    if not ms["enabled"]:
        return

    try:
        windows = qq.chat_windows(with_draft=True)
    except Exception:
        return
    for who, hwnd, txt in windows:
        # who 为空 = 输入框里没字（那种窗口压根没去解析会话名），跳过
        if not who:
            continue
        st = _state_by_conversation(state, who)
        if st is None:
            continue
        # 机器人自己正在发送的过程中不要来读：那会儿输入框里是刚塞进去的正文，
        # 会被误判成人工草稿（发送在主线程串行执行，此时压根不该有别的话在输入）。
        if st.get("sending"):
            continue
        note_manual_draft(st, txt, now)
        until = now + max(0.0, ms["pause"])
        if not st.get("manual_typing"):
            log("[%s] 检测到你在手动打字（%r），%.0f 秒内不插嘴"
                % (who, txt[:30], ms["pause"]))
            # 你都上手了，之前积压的待回复就不必再回 —— 否则 90 秒后机器人
            # 会冲出去补一句，正好和你已经说过的话重复。
            if ms["clears"] and st.get("pending"):
                log("[%s] 清掉 %d 条积压待回复（你开始自己敲字了）"
                    % (who, len(st["pending"])))
                st["pending"] = []
                st.pop("pending_at", None)
        st["manual_typing"] = True
        st["manual_until"] = until


def _state_by_conversation(state, who):
    """按会话名从 state 里取状态（key 是 (主窗口hwnd, 会话名)）。"""
    if not who:
        return None
    for key, st in state.items():
        if key[1] == who:
            return st
    return None


def too_many_recent_replies(st, now=None, name=""):
    """循环熔断：回复次数远多于收到的真消息时，判定为自我循环。

    只看次数不够准 —— 对方连珠炮式发消息时，正常回复也会很密。
    真正的自我循环特征是「对方压根没说话」，会话里滚过去的都是我们自己发的。
    所以判据是回复数与「非回显新消息」数的对比，完全不依赖内容比对：
    将来 QQ 改预览格式、模型输出特殊字符导致回显识别失效时，这层仍然兜得住。

    阈值放得很宽松（3 分钟回 15 条、且期间收到不超过 2 条真消息），
    正常聊天基本碰不到。
    """
    now = now if now is not None else time.time()
    if now < st.get("loop_break_until", 0.0):
        return True
    st["reply_log"] = [t for t in st.get("reply_log", []) if now - t < LOOP_WINDOW]
    st["incoming_log"] = [t for t in st.get("incoming_log", []) if now - t < LOOP_WINDOW]
    sent = len(st["reply_log"])
    got = len(st["incoming_log"])
    if sent >= LOOP_MAX_REPLIES and got <= LOOP_MAX_REAL_INCOMING:
        st["loop_break_until"] = now + LOOP_BREAK_COOLDOWN
        st["reply_log"] = []
        st["incoming_log"] = []
        log("[%s] 警告：%.0f 秒内回复 %d 次，但只收到 %d 条对方消息，疑似自我循环，"
            "该会话冷却 %.0f 秒"
            % (name or "?", LOOP_WINDOW, sent, got, LOOP_BREAK_COOLDOWN))
        return True
    return False


def call_llm(history, incoming, image=None):
    llm = llm_settings()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-MAX_HISTORY_TURNS * 2:])
    if image is not None:
        # 带一张聊天窗口截图。图片是按像素计费的，所以抓的时候已经缩过一次。
        try:
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            log("截图编码失败，这次不带图：%r" % (e,))
            b64 = ""
        if b64:
            messages.append({"role": "user", "content": [
                {"type": "text", "text": incoming},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]})
        else:
            messages.append({"role": "user", "content": incoming})
    else:
        messages.append({"role": "user", "content": incoming})
    body = {
        "model": llm["model"],
        "messages": messages,
        "temperature": llm["temperature"],
        "max_tokens": llm["max_tokens"],
    }
    if llm["reasoning_effort"]:
        body["reasoning_effort"] = llm["reasoning_effort"]
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        llm["base_url"] + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + llm["api_key"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=llm["llm_timeout"]) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choice = data["choices"][0]
    content = (choice.get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    rtok = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if image is not None:
        # 图片 token 单独记一行方便对账：它和文本 token 是分开算的
        log("识图请求完成：prompt=%s（其中图片 %s）completion=%s"
            % (usage.get("prompt_tokens"),
               (usage.get("prompt_tokens_details") or {}).get("image_tokens"),
               usage.get("completion_tokens")))
    if not content.strip():
        log("模型返回空正文（finish_reason=%s, reasoning_tokens=%s, max_tokens=%s）"
            % (choice.get("finish_reason"), rtok, llm["max_tokens"]))
        return None
    return content.strip()


def make_reply(history, incoming, image=None):
    refresh_prompt()
    if not llm_settings()["api_key"]:
        # 配置解析失败时 llm_settings() 读不到 key，但不能因此变成"回声模式"：
        # 那会对着真人回「收到：xxx」。修好配置之前宁可不回。
        if _CFG_STATE["broken"]:
            log("配置读取失败且没有可用的 api_key，放弃这条回复（不退回回声模式）")
            return None
        return "收到：" + incoming[:30]
    try:
        return call_llm(history, incoming, image)
    except urllib.error.HTTPError as e:
        # 只有状态码看不出问题所在，余额不足/限流的具体原因都在响应体里
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            detail = ""
        log("LLM HTTP %s：%s" % (e.code, detail))
        return None
    except Exception as e:
        log("LLM 调用失败：%r" % (e,))
        return None


_mutex_handle = None
_last_state = {}

# 运行期统计，主要给 status.json 用：pythonw 没有界面，进程还活着与否、有没有在干活、
# 上次出错是什么，外面通通看不见，只能靠这个文件。
RUNTIME = {
    "pid": os.getpid(),
    "started_at": 0.0,
    "last_incoming_at": 0.0,
    "last_incoming_from": "",
    "last_reply_at": 0.0,
    "last_reply_to": "",
    "replies_today": 0,
    "replies_total": 0,
    "images_total": 0,
    "errors_total": 0,
    "last_error": "",
    "_day": "",
}

# 状态文件内容去重用：和上次写入相比没变就跳过磁盘写
_last_status_json = {"raw": ""}

# 当前在处理的 QQ 实例信息（版本、形态、会话数），给 status / 日志用。
_ENV_INFO = {"flavor": "", "version": "", "instances": []}


def _fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def _update_env_info(sessions):
    """记录当前处理的实例信息（版本/形态/会话数），写进 status。"""
    try:
        _ENV_INFO["instances"] = [
            {"pid": wpid, "main_hwnd": whwnd, "conversations": len(convs),
             "flavor": qq.qq_flavor(wpid), "version": qq.process_version(wpid)}
            for whwnd, wpid, convs in sessions]
        if sessions:
            _ENV_INFO["flavor"] = qq.qq_flavor(sessions[0][1])
            _ENV_INFO["version"] = qq.process_version(sessions[0][1])
    except Exception:
        pass


def write_status(state, extra=None):
    """把当前状态写成 json，方便随时 cat 一眼确认它还活着。

    内容没变化就不重写 —— 以前主循环每秒都调一次，状态文件一天被
    白写八万多遍。updated_at 不参与比较，只在真正落盘时刷新。
    """
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    if RUNTIME["_day"] != today:
        RUNTIME["_day"] = today
        RUNTIME["replies_today"] = 0
    info = {
        "pid": RUNTIME["pid"],
        "updated_at": _fmt(now),
        "started_at": _fmt(RUNTIME["started_at"]),
        "running_minutes": int((now - RUNTIME["started_at"]) / 60) if RUNTIME["started_at"] else 0,
        "listen_mode": "全部会话(listen_all)" if _WATCH["listen_all"] and not _WATCH["targets"] else "白名单",
        "targets": _WATCH["targets"],
        "ignore": _WATCH["ignore"],
        "account_markers": _WATCH["markers"],
        "strict_account": _WATCH["strict"],
        "send_key": _WATCH["send_key"],
        "qq_flavor": _ENV_INFO.get("flavor", ""),
        "qq_version": _ENV_INFO.get("version", ""),
        "instances": _ENV_INFO.get("instances", []),
        "conversations": len(state),
        "in_flight_replies": in_flight_replies(state),
        "replies_today": RUNTIME["replies_today"],
        "replies_total": RUNTIME["replies_total"],
        "images_total": RUNTIME["images_total"],
        "image_categories": sorted(image_categories().keys()),
        "last_incoming_at": _fmt(RUNTIME["last_incoming_at"]),
        "last_incoming_from": RUNTIME["last_incoming_from"],
        "last_reply_at": _fmt(RUNTIME["last_reply_at"]),
        "last_reply_to": RUNTIME["last_reply_to"],
        "errors_total": RUNTIME["errors_total"],
        "last_error": RUNTIME["last_error"],
        "pending": sum(len(s.get("pending") or []) for s in (state or {}).values()),
        "log_path": LOG_PATH,
    }
    if extra:
        info.update(extra)
    stamp = info.pop("updated_at")
    raw = json.dumps(info, ensure_ascii=False, indent=2)
    if raw == _last_status_json["raw"]:
        return
    info["updated_at"] = stamp
    try:
        write_json_atomic(STATUS_PATH, info)
        _last_status_json["raw"] = raw
    except Exception:
        pass


class RateLimiter:
    """滑动窗口限速：window 秒内最多放行 max_events 次。"""

    def __init__(self, max_events, window):
        self.max_events = max_events
        self.window = window
        self.hits = []

    def peek(self, now=None):
        """只看不带走：能不能放行，但不占额度。

        allow() 一旦调用就吃掉一个名额，而真正发送可能还没开始
        （还要经历 debounce、开窗口等），所以用 peek 先探路，
        真正要发的时候才 allow()。
        """
        now = now if now is not None else time.time()
        recent = [t for t in self.hits if now - t < self.window]
        return len(recent) < self.max_events

    def allow(self, now=None):
        now = now if now is not None else time.time()
        self.hits = [t for t in self.hits if now - t < self.window]
        if len(self.hits) >= self.max_events:
            return False
        self.hits.append(now)
        return True


def in_quiet_hours(spec):
    """spec 形如 "23:00-08:00"，跨零点也算。格式不对时一律当作非免打扰。"""
    if not spec:
        return False
    try:
        a, b = [s.strip() for s in spec.split("-")]

        def to_min(s):
            h, m = s.split(":")
            return int(h) * 60 + int(m)

        now = datetime.datetime.now()
        cur = now.hour * 60 + now.minute
        start, end = to_min(a), to_min(b)
        if start <= end:
            return start <= cur < end
        return cur >= start or cur < end
    except Exception:
        return False


def acquire_single_instance():
    """拿会话级命名互斥体，防止同一个帐号的轮询进程开成两个。

    开机自启会拉一个，手滑再双击桌面快捷方式又是一个，两个进程读的是同一份会话列表，
    同一条消息会被回两次。拿不到锁返回 False（调用方直接退出即可）；
    万一 ctypes 这套走不通，宁可不拦也不挡正常启动。
    """
    global _mutex_handle
    if _mutex_handle:
        # 同一进程里第二次调用：锁本来就在自己手上。再 CreateMutexW 会拿到
        # ERROR_ALREADY_EXISTS，被判成"已经有实例在运行"，于是崩溃自愈重试的
        # 第一次 main() 就退出、进程结束 —— MAX_RESTARTS 那套自愈从来没生效过。
        # （实测：同一进程连调两次，改之前第二次返回 False。）
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = kernel32.CreateMutexW(None, True, SINGLE_INSTANCE_MUTEX)
        if not h:
            return True
        if ctypes.get_last_error() == 183:              # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(h))
            return False
        _mutex_handle = ctypes.c_void_p(h)              # 持有到进程结束
        return True
    except Exception:
        return True


def prune_state(state, active_keys, now):
    """丢掉长时间没再见到的会话状态，返回清理数量。

    QQ 主窗口重启后 hwnd 是新值，旧 (hwnd, name) key 会永远留在 state 里；
    跑几天就是一堆僵尸条目，既占内存又让心跳里的会话数变得没参考价值。
    """
    dead = [k for k, st in state.items()
            if k not in active_keys and now - st.get("last_seen_at", 0.0) > STALE_STATE_SECONDS]
    for k in dead:
        del state[k]
    return len(dead)


# —— 对话历史持久化 ——
# 崩溃自愈重启后 state 全新，上下文丢失，对方会察觉「失忆」。
# 按联系人名把 history 落盘，重启后接着聊。（key 用名字而不是 hwnd：
# hwnd 每次重启都变，名字才稳定；多开靠 account_markers 保证不撞名。）
HISTORY_PATH = os.path.join(HERE, "auto_reply_history.json")
_HISTORY = {}


def load_history():
    global _HISTORY
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _HISTORY = {k: v for k, v in d.items() if isinstance(v, list)}
    except Exception:
        pass


def save_history(name, history):
    if not history:
        return
    _HISTORY[name] = history
    try:
        write_json_atomic(HISTORY_PATH, _HISTORY)
    except Exception:
        pass


def new_state(name, preview, whwnd, wpid, now):
    """新建一个会话的状态条目；历史从磁盘恢复。"""
    return {"seen": preview, "sent": None, "sent_at": 0.0,
            "history": list(_HISTORY.get(name, [])), "hwnd": whwnd, "pid": wpid,
            "last_seen_at": now}


def flush_pending(state, limiter, now=None):
    """把排队的消息派发出去（时机判断 + 开窗 + 交给后台线程生成回复）。

    时机由五个条件共同决定，任一不满足就留在队列里等下一轮：
      1. 已经沉默了 REPLY_DEBOUNCE_SECONDS（把连发的话攒成一条）
      2. 不在免打扰时段
      3. 距该会话上次回复过了 MIN_REPLY_INTERVAL
      4. 没触达全局限速
      5. 后台生成中的回复没超过 MAX_IN_FLIGHT
    条件都满足时先把聊天窗口开好 —— 窗口打不开就直接重排，一次模型调用
    都不烧（以前是先调 LLM 再开窗，开窗失败白烧 token，重试再烧一次）。
    窗口就绪后把消息从队列摘下来交给后台线程；真正的发送由
    collect_replies() 在模型返回后进行，对 QQ 的写入始终只在主线程串行。
    """
    now = now if now is not None else time.time()
    if _MANUAL["mode"]:
        # 完全手动模式：只听不回。想自己全程掌控时把 manual_mode 设 true 即可，
        # 不用去任务计划里停进程（那样连消息队列和历史都不会丢）。
        write_status(state)     # 手动模式下也要让 status 反映最新名单
        return
    for key, st in list(state.items()):
        pend = st.get("pending")
        if not pend:
            continue
        name = key[1] if isinstance(key, tuple) else str(key)

        # 积压太久：对方多半已经放弃对话了，硬回一句反而奇怪
        if st.get("pending_at", 0.0) and now - st["pending_at"] > PENDING_MAX_AGE:
            log("[%s] 积压的 %d 条消息等待超过 %.0f 分钟，丢弃（最后一条 %r）"
                % (name, len(pend), PENDING_MAX_AGE / 60.0, pend[-1]))
            st["pending"] = []
            st["pending_retries"] = 0
            continue

        if QUIET_HOURS and in_quiet_hours(QUIET_HOURS):
            continue
        # 人工接管：你正在这个会话里自己打字（或刚打完没多久），别插嘴
        if _MANUAL["enabled"] and now < st.get("manual_until", 0.0):
            continue
        # 熔断：本会话短时间内回太多次，宁可不回也不能刷屏
        if too_many_recent_replies(st, now, name):
            continue
        if now - st.get("pending_at", 0.0) < REPLY_DEBOUNCE_SECONDS:
            continue
        if now - st.get("last_reply_at", 0.0) < MIN_REPLY_INTERVAL:
            continue
        if not limiter.peek(now):
            continue
        if in_flight_replies(state) >= MAX_IN_FLIGHT:
            continue

        # 先把窗口开好（ensure_chat_window 一路兜底：已开的窗口（含最小化、
        # 含"X等N个会话"合并窗口）→ 合并窗口切 Tab → UIA Invoke 打开 →
        # 双击会话项打开）
        chwnd = qq.ensure_chat_window(name, pid=st.get("pid"), main_hwnd=st.get("hwnd"))
        if not chwnd and TARGET_UIN and name == TARGET:
            log("[%s] 打开失败，改用 tencent:// 协议" % name)
            try:
                os.startfile("tencent://Message/?Uin=" + TARGET_UIN)
            except Exception as e:
                log("协议打开失败：%r" % (e,))
            time.sleep(3.5)
            chwnd = qq.ensure_chat_window(name, pid=st.get("pid"), main_hwnd=st.get("hwnd"))
        if not chwnd:
            retries = st.get("pending_retries", 0) + 1
            st["pending_retries"] = retries
            if retries <= MAX_PENDING_RETRIES:
                st["pending_at"] = now          # 让 debounce 重新计时，天然退避
                log("[%s] 无法打开聊天窗口，稍后重试（第 %d 次）" % (name, retries))
                continue
            st["pending_retries"] = 0
            st["pending"] = []
            log("[%s] 重试 %d 次仍打不开聊天窗口，丢弃：%r" % (name, retries, pend[-1][:30]))
            continue

        # 派发：多条消息合成一条 user 消息（模型能同时看到上下文），
        # 从队列摘下来，后台线程里先等拟人延迟再调模型
        st["pending_retries"] = 0
        st["pending"] = []
        st["reply_incoming"] = "\n".join(pend)
        st["reply_pending_saved"] = pend
        # 图片消息：优先用 QQ 收图缓存里的原图（最清楚）；找不到再退回
        # 截聊天窗口消息列表。（capture_messages 会自己把窗口显示出来再收回）
        st.pop("reply_image", None)
        if VISION_ENABLED and image_preview(pend[-1] if pend else ""):
            shot, note = None, ""
            if VISION_USE_ORIGINAL:
                opath, onote = find_cached_original(now)
                if opath:
                    try:
                        shot = load_original(opath, VISION_ORIGINAL_MAX_SIDE)
                        note = "原图 %s %dx%d" % (onote, shot.size[0], shot.size[1])
                    except Exception as e:
                        log("读原图失败，退回截图：%r" % (e,))
            if shot is None:
                shot, note2 = qq.capture_messages(chwnd, max_side=VISION_MAX_SIDE)
                note = ("截图兜底 " if shot is not None else "截图失败：") + str(note2)
            if shot is not None:
                st["reply_image"] = shot
                if VISION_SAVE_DEBUG:
                    # 存一份到临时目录，识别不准时能自己看一眼是图的问题还是模型的问题
                    try:
                        d = os.path.join(tempfile.gettempdir(), "qq_vision_debug")
                        if not os.path.isdir(d):
                            os.makedirs(d)
                        fn = "%s_%s.jpg" % (re.sub(r"[^\w\u4e00-\u9fff-]", "_", name)[:24],
                                            time.strftime("%m%d_%H%M%S"))
                        shot.save(os.path.join(d, fn), "JPEG", quality=90)
                    except Exception as e:
                        log("识图调试图保存失败：%r" % (e,))
            log("[%s] 识图：%s" % (name, note))
        limiter.allow(now)
        log("[%s] 准备回复（后台生成中）：%r" % (name, st["reply_incoming"]))
        try:
            threading.Thread(target=_llm_worker, args=(st, st["reply_incoming"]),
                             daemon=True, name="llm-" + name).start()
        except Exception:
            # 线程起不来（极端情况）：把消息放回去，走重排路径
            st.pop("reply_incoming", None)
            st.pop("reply_pending_saved", None)
            st["pending"] = pend
            st["pending_at"] = now
            log("[%s] 生成线程启动失败，重新排队：%r" % (name, traceback.format_exc(limit=3)))
    write_status(state)


def in_flight_replies(state):
    """正在后台生成回复的会话数。"""
    return sum(1 for st in state.values()
               if "reply_incoming" in st and "reply_result" not in st)


def _llm_worker(st, incoming):
    """后台线程：只做 LLM 请求（纯网络 IO，不碰任何窗口/UIA）。

    结果写回 st["reply_result"]，主循环在 collect_replies() 里取走。
    异常也写成 None，保证主循环一定能收到结果、消息能走重排路径。
    """
    try:
        time.sleep(random.uniform(*REPLY_DELAY))
        shot = st.pop("reply_image", None)      # 截图是主线程放的，这里取走
        st["reply_result"] = make_reply(st["history"], incoming, shot)
    except Exception:
        log("回复生成线程异常：\n" + traceback.format_exc(limit=5))
        st["reply_result"] = None


def collect_replies(state):
    """收取后台线程就绪的回复并发送。

    窗口、前台、键盘这些对 QQ 的实际写入只在主线程做 —— 并发的 SendInput
    会互相抢前台，那才是真正危险的行为，线程化不改变这一点。
    发送失败时把消息放回队列重排，和以前的语义一致。
    """
    for key, st in list(state.items()):
        if "reply_result" not in st:
            continue
        reply = st.pop("reply_result")
        # name 必须在任何 log 之前取出：TIMEOUT_SIGNAL 分支以前先用后赋值，
        # 一触发就 UnboundLocalError，消息上下文已弹出却不发送也不重排，静默丢失。
        name = key[1] if isinstance(key, tuple) else str(key)
        # 人设里的 TIMEOUT_SIGNAL 表示「这轮不回话」。模型偶尔会把它当整个正文
        # 输出（13:39 那次原文发出去了，对方一头雾水）。这里直接消费掉：
        # 不发送、也不重排（重排会再调一次模型，白烧 token）。
        if reply and reply.strip() in ("TIMEOUT_SIGNAL", "[TIMEOUT_SIGNAL]", "【TIMEOUT_SIGNAL】"):
            st.pop("reply_incoming", None)
            st.pop("reply_pending_saved", None)
            log("[%s] 模型输出 TIMEOUT_SIGNAL（这轮不回话），跳过" % name)
            continue
        incoming = st.pop("reply_incoming", "")
        pend = st.pop("reply_pending_saved", [])
        ok = reply_send(name, incoming, st, reply)
        if ok:
            st["pending_retries"] = 0
        else:
            retries = st.get("pending_retries", 0) + 1
            st["pending_retries"] = retries
            if retries <= MAX_PENDING_RETRIES and pend:
                st["pending"] = pend
                st["pending_at"] = time.time()
                log("[%s] 本条未发出，重新排队（第 %d 次）" % (name, retries))
            else:
                st["pending_retries"] = 0
                log("[%s] 重试 %d 次仍未发出，丢弃：%r" % (name, retries, incoming[:30]))


def reply_send(name, incoming, st, reply):
    """发送半程：模型回复已就绪（失败时为 None）。校验、发送、记账。

    返回 True/False，False 时由调用方决定要不要把消息放回队列重试。
    """
    if not reply:
        log("[%s] 没能生成回复，跳过这次" % name)
        return False

    # 模型可以在回复末尾写 [表情:类别] 表示想配一张图。标记要摘掉，不能发给对方。
    body, cat = split_image_marker(reply)
    if cat:
        log("[%s] 模型想发表情包：%s（正文 %r）" % (name, cat, body))
    if not body and not cat:
        log("[%s] 摘掉标记后没内容了，跳过" % name)
        return False

    # 决定到底要不要真的发图：开关、目录里有没有这个类别、以及冷却
    image_path = None
    if cat and IMAGE_ENABLED:
        elapsed = time.time() - st.get("last_image_at", 0.0)
        if elapsed < IMAGE_MIN_INTERVAL:
            log("[%s] 距上次表情包仅 %.0f 秒（<%.0f），这次不发图"
                % (name, elapsed, IMAGE_MIN_INTERVAL))
        else:
            image_path = pick_image(cat, st)
            if not image_path:
                known = "、".join(sorted(image_categories().keys())) or "（图片目录还是空的）"
                log("[%s] 没有类别「%s」的图，可用类别：%s" % (name, cat, known))

    # 标记「正在发送」：这段时间里输入框里的内容是脚本塞的，不能被人工打字
    # 检测当成用户草稿（否则每一条 AI 回复都会被误认成手动代打）。
    st["sending"] = True
    try:
        chwnd = qq.ensure_chat_window(name, pid=st.get("pid"), main_hwnd=st.get("hwnd"))
        if not chwnd:
            log("[%s] 无法打开聊天窗口，跳过这次回复" % name)
            return False

        # 发送前的最后一道校验：窗口现在显示的必须是这个人（防发错人）
        shown = qq.active_conversation(chwnd)
        if shown != name:
            log("[%s] 窗口当前显示的是「%s」，不是它，放弃发送" % (name, shown))
            return False
    finally:
        st["sending"] = False

    sent_text = False
    if body:
        ok, prev_fg, info = qq.send_message(chwnd, body, blockers=(st.get("hwnd"),))
        log("[%s] 回复 %r -> ok=%s (%s)" % (name, body, ok, info))
        if not ok:
            return False
        sent_text = True

    sent_image = False
    if image_path:
        time.sleep(0.4)                     # 让上一条文字先落地，免得顺序颠倒
        gm, gf, gmb = gif_settings()
        ok_img, info_img = qq.send_image(chwnd, image_path, max_side=IMAGE_MAX_SIDE,
                                         gif_mode=gm, gif_frame=gf, gif_max_bytes=gmb,
                                         blockers=(st.get("hwnd"),))
        log("[%s] 表情包 %s -> ok=%s (%s)"
            % (name, os.path.basename(image_path), ok_img, info_img))
        if ok_img:
            sent_image = True
            st["last_image"] = image_path
            st["last_image_at"] = time.time()
            RUNTIME["images_total"] += 1

    if not sent_text and not sent_image:
        return False

    # 回显认领：发出去什么就登记什么预期，见 consume_own_echo()
    if sent_text:
        register_expect_echo(st, "text", body)
    if sent_image:
        register_expect_echo(st, "image")

    st["last_reply_at"] = time.time()
    # 给循环熔断记账：短时间回太多次就说明多半在自己回自己
    rl = st.setdefault("reply_log", [])
    rl.append(time.time())
    del rl[:-100]
    RUNTIME["last_reply_at"] = time.time()
    RUNTIME["last_reply_to"] = name
    RUNTIME["replies_today"] += 1
    RUNTIME["replies_total"] += 1
    st["history"].append({"role": "user", "content": incoming})
    st["history"].append({"role": "assistant", "content": body or ("[表情:%s]" % cat)})
    del st["history"][:-MAX_HISTORY_MESSAGES]   # 只留最近若干轮，避免长跑后只增不减
    save_history(name, st["history"])
    return True


# 「为什么找不到目标实例」的最新原因。只在变化时记日志（30 秒防抖），
# 心跳和 status 里随时引用 —— 以前「QQ 没登录」和「标记会话被删了」
# 在日志里一模一样，只能干瞪眼。
_PICK_NOTE = {"text": "", "at": 0.0}


def _note_pick(text):
    now = time.time()
    if text != _PICK_NOTE["text"]:
        if now - _PICK_NOTE["at"] >= 30.0:
            log(text)
        _PICK_NOTE["text"] = text
        _PICK_NOTE["at"] = now


def pick_sessions():
    """收集各 QQ 实例的会话列表，挑出要处理的那个实例。

    多开时每个实例一个主窗口、一份会话列表；用"列表里必须包含某些会话"
    来认账号（例如小号里有"小明"这个会话，大号自己没有）。
    返回 [(main_hwnd, pid, [(name, preview), ...]), ...]

    标记对不上时不再直接放弃 —— 以前的写法会让脚本在"好友被删/改名"之后
    永远找不到账号、一声不响地空转（本机日志里连着几十分钟的"缺标记会话"就是
    这么来的）。现在按风险从低到高兜底：
      1. 只有一个 QQ 实例，且配了白名单 -> 用它（名单已经限定了范围）；
      2. 多个实例 -> 只在唯一一个实例包含白名单好友时采用它；
      3. 开的是 listen_all 又没配白名单 -> 不猜账号，宁可不回。
    """
    instances = []
    rejects = []
    mains = qq.find_main_windows()
    for _, whwnd in mains:
        try:
            # 先只读名字：认账号用不到预览，而没选中的实例可能有一百个会话
            convs = qq.all_conversations(whwnd, with_preview=False)
        except Exception:
            convs = None
        if not convs:
            rejects.append("会话列表读取失败")
            continue
        try:
            _, wpid = win32process.GetWindowThreadProcessId(whwnd)
        except Exception:
            wpid = None
        instances.append({"hwnd": whwnd, "pid": wpid, "convs": convs})

    if not instances:
        if not mains:
            _note_pick("没有发现任何 QQ 主窗口（QQ 未启动或未登录）")
        else:
            _note_pick("发现 %d 个 QQ 窗口但会话列表都读不出来" % len(mains))
        return []

    chosen = []
    if ACCOUNT_MARKERS:
        for inst in instances:
            names = {n for n, _ in inst["convs"]}
            missing = [m for m in ACCOUNT_MARKERS if m not in names]
            if missing:
                rejects.append("一个实例（%d 个会话）缺标记会话 %s"
                               % (len(inst["convs"]), "、".join(missing)))
            else:
                chosen.append(inst)
    else:
        chosen = instances

    if not chosen and _WATCH["strict"] and ACCOUNT_MARKERS:
        _note_pick("严格模式：没有 QQ 实例含标记会话 %s，不做任何回复"
                   % ("、".join(ACCOUNT_MARKERS),))
        return []
    if not chosen:
        targets = _WATCH["targets"]
        if len(instances) == 1 and targets:
            chosen = instances
            _note_pick("没有实例含标记会话 %s，但只有 1 个 QQ 实例且配了白名单，直接用它"
                       % ("、".join(ACCOUNT_MARKERS),))
        elif targets:
            matched = [inst for inst in instances
                       if any(name_matches(n, t) for n, _ in inst["convs"] for t in targets)]
            if len(matched) == 1:
                chosen = matched
                _note_pick("没有实例含标记会话 %s，但只有 1 个实例含白名单好友，采用它"
                           % ("、".join(ACCOUNT_MARKERS),))
        if not chosen:
            _note_pick("发现 %d 个 QQ 实例但都不匹配：%s"
                       % (len(instances), "；".join(rejects) or "标记会话不存在"))

    if len(chosen) > 1 and ACCOUNT_MARKERS:
        _note_pick("有 %d 个实例都含标记会话 %s，无法区分账号，可能重复回复"
                   % (len(chosen), "、".join(ACCOUNT_MARKERS)))
    if chosen:
        _PICK_NOTE["text"] = ""        # 成功了就把上次的"找不到实例"提示清掉
    # 选中的实例补读一次预览：状态基线要用最后一条消息
    result = []
    for inst in chosen:
        try:
            full = qq.all_conversations(inst["hwnd"]) or inst["convs"]
        except Exception:
            full = inst["convs"]
        result.append((inst["hwnd"], inst["pid"], full))
    return result


def main():
    rotate_log()
    log("=" * 56)
    if not acquire_single_instance():
        log("已经有实例在运行（再开一个会把同一条消息回两次），本次退出")
        return 0
    log("启动：等待 QQ 主窗口出现")
    hwnd = None
    while not hwnd:
        hwnd = wait_for_main_window(900)
        if not hwnd:
            # 开机自启时 QQ 可能卡在扫码/验证登录上，超过 15 分钟很正常。
            # 以前这里直接退出，进程死了就再也不会自己起来，干脆一直等。
            log("等待 15 分钟仍未发现 QQ 主窗口（QQ 可能还没登录），继续等待")
    log("QQ 主窗口已就绪 hwnd=%s" % hwnd)
    if not os.path.exists(CONFIG_PATH):
        log("没有配置文件 %s —— 首次使用请复制 auto_reply_config.example.json "
            "为 auto_reply_config.json 并填好 api_key；现在按默认值（回声模式）运行"
            % CONFIG_PATH)
    refresh_watch_settings(force=True)      # 名单/开关以文件里最新的为准
    try:
        _pid0 = qq.window_pid(hwnd)
        log("QQ 版本：%s %s（%s 形态；控件按结构识别，不依赖某一版）"
            % (qq.process_name(_pid0) or "?", qq.process_version(_pid0) or "未知",
               qq.qq_flavor(_pid0)))
    except Exception:
        pass

    load_history()
    state = {}
    _now = time.time()
    sessions = pick_sessions()
    _update_env_info(sessions)
    for whwnd, wpid, convs in sessions:
        for name, preview in convs:
            state[(whwnd, name)] = new_state(name, preview, whwnd, wpid, _now)

    log("配置文件：%s" % CONFIG_PATH)
    if ACCOUNT_MARKERS:
        log("账号识别：只处理会话列表里包含 %s 的 QQ 实例" % ("、".join(ACCOUNT_MARKERS),))
    else:
        log("账号识别：未设置 account_markers，会处理所有 QQ 实例（多开时请务必设置）")
    if TARGETS:
        log("监听对象：%s" % ("、".join(TARGETS),))
    elif LISTEN_ALL:
        log("监听对象：全部会话（listen_all=true）")
    else:
        log("监听对象：未配置（不会有任何回复）")
    if IGNORE:
        log("黑名单（忽略）：%s" % ("、".join(IGNORE),))
    _llm0 = llm_settings()
    if _llm0["api_key"]:
        log("模型=%s  base_url=%s  (key 已配置，长度 %d；改配置即生效，无需重启)"
            % (_llm0["model"], _llm0["base_url"], len(_llm0["api_key"])))
    else:
        log("未配置 api_key —— 回声模式，回复内容为『收到：xxx』（填好 key 后下一条消息即生效）")
    log("已为 %d 个会话建立基线，开始监听（轮询 %.1f 秒；Ctrl+C 停止）" % (len(state), POLL_SECONDS))

    limiter = RateLimiter(GLOBAL_REPLY_LIMIT, GLOBAL_WINDOW)
    RUNTIME["started_at"] = time.time()

    # 崩溃重启时 main() 里的 state 已经没了，留个引用给 write_status 兜底
    global _last_state
    _last_state = state

    misses = 0
    last_beat = time.time()
    if QUIET_HOURS:
        log("免打扰时段：%s" % QUIET_HOURS)
    log("节流：单会话最少间隔 %.0f 秒；全局 %d 条 / %.0f 秒；收到后先攒 %.1f 秒防连发"
        % (MIN_REPLY_INTERVAL, GLOBAL_REPLY_LIMIT, GLOBAL_WINDOW, REPLY_DEBOUNCE_SECONDS))
    if IMAGE_ENABLED:
        _cats = image_categories()
        if _cats:
            log("表情包：启用，%d 个类别共 %d 张（%s），同一会话最少间隔 %.0f 秒"
                % (len(_cats), sum(len(v) for v in _cats.values()),
                   "、".join(sorted(_cats.keys())), IMAGE_MIN_INTERVAL))
        else:
            log("表情包：已启用，但 %s 下还没有图片 —— 建子目录丢图进去即可，不用重启"
                % IMAGE_DIR_ABS)
    else:
        log("表情包：已关闭（image_enabled=false）")
    write_status(state)

    while True:
        # 每轮都看一眼日志大小。原先轮转只挂在心跳分支上，而心跳要求"本轮没有
        # 任何会话变化"——一直有人说话时它永远不触发，日志就不会轮转。
        # 这个调用只是一次 getsize，几乎不花钱。
        rotate_log()
        try:
            if refresh_watch_settings():
                # 名单改完立刻生效：被移出监听范围的人，积压的待回复也一起丢掉，
                # 否则改完名单它还会再回一句。
                for _key, _st in list(state.items()):
                    if should_listen(_key[1]):
                        continue
                    if _st.get("pending"):
                        log("[%s] 已不在监听名单里，丢弃 %d 条待回复"
                            % (_key[1], len(_st["pending"])))
                    _st["pending"] = []
                    _st.pop("pending_at", None)
                write_status(state)
        except Exception as e:
            log("配置热更新失败：%r" % (e,))
        try:
            sessions = pick_sessions()
        except Exception as e:
            log("读取异常：%r" % (e,))
            sessions = []

        if not sessions:
            misses += 1
            if misses in (5, 30):
                log("已连续 %d 次找不到目标账号的会话列表（不做任何窗口操作，继续等待）" % misses)
            if time.time() - last_beat > HEARTBEAT_SECONDS:
                log("心跳：%s" % (_PICK_NOTE["text"] or "仍在监听，只是找不到目标账号"))
                last_beat = time.time()
                rotate_log()
                write_status(state, {"note": _PICK_NOTE["text"] or "找不到目标 QQ 实例的会话列表"})
            time.sleep(POLL_SECONDS)
            continue
        misses = 0
        _update_env_info(sessions)

        changed_any = False
        now = time.time()
        active_keys = set()
        try:
            for whwnd, wpid, convs in sessions:
                for name, preview in convs:
                    if not should_listen(name):
                        continue
                    key = (whwnd, name)
                    active_keys.add(key)
                    st = state.get(key)
                    if st is None:
                        state[key] = new_state(name, preview, whwnd, wpid, now)
                        st = state[key]
                        if (_WATCH["skip_group"] and _WATCH["listen_all"]
                                and not _WATCH["targets"] and is_group_like(preview)):
                            st["group_muted"] = True
                            log("[%s] 疑似群聊/系统会话（预览 %r），已自动跳过"
                                "（要监听就加进 targets，或设 skip_group_like=false）"
                                % (name, preview))
                        else:
                            log("[%s] 新会话，建立基线：%r" % (name, preview))
                        continue
                    st["last_seen_at"] = now
                    # listen_all 下拦群聊。每条预览都复检，不像群聊了自动恢复，
                    # 防止朋友一句「PS: 明天带伞」把会话永久误伤。
                    group_like = (_WATCH["skip_group"] and _WATCH["listen_all"]
                                  and not _WATCH["targets"] and is_group_like(preview))
                    if st.get("group_muted"):
                        if group_like:
                            st["seen"] = preview
                            continue
                        st["group_muted"] = False
                        log("[%s] 预览不再像群聊，恢复监听" % name)
                    elif group_like:
                        st["group_muted"] = True
                        st["seen"] = preview
                        log("[%s] 疑似群聊/系统会话（预览 %r），已自动跳过"
                            "（要监听就加进 targets，或设 skip_group_like=false）"
                            % (name, preview))
                        continue
                    if preview == st["seen"]:
                        continue

                    st["seen"] = preview
                    changed_any = True

                    if consume_own_echo(st, preview):
                        log("[%s] （自己刚发出的回显，忽略）" % name)
                        continue

                    # 你在聊天窗口里手动打的字发出的：预览长得跟对方发的一模一样，
                    # 不认领就会被当成对方的新消息，然后自己回自己（自言自语）。
                    if _MANUAL["enabled"] and claim_manual_send(st, preview, now):
                        log("[%s] （你手动发出的：%r，按我方消息处理）"
                            % (name, preview[:40]))
                        st["seen"] = preview
                        # 既然你已经替它回过话了，积压的待回复就不必再回一次
                        if _MANUAL["clears"] and st.get("pending"):
                            log("[%s] 清掉 %d 条积压待回复（人工已代答）"
                                % (name, len(st["pending"])))
                            st["pending"] = []
                            st.pop("pending_at", None)
                        # 记进上下文，让模型知道自己这侧说过这句话，对话才连得上
                        st["history"].append({"role": "assistant", "content": preview})
                        del st["history"][:-MAX_HISTORY_MESSAGES]
                        save_history(name, st["history"])
                        st["last_reply_at"] = now
                        continue

                    log("[%s] 检测到新消息：%r" % (name, preview))
                    # 给循环熔断记账：这条不是回显，是对方真发的
                    il = st.setdefault("incoming_log", [])
                    il.append(now)
                    del il[:-100]
                    RUNTIME["last_incoming_at"] = now
                    RUNTIME["last_incoming_from"] = name

                    # 入队，由 flush_pending() 统一挑时机发出。
                    # 以前在这里直接做节流判断，命中的话 continue —— 而 seen 已经更新，
                    # 那条消息再也检测不到，等于被静默丢弃。这是"回了一句之后就不回了"的原因。
                    pend = st.setdefault("pending", [])
                    pend.append(preview)
                    del pend[:-MAX_PENDING_MESSAGES]     # 只保留最近几条，防止刷屏时堆积
                    st["pending_at"] = now
        except Exception:
            # 单条消息处理失败（UIA 抖动、LLM 超时等）不该弄死整个轮询，
            # 以前它一路冒泡到顶层，进程在 pythonw 下就静默没了。
            RUNTIME["errors_total"] += 1
            RUNTIME["last_error"] = traceback.format_exc(limit=5).strip().splitlines()[-1]
            log("处理异常（已跳过本轮）：\n" + traceback.format_exc())

        # 扫一遍已打开的聊天窗口：有没有人在手动打字（人工代打）
        try:
            scan_manual_typing(state, now)
        except Exception:
            RUNTIME["errors_total"] += 1
            RUNTIME["last_error"] = traceback.format_exc(limit=5).strip().splitlines()[-1]
            log("人工打字检测异常（已跳过本轮）：\n" + traceback.format_exc())

        try:
            collect_replies(state)
        except Exception:
            RUNTIME["errors_total"] += 1
            RUNTIME["last_error"] = traceback.format_exc(limit=5).strip().splitlines()[-1]
            log("回复发送异常（已跳过本轮）：\n" + traceback.format_exc())

        try:
            flush_pending(state, limiter, time.time())
        except Exception:
            RUNTIME["errors_total"] += 1
            RUNTIME["last_error"] = traceback.format_exc(limit=5).strip().splitlines()[-1]
            log("发送队列异常（已跳过本轮）：\n" + traceback.format_exc())

        dropped = prune_state(state, active_keys, now)
        if dropped:
            log("清理 %d 个失效会话状态（QQ 主窗口 hwnd 已变化）" % dropped)

        if not changed_any and time.time() - last_beat > HEARTBEAT_SECONDS:
            log("心跳：监听中，%d 个会话均无变化" % len(state))
            last_beat = time.time()
            rotate_log()
            write_status(state)

        time.sleep(POLL_SECONDS)


# ————————————————————————————————————————————————
# 命令行子命令（和常驻监听共用同一个配置文件）
# ————————————————————————————————————————————————
CLI_COMMANDS = {
    "list": "qq_friends", "friends": "qq_friends", "pick": "qq_friends",
    "add": "qq_friends", "del": "qq_friends", "rm": "qq_friends",
    "remove": "qq_friends", "ignore": "qq_friends", "unignore": "qq_friends",
    "show": "qq_friends", "check": "qq_friends", "account": "qq_friends",
    "doctor": "qq_doctor", "diag": "qq_doctor",
}

USAGE = """用法：
  python auto_reply.py                 启动自动回复（原来的行为）
  python auto_reply.py list            列出 QQ 里的全部会话，标出谁在名单里
  python auto_reply.py pick            交互式勾选「可自动回复的好友」
  python auto_reply.py add 名字 ...    把好友加进白名单
  python auto_reply.py del 名字 ...    从白名单移除
  python auto_reply.py ignore 名字 ... 拉黑（永不回复）
  python auto_reply.py unignore 名字   从黑名单移除
  python auto_reply.py show            显示当前名单
  python auto_reply.py check           核对名单里的人现在还在不在
  python auto_reply.py account         查看 QQ 实例、账号标记命中、本机登录过的号
  python auto_reply.py doctor          自检 QQ 版本/控件识别/配置
名单改完，正在运行的脚本会在下一个轮询周期自动生效，不用重启。
"""


def cli(argv):
    cmd = (argv[0] or "").strip().lower()
    mod = CLI_COMMANDS.get(cmd)
    if not mod:
        print("未知命令：%s" % cmd)
        print(USAGE)
        return 2
    return __import__(mod).main(argv)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cmd = sys.argv[1].strip().lower()
        if _cmd in CLI_COMMANDS:
            sys.exit(cli(sys.argv[1:]))
        if _cmd in ("-h", "--help", "help", "?", "/?"):
            print(USAGE)
            sys.exit(0)
    _retries = 0
    while True:
        _t0 = time.time()
        try:
            sys.exit(main())
        except KeyboardInterrupt:
            log("已停止")
            break
        except SystemExit:
            break                       # main() 正常返回（含等待超时、已有实例在跑）
        except Exception:
            # 兜底：pythonw 没有控制台，崩溃不留痕的话只能盯着进程列表猜
            if time.time() - _t0 > RESTART_RESET_SECONDS:
                _retries = 0           # 稳定运行够久就重新计数
            _retries += 1
            tb = traceback.format_exc()
            RUNTIME["errors_total"] += 1
            RUNTIME["last_error"] = tb.strip().splitlines()[-1]
            log("第 %d 次崩溃：\n%s" % (_retries, tb))
            write_status(_last_state, {"note": "崩溃后准备重启，第 %d 次" % _retries})
            if _retries > MAX_RESTARTS:
                log("连续崩溃超过 %d 次，放弃自愈，进程退出" % MAX_RESTARTS)
                break
            time.sleep(RESTART_BACKOFF_SECONDS)
            log("尝试第 %d 次重启" % _retries)
