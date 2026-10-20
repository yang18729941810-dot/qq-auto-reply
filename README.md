# QQ 自动回复（Windows）

中文 · [English](README.en.md)

用 UI Automation 读 QQ 窗口、用模拟键盘把回复打进去的自动回复工具。不注入进程、不改协议、不抓包，只做一个坐在电脑前的人会做的事。

- 监听指定好友的新消息，调任意 OpenAI 兼容接口生成回复，发回去
- 支持文字 + 表情包（模型在回复末尾写 `[表情:类别]`）
- 好友白名单可以在会话列表里勾选，改完运行中的脚本自动生效
- 控件识别按结构做，不写死某一版 QQ 的控件名/类名

> **重要声明**
> 这是对 QQ 客户端的自动化操作，可能违反腾讯的用户协议，存在被限制或封号的风险。
> 作者不对任何账号处罚、数据丢失或其它后果负责，请只在自己的账号上、在你能接受的范围内使用。
> 不要用它做群发、营销或骚扰。软件按 MIT 协议「原样」提供，不作任何担保。

## 功能

| 能力 | 说明 |
|---|---|
| 自动回复 | 指定好友来消息后，按上下文调用大模型生成回复并发出 |
| 好友白名单 | `pick` 从会话列表勾选，或 `add/del`；支持多开区分账号 |
| 表情包 | 按类别放图，模型自己决定发不发、发哪张 |
| 识图（可选，默认关） | 对方发图时把原图/截图交给多模态模型 |
| 人工接管 | 你在聊天窗口里打字时自动暂停，不插嘴 |
| 防刷屏 | 单会话间隔、全局限速、免打扰时段、自我循环熔断 |
| 热更新 | 提示词、模型参数、好友名单改完不用重启 |

## 它是怎么工作的

读写两条完全不同的路径，这也是它比注入式方案更「像人」的原因：

1. **读**：UI Automation（RawView）读会话列表项的名字和「最后一条消息」预览，靠预览变化判断有新消息。
2. **写**：把窗口抢到前台，用 `SendInput` 往输入框敲字，再用发送快捷键发出去，最后读输入框是否清空来确认。

发送前会校验「窗口当前显示的会话名 == 目标好友」，对不上就放弃，避免发错人。

## 环境要求

- Windows 10 / 11
- Python 3.9+（开发环境 3.13）
- QQ 9.x / QQ NT / TIM，桌面端，已登录
- **QQ 和脚本要用同一权限级别运行**（都普通，或都管理员）。管理员运行的 QQ，普通权限的脚本读不到它的控件

已实测：QQ 9.7.25（老 DirectUI 内核）。QQ NT（9.9+，Chromium 内核）和 TIM 走结构兜底，但没有实机验证，换版本后请先跑自检。

## 快速开始

```powershell
git clone https://github.com/yang18729941810-dot/qq-auto-reply.git
cd qq-auto-reply
pip install -r requirements.txt

copy auto_reply_config.example.json auto_reply_config.json
notepad auto_reply_config.json      # 填 api_key / base_url / model
python auto_reply.py
```

第一次先跑自检，确认窗口和控件都认出来了：

```powershell
python auto_reply.py doctor
```

## 设置可回复的好友

```powershell
python auto_reply.py list     # 列出 QQ 里的全部会话，标出谁在名单里、谁是群
python auto_reply.py pick     # 勾选（输入编号、区间、all/none，或直接写名字）
python auto_reply.py check    # 核对名单里的人现在还在不在
```

也可以双击 `设置回复好友.bat`。零散增删：

```powershell
python auto_reply.py add 小明 小红
python auto_reply.py del 小明
python auto_reply.py ignore 某群
python auto_reply.py unignore 某群
python auto_reply.py show
```

- 名字按**会话列表里显示的备注名**匹配；会忽略空格和零宽字符，不做包含匹配（写「牛」不会命中「牛马一家亲」）
- 配了 `targets` 就只回名单里的人，`listen_all` 不再生效
- 白名单模式**不会**自动过滤群；把群名写进名单等于让它对着群说话，`pick` 会提醒
- 名单写进 `auto_reply_config.json`，正在运行的脚本下一个轮询周期就生效

多开（同时登录多个 QQ）时，用 `account_markers` 指定「列表里含哪个会话」来认账号；
标记对不上时不会死等：只有一个实例且配了白名单就直接用它，多个实例时只在唯一一个含白名单好友时采用。

想先确认「哪个窗口是哪个号」：

```powershell
python auto_reply.py account
```

