# -*- coding: utf-8 -*-
"""自检：确认这台机器上的 QQ 能被脚本认出来。

换 QQ 版本之后如果自动回复不工作，先跑这个：
    python auto_reply.py doctor

它只读，不点任何按钮、不改窗口状态，输出：
  - Python 和依赖库是否齐全
  - 找到几个 QQ 进程、什么版本、被判定成哪种形态
  - 每个 QQ 实例的会话列表能不能读、账号标记对不对得上
  - 每个聊天窗口的输入框/消息列表有没有认出来
  - 配置里的名单现在是否有效

排查口诀：
  没有 QQ 主窗口   -> QQ 没登录 / 只挂在托盘但窗口被销毁，重开 QQ
  会话 0 条        -> 权限不够或 UIA 被挡（管理员运行的 QQ 要管理员运行脚本）
  输入框没认出来   -> 把这里的输出发给我，按控件的名字和位置补一条规则
"""
import os
import sys

import qq_driver as qq
import qq_friends as friends


def _line(title):
    print("\n== %s ==" % title)


def _dep(mod_name, label=None):
    try:
        mod = __import__(mod_name)
        ver = getattr(mod, "__version__", "")
        return True, "%s%s 可用" % (label or mod_name, (" " + str(ver)) if ver else "")
    except Exception as e:
        return False, "%s 导入失败：%r" % (label or mod_name, e)


def main(argv=None):
    ok_all = True
    _line("环境")
    print("Python %s" % sys.version.split()[0])
    print("脚本目录 %s" % os.path.dirname(os.path.abspath(__file__)))
    for mod, label in (("uiautomation", "uiautomation"), ("comtypes", "comtypes"),
                       ("win32gui", "pywin32"), ("PIL", "Pillow")):
        ok, msg = _dep(mod, label)
        print(("  ✓ " if ok else "  ✗ ") + msg)
        ok_all = ok_all and ok

    _line("QQ 进程")
    tops = qq.top_windows()
    pids = []
    for _t, h in tops:
        pid = qq.window_pid(h)
        if pid and pid not in pids and qq.is_qq_process(pid):
            pids.append(pid)
    if not pids:
        print("  ✗ 没有发现 QQ/TIM 进程（QQ 没启动？）")
        ok_all = False
    for pid in pids:
        print("  pid=%s %s 版本 %s  形态=%s"
              % (pid, qq.process_name(pid), qq.process_version(pid) or "未知",
                 qq.qq_flavor(pid)))

    _line("QQ 窗口")
    mains = qq.find_main_windows()
    print("主窗口 %d 个" % len(mains))
    if not mains:
        print("  ✗ 没认出主窗口：脚本不会读会话列表")
        ok_all = False
    for i, (title, hwnd) in enumerate(mains, 1):
        convs = qq.all_conversations(hwnd) or []
        pid = qq.window_pid(hwnd)
        print("  [实例 %d] hwnd=%s 标题=%r  会话 %d 条"
              % (i, hwnd, (title or "")[:30], len(convs)))
        if not convs:
            ok_all = False
            print("      ✗ 会话列表读不出来（见下方「排查」）")

    markers = friends.config_list("account_markers")
    if markers and mains:
        print("\n账号标记 %s：" % ("、".join(markers),))
        for i, (_t, hwnd) in enumerate(mains, 1):
            names = {n for n, _ in (qq.all_conversations(hwnd) or [])}
            missing = [m for m in markers if m not in names]
            if missing:
                print("  [实例 %d] 缺 %s" % (i, "、".join(missing)))
            else:
                print("  [实例 %d] ✓ 全部命中" % i)

    _line("聊天窗口与控件识别")
    chats = qq.chat_windows()
    if not chats:
        print("  当前没有打开的聊天窗口（不影响监听，来消息时会自己打开）")
    for name, hwnd in chats:
        el = qq.input_elem(hwnd)
        ml = qq.message_list_elem(hwnd)
        print("  窗口 hwnd=%s 当前会话=%r" % (hwnd, name))
        if el is None:
            ok_all = False
            print("    ✗ 输入框没认出来 —— 发送会直接失败（脚本会拒绝在无输入框时打字）")
        else:
            try:
                ct = el.CurrentControlType
            except Exception:
                ct = "?"
            has_value = qq.elem_value(el) is not None
            has_text = qq.elem_text(el) is not None
            print("    ✓ 输入框：类型=%s 名字=%r Value=%s Text=%s"
                  % (ct, qq._name_of(el), has_value, has_text))
        if ml is None:
            print("    · 消息列表没认出来（不影响发送，影响「确认发出」和识图截图）")
        else:
            msgs = qq.read_message_texts(hwnd)
            print("    ✓ 消息列表：名字=%r 条目 %d 条"
                  % (qq._name_of(ml), len(msgs)))
        draft = qq.read_input_text(hwnd)
        print("    输入框当前内容：%r" % (draft,))

    _line("发送键")
    print("配置 send_key=%s；运行中首次发送会先试 Ctrl+Enter，"
          "输入框没清空就自动换 Enter。" % (friends.load_config().get("send_key") or "auto"))

    _line("配置")
    cfg = friends.load_config()
    if not cfg:
        print("  ✗ 配置文件读不出来：%s" % friends.CONFIG_PATH)
        ok_all = False
    print("  文件：%s" % friends.CONFIG_PATH)
    print("  可回复好友 targets：%s"
          % ("、".join(friends.config_list("targets")) or "（空）"))
    print("  黑名单 ignore：%s"
          % ("、".join(friends.config_list("ignore")) or "（空）"))
    print("  listen_all=%s  send_key=%s"
          % (cfg.get("listen_all"), cfg.get("send_key") or "auto"))

    targets = friends.config_list("targets")
    if targets:
        rows = friends.list_conversations()
        names = {r["name"] for r in rows}
        missing = [t for t in targets if t not in names]
        if missing:
            ok_all = False
            print("  ✗ 这些好友不在任何会话列表里：%s" % ("、".join(missing),))
        else:
            print("  ✓ 白名单里的好友都在会话列表里")
        if not rows:
            print("  ✗ 读不到会话列表，没法核对名单")

    _line("结论")
    if ok_all:
        print("  ✓ 基本可用：窗口、输入框、发送键都认得出来。")
    else:
        print("  ✗ 有项目没通过（看上面带 ✗ 的行）。")
        print("    常见原因：QQ 没登录 / QQ 以管理员身份运行而脚本不是 / "
              "QQ 是全新版本、控件结构变了。")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
