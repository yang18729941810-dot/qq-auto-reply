# -*- coding: utf-8 -*-
"""QQ 好友/群 会话枚举 + 可回复白名单管理。

为什么要单独做这个：自动回复脚本的「能回谁」是配置里的 targets 名单，
而名单要手抄 QQ 里的备注名，抄错一个字就永远匹配不上（日志里表现为
"缺标记会话 小明"，脚本一声不响地空转）。这里直接把 QQ 会话列表读出来，
列编号让人勾选，写完配置正在跑的脚本会自动生效。

用法（在项目目录下，或者双击「设置回复好友.bat」）：
    python auto_reply.py list        列出全部会话，标出谁在名单里
    python auto_reply.py pick        交互式勾选可自动回复的好友
    python auto_reply.py add 名字    追加
    python auto_reply.py del 名字    移除
    python auto_reply.py ignore 名字 拉黑（优先级最高，永不回复）
    python auto_reply.py show        看当前配置
    python auto_reply.py check       核对名单里的人现在还在不在会话列表里

只读 QQ、不抢焦点；写配置是「读原文 -> 只替换那一个数组 -> 原子替换」，
其余注释和字段原样保留。
"""
import json
import os
import re
import sys
import time
import unicodedata

import qq_driver as qq

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "auto_reply_config.json")
EXAMPLE_PATH = os.path.join(HERE, "auto_reply_config.example.json")
STATUS_PATH = os.path.join(HERE, "auto_reply_status.json")

# 群消息预览带「发送者: 内容」前缀，单聊预览是纯文本。和 auto_reply.py 里的
# 判断保持一致（那边用于 listen_all 自动跳过群聊，这边只用于给用户提示）。
GROUP_PREVIEW_RE = re.compile(r"^[^:：\n]{1,20}[:：](?!//)")
SYSTEM_NAMES = ("验证消息", "好友动态", "QQ空间", "服务号", "订阅号", "新朋友",
                "QQ团队", "QQ安全中心", "腾讯新闻")


# ————————————————————————————————————————————————
# 配置读写（尽量保留原文件的排版和注释键）
# ————————————————————————————————————————————————

def ensure_config():
    """没有 auto_reply_config.json 时从 example 复制一份（刚 clone 下来的情况）。"""
    if os.path.exists(CONFIG_PATH):
        return CONFIG_PATH
    if os.path.exists(EXAMPLE_PATH):
        try:
            with open(EXAMPLE_PATH, "r", encoding="utf-8") as f:
                write_text_atomic(f.read())
            print("已从 auto_reply_config.example.json 生成 auto_reply_config.json")
            return CONFIG_PATH
        except Exception:
            pass
    write_text_atomic('{\n  "targets": []\n}\n')
    return CONFIG_PATH


def read_text():
    ensure_config()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return f.read()


def write_text_atomic(text):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def config_list(key):
    v = load_config().get(key)
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _dedupe(values):
    out, seen = [], set()
    for v in values:
        v = str(v).strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def set_config_list(key, values):
    """只替换 `"key": [...]` 那一行，其余字节原样保留；返回写入后的列表。"""
    values = _dedupe(values)
    text = read_text()
    payload = json.dumps(values, ensure_ascii=False)
    pat = re.compile(r'(?m)^(\s*)"%s"(\s*:\s*)\[[^\[\]]*\]' % re.escape(key))
    m = pat.search(text)
    if m:
        new_text = (text[:m.start()] + m.group(1) + '"%s"%s%s'
                    % (key, m.group(2), payload) + text[m.end():])
    else:
        # 配置里没这个键：退回整份 JSON 重写（会丢排版，但不会丢数据）
        cfg = load_config()
        cfg[key] = values
        new_text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    write_text_atomic(new_text)
    return values


# ————————————————————————————————————————————————
# 会话枚举
# ————————————————————————————————————————————————

def _mains_sorted():
    """按 pid 排序，让「实例 1/2」的编号在同一个 QQ 会话里稳定。

    EnumWindows 的枚举顺序不保证固定，直接用它编号会出现「上次 1 是小明、
    这次 1 是别人」——勾选时按编号写名单就写错人了。
    """
    mains = qq.find_main_windows()
    return sorted(mains, key=lambda t: (qq.window_pid(t[1]) or 0, t[1]))


def list_conversations():
    """所有 QQ 实例的会话：[{inst, hwnd, pid, version, name, preview, kind}]。"""
    rows = []
    mains = _mains_sorted()
    for i, (_title, hwnd) in enumerate(mains, 1):
        try:
            convs = qq.all_conversations(hwnd) or []
        except Exception:
            convs = []
        pid = qq.window_pid(hwnd)
        ver = qq.process_version(pid)
        for name, preview in convs:
            name = (name or "").strip()
            if not name:
                continue
            rows.append({"inst": i, "hwnd": hwnd, "pid": pid, "version": ver,
                         "name": name, "preview": (preview or "").strip()})
    return rows