它会列出每个实例的 pid、窗口、会话数、账号标记命中情况、只在这个号上有的会话（可直接拿来当标记），
以及本机 QQ 数据目录里登录过的号。**项目读不到当前实例的 QQ 号**（腾讯没把它暴露给 UI Automation），
号码要在 QQ 左上角头像 → 个人资料里看，再用标记会话把两边对上。
担心回错号就设 `strict_account: true`：标记对不上时完全不回，不用单实例兜底。

## 自检

```powershell
python auto_reply.py doctor
```

只读，不点按钮、不改窗口状态。输出环境依赖、QQ 进程/版本/形态、每个实例的会话数、聊天窗口的输入框和消息列表认到了什么、名单是否有效，最后给结论。

换 QQ 版本后自动回复不工作时，先跑它；「输入框没认出来」这类问题看输出里的控件类型和名字。

## 常用配置

完整项见 `auto_reply_config.example.json`，带 `_说明_` 的键是注释，程序不读。

| 键 | 说明 |
|---|---|
| `api_key` / `base_url` / `model` | 任意 OpenAI 兼容接口；留空 key 走回声模式 |
| `targets` | 可回复好友名单，空则看 `listen_all` |
| `listen_all` | 是否回复所有会话（风险高，默认关） |
| `account_markers` | 多开认账号用的会话名 |
| `strict_account` | 标记对不上时完全不回（默认 false，会走单实例兜底） |
| `ignore` | 黑名单，优先级最高 |
| `send_key` | `auto` / `enter` / `ctrl+enter`；auto 会自己学会当前 QQ 的设置 |
| `min_reply_interval` / `global_reply_limit` / `quiet_hours` | 节流与免打扰 |
| `manual_typing_enabled` | 检测到你手动打字时暂停该会话 |
| `manual_mode` | 只听不回（比杀进程省事，队列和历史都不丢） |
| `vision_enabled` | 识图；会消耗图片 token，图会上传到模型服务商 |

`api_key`、`system_prompt_lines`、模型参数、`targets`、`ignore`、`listen_all`、`send_key`、`account_markers`、`strict_account` 都是热更新的；节流、免打扰、识图、表情包等改完要重启。

## 表情包

把图片放进 `images\<类别名>\`，类别名就是模型能写的类别：

```
images/
  开心/  无语/  疑问/  生气/  害羞/  赞同/  敷衍/  晚安/  打招呼/
```

模型在回复末尾写 `[表情:类别]` 时，脚本摘掉标记，先发文字再发一张图。
同一目录随机挑、一轮内不重复；同一会话默认 120 秒最多一张（`image_min_interval`）。
支持 png/jpg/jpeg/bmp/gif/webp。仓库不带图片，自己往里放。

## 已知限制

- 读消息靠「会话预览变了没有」，两条完全相同的消息可能只认到一条；方向读不到，自己发的回显靠内容比对认领
- 群聊/单聊靠「预览里有没有 `发送者: ` 前缀」区分，是启发式
- 消息列表不提供每条消息的坐标，所以发消息前只能校验「窗口显示的是谁」，不能校验「这条是发给谁的」
- QQ NT 需要 Chromium 建立无障碍树；脚本会发 `WM_GETOBJECT` 去触发，但这不是官方接口
- 多开时两个号有同名会话，`account_markers` 无法区分，会重复回复

## 常见问题

**日志一直说「没有发现任何 QQ 主窗口」**
QQ 没启动/没登录，或者 QQ 以管理员运行而脚本不是（反过来也一样）。

**一直「无法打开聊天窗口」**
先用 `list` 看会话在不在、名字写对没有；再跑 `doctor` 看控件识别。合并会话窗口（`小明等2个会话`）是支持的。

**发送失败 / 输入框一直是空的**
`send_key` 设 `auto`；两个组合都试过还不行，多半是输入框没认对，跑 `doctor` 看那一行。

**会不会封号**
会。这是客户端自动化，不是官方接口。降低频率、只回少数好友、别群发，能降低概率，但不能消除。

## 目录结构

```
auto_reply.py              主程序：轮询、节流、调用模型、派发
qq_driver.py               QQ 窗口/控件驱动：识别、读写、发送
qq_friends.py              会话枚举与好友名单管理（list/pick/add/...）
qq_doctor.py               自检（doctor）
auto_reply_config.example.json   配置模板
images/                    表情包目录（自备图片）
tests/test_logic.py        离线单元测试
stop_auto_reply.bat        停止正在运行的脚本
设置回复好友.bat            打开好友勾选
自检.bat                    打开自检
```

## 开发

```powershell
python -m unittest discover -s tests -v
```

单元测试只覆盖纯逻辑（名字匹配、勾选解析），不碰 QQ。

## License

[MIT](LICENSE)