def kind_of(row):
    name = row["name"]
    if name in SYSTEM_NAMES:
        return "系统"
    if GROUP_PREVIEW_RE.match(row["preview"] or ""):
        return "群聊?"
    return "单聊"


def _dw(s):
    """显示宽度：中日韩全角字符占两格，用来对齐表格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in s)


def _fit(s, width):
    """按显示宽度截断/补空格。"""
    s = s or ""
    out = ""
    w = 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width:
            if width >= 1 and w < width:
                out += "…"
            break
        out += ch
        w += cw
    return out + " " * max(0, width - w)


def _pid_alive(pid):
    """进程还在不在（Windows：OpenProcess + GetExitCodeProcess == STILL_ACTIVE）。

    不能用 os.kill(pid, 0)：在 Windows 上它等价于 TerminateProcess(pid, 0)，
    会把正在跑的自动回复直接杀掉。
    """
    import ctypes
    from ctypes import wintypes
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259          # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def bot_running():
    """正在跑的实例（status.json 里的 pid 还活着、且状态是最近更新的）。"""
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        pid = int(st.get("pid") or 0)
        if not pid or not _pid_alive(pid):
            return None
        updated = time.mktime(time.strptime(st.get("updated_at", ""), "%Y-%m-%d %H:%M:%S"))
        if time.time() - updated > 900:
            return None
        return st
    except Exception:
        return None


def print_status_line():
    st = bot_running()
    if st:
        print("自动回复脚本正在运行（pid %s，%s 起，今天回了 %s 条）——"
              "改完名单会自动生效，不用重启。"
              % (st.get("pid"), st.get("started_at", "?"), st.get("replies_today", 0)))
    else:
        print("自动回复脚本当前没有在运行（改完名单下次启动生效）。")


def print_rows(rows, targets, ignore):
    if not rows:
        print("没有读到任何会话。")
        print("可能原因：QQ 没登录 / 主窗口被隐藏 / 这个 QQ 是全新账号。")
        return
    by_inst = {}
    for i, r in enumerate(rows, 1):
        r["idx"] = i
        by_inst.setdefault(r["inst"], []).append(r)
    print("共 %d 个会话，来自 %d 个 QQ 实例：" % (len(rows), len(by_inst)))
    for inst, items in sorted(by_inst.items()):
        r0 = items[0]
        print("\n[实例 %d] pid=%s  QQ %s  %d 个会话"
              % (inst, r0["pid"], r0["version"] or "?", len(items)))
        for r in items:
            mark = "  "
            if r["name"] in targets:
                mark = "✅"
            elif r["name"] in ignore:
                mark = "🚫"
            kind = kind_of(r)
            print("  %s%4d. %s %s %s"
                  % (mark, r["idx"], _fit(kind, 6), _fit(r["name"], 30),
                     _fit(r["preview"], 34)))


def _parse_selection(raw, rows):
    """解析勾选输入，返回 (选中的会话名列表, 无法识别的词列表)。

    支持：编号 `1 3 5`、区间 `1-4`、`all`、`none`，也允许直接写名字
    （`小明 小红`）—— 编号会随实例顺序变，名字不会。
    """
    raw = (raw or "").strip()
    if not raw:
        return [], []
    low = raw.lower()
    if low in ("all", "*", "全部"):
        return [r["name"] for r in rows], []
    if low in ("none", "0", "空", "清空"):
        return [], []
    picked, unknown = [], []
    by_name = {r["name"]: r["name"] for r in rows}
    if raw in by_name:                 # 名字里带空格（如「结课 数学分析上2025结课」）
        return [raw], []
    for tok in re.split(r"[,\s，、;；]+", raw):
        if not tok:
            continue
        m = re.match(r"^(\d+)\s*[-~]\s*(\d+)$", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            for i in range(min(a, b), max(a, b) + 1):
                if 1 <= i <= len(rows):
                    picked.append(rows[i - 1]["name"])
            continue
        if tok.isdigit():
            i = int(tok)
            if 1 <= i <= len(rows):
                picked.append(rows[i - 1]["name"])
            continue
        if tok in by_name:
            picked.append(tok)
        else:
            unknown.append(tok)
    return picked, unknown


# ————————————————————————————————————————————————
# 子命令
# ————————————————————————————————————————————————

def cmd_list(args=None):
    rows = list_conversations()
    print_rows(rows, set(config_list("targets")), set(config_list("ignore")))
    targets = config_list("targets")
    print("\n当前白名单（targets）：%s" % ("、".join(targets) if targets else "（空，会看 listen_all）"))
    ignore = config_list("ignore")
    if ignore:
        print("当前黑名单（ignore）：%s" % ("、".join(ignore),))
    print("提示：勾选用 `python auto_reply.py pick`，直接加用 `... add 名字`。")
    return 0


def cmd_pick(args=None):
    rows = list_conversations()
    if not rows:
        print("没有读到任何会话，先确认 QQ 已登录、主窗口能打开。")
        return 1
    targets = config_list("targets")
    ignore = config_list("ignore")
    args = [a for a in (args or []) if a.strip()]
    if not args:
        print_rows(rows, set(targets), set(ignore))
        print("\n当前白名单：%s" % ("、".join(targets) if targets else "（空）"))
    if args:
        raw = " ".join(args)          # 也支持 pick 小明 小红 直接指定
    else:
        print("请输入要自动回复的好友编号：空格/comma 分隔，可写 1-5；"
              "也可以直接写名字；all=全部；none=清空；直接回车=不改。")
        try:
            raw = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 0
    if not raw.strip():
        print("没有改动。")
        return 0
    chosen, unknown = _parse_selection(raw, rows)
    if unknown:
        print("没听懂这些：%s（要么写编号，要么写 list 里的名字）" % ("、".join(unknown),))
    if not chosen and not unknown:
        print("没有改动。")
        return 0
    if not chosen:
        print("注意：空白名单 + listen_all=false 时脚本不会回复任何人。")
    # 同名会话在不同实例里出现时，按名字写入等于两个实例都会回 —— 提醒一下
    dup = {n for n in chosen if sum(1 for r in rows if r["name"] == n) > 1}
    if dup:
        print("提醒：%s 在多个 QQ 实例里都有同名会话，名单按名字匹配，两个号都会回。"
              % ("、".join(sorted(dup)),))
    after = set_config_list("targets", chosen)
    print("\n已写入 %s" % CONFIG_PATH)
    print("白名单：%s" % ("、".join(after) if after else "（空）"))
    # 勾了群的时候再多说一句
    groups = [n for n in after if any(r["name"] == n and kind_of(r) == "群聊?"
                                       for r in rows)]
    if groups:
        print("提醒：%s 看起来像群聊。白名单模式下不会自动过滤群，"
              "确认要让它对着群说话吗？" % ("、".join(groups),))
    print_status_line()
    return 0


def cmd_add(args):
    names = [a for a in (args or []) if a.strip()]
    if not names:
        print("用法：python auto_reply.py add 好友名 [好友名...]")
        return 2
    rows = list_conversations()
    known = {r["name"] for r in rows}
    for n in names:
        if known and n not in known:
            print("提醒：会话列表里没有「%s」这个名字。会照写，但脚本认不出来的话"
                  "多半是名字对不上（用 list 看一眼准确写法）。" % n)
    after = set_config_list("targets", config_list("targets") + names)
    print("白名单：%s" % ("、".join(after),))
    print_status_line()
    return 0


def cmd_del(args):
    names = [a for a in (args or []) if a.strip()]
    if not names:
        print("用法：python auto_reply.py del 好友名 [好友名...]")
        return 2
    after = [n for n in config_list("targets") if n not in names]
    set_config_list("targets", after)
    print("白名单：%s" % ("、".join(after) if after else "（空）"))
    print_status_line()
    return 0


def cmd_ignore(args):
    names = [a for a in (args or []) if a.strip()]
    if not names:
        print("用法：python auto_reply.py ignore 名字 [...]")
        return 2
    after = set_config_list("ignore", config_list("ignore") + names)
    print("黑名单：%s" % ("、".join(after),))
    print_status_line()
    return 0


def cmd_unignore(args):
    names = [a for a in (args or []) if a.strip()]
    if not names:
        print("用法：python auto_reply.py unignore 名字 [...]")
        return 2
    after = [n for n in config_list("ignore") if n not in names]
    set_config_list("ignore", after)
    print("黑名单：%s" % ("、".join(after) if after else "（空）"))
    print_status_line()
    return 0


def cmd_show(args=None):
    cfg = load_config()
    targets = config_list("targets")
    ignore = config_list("ignore")
    markers = config_list("account_markers")
    print("配置文件：%s" % CONFIG_PATH)
    print("可回复好友（targets）：%s" % ("、".join(targets) if targets else "（空）"))
    print("黑名单（ignore）      ：%s" % ("、".join(ignore) if ignore else "（空）"))
    print("账号标记（account_markers）：%s"
          % ("、".join(markers) if markers else "（未设置，会处理所有 QQ 实例）"))
    print("listen_all = %s%s" % (
        bool(cfg.get("listen_all")),
        "（targets 为空时才会对所有会话回复）" if not targets else "（已有 targets，listen_all 不生效）"))
    print("send_key   = %s" % (cfg.get("send_key") or "auto",))
    print("")
    print_status_line()
    return 0


def cmd_check(args=None):
    rows = list_conversations()
    names = {r["name"] for r in rows}
    targets = config_list("targets")
    ignore = config_list("ignore")
    if not rows:
        print("没有读到会话列表，无法核对。")
        return 1
    print("共 %d 个会话。" % len(rows))
    bad = 0
    for t in targets:
        hit = sorted({r["inst"] for r in rows if r["name"] == t})
        if hit:
            print("  ✅ 白名单「%s」在实例 %s 里找到了" % (t, "、".join(map(str, hit))))
        else:
            bad += 1
            print("  ❌ 白名单「%s」不在任何实例的会话列表里（名字抄错/好友被删/在这个号上没有）" % t)
    for t in ignore:
        if t not in names:
            print("  · 黑名单「%s」不在会话列表里（无害，可以不管）" % t)
    if not targets:
        print("  · 白名单是空的：listen_all=%s，此时会对所有会话生效（群聊会被 skip_group_like 过滤）"
              % (load_config().get("listen_all"),))
    return 0 if bad == 0 else 1


def _fmt_mtime(path):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    except Exception:
        return "?"


def _account_dirs():
    try:
        import auto_reply as ar
        return ar._qq_data_dirs()
    except Exception:
        return []


def cmd_account(args=None):
    """列出 QQ 实例、账号标记命中情况、可作为标记的独有会话。

    项目本身读不到 QQ 号（腾讯没把它暴露给 UI Automation），这里能做的是
    把「哪个窗口对哪个号」这件事变得可核对：实例的 pid/会话数、标记命中、
    以及本机数据目录里登录过的号。
    """
    mains = _mains_sorted()
    if not mains:
        print("没有发现 QQ 主窗口（QQ 未启动或未登录）。")
        return 1
    markers = config_list("account_markers")
    targets = config_list("targets")
    per_instance = []
    print("== QQ 实例 ==")
    for i, (_title, hwnd) in enumerate(mains, 1):
        convs = qq.all_conversations(hwnd) or []
        per_instance.append(convs)
        names = {n for n, _ in convs}
        print("[实例 %d] pid=%s hwnd=%s 会话 %d 条"
              % (i, qq.window_pid(hwnd), hwnd, len(convs)))
        if not markers:
            print("    账号标记：未设置（会处理所有实例）")
        else:
            miss = [m for m in markers if m not in names]
            if miss:
                print("    账号标记：✗ 缺 %s" % ("、".join(miss),))
            else:
                print("    账号标记：✓ 命中 %s" % ("、".join(markers),))
        hit = [t for t in targets if any(name_matches_loose(n, t) for n in names)]
        print("    白名单命中：%s" % ("、".join(hit) if hit else "（无）"))

    if len(per_instance) > 1:
        for i, convs in enumerate(per_instance, 1):
            others = set()
            for j, other in enumerate(per_instance, 1):
                if j != i:
                    others |= {n for n, _ in other}
            uniq = [n for n, _ in convs if n and n not in others]
            if uniq:
                print("\n[实例 %d] 只在这个号上有的会话（适合写进 account_markers）：" % i)
                for n in uniq[:8]:
                    print("   ", n)

    print("\n== 本机 QQ 数据目录里登录过的号 ==")
    dirs = _account_dirs()
    if not dirs:
        print("  没找到（可在配置里填 qq_data_dir，或 QQ 还没产生数据）")
    for d in dirs:
        print("  uin %s  最后写入 %s" % (os.path.basename(d), _fmt_mtime(d)))

    print("\n说明：项目读不到当前实例的 QQ 号（腾讯没把它暴露给 UI Automation）。")
    print("号码在 QQ 左上角头像 → 个人资料里看；把只有那个号才有的会话写进")
    print("account_markers（或用上面列出的候选），启动日志和 doctor 会显示命中情况。")
    print("担心回错号就设 strict_account=true：标记对不上时宁可完全不回。")
    return 0


def name_matches_loose(name, target):
    """会话名与配置名是否指同一个人（忽略空白/零宽字符）。"""
    def norm(x):
        return re.sub(r"[\s\u3000\u200b\ufeff]+", "", x or "").lower()
    return bool(name) and bool(target) and norm(name) == norm(target)


HANDLERS = {
    "account": cmd_account,
    "list": cmd_list, "friends": cmd_list, "pick": cmd_pick,
    "add": cmd_add, "del": cmd_del, "rm": cmd_del, "remove": cmd_del,
    "ignore": cmd_ignore, "unignore": cmd_unignore,
    "show": cmd_show, "check": cmd_check,
}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print(__doc__)
        return 2
    fn = HANDLERS.get(argv[0].lower())
    if fn is None:
        print(__doc__)
        return 2
    return fn(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
