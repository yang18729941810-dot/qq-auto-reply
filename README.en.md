# QQ Auto-Reply (Windows)

[中文](README.md) · English

An auto-reply bot for the QQ desktop client. It reads chats with UI Automation and types replies with simulated keystrokes — no process injection, no protocol reverse-engineering, no packet capture. Just the things a person sitting at the keyboard would do.

> **Disclaimer**
> Automating the QQ client may violate Tencent's Terms of Service and carries a risk of account restrictions or bans.
> The author is not responsible for any account penalty, data loss, or other consequences. Use it on your own account, at your own risk.
> Do not use it for bulk messaging, marketing, or harassment. Provided "as is" under the MIT license, without warranty.

## What it does

- Watches chosen friends, calls any OpenAI-compatible API, sends the reply back
- Whitelist picked from the real conversation list; changes apply to a running bot
- Optional stickers: the model appends `[表情:category]`, the bot sends one image
- Optional vision: forwards incoming images to a multimodal model
- Stays quiet while you are typing in that chat; human-like pacing and rate limits
- Widgets are found by structure, not by hard-coded control names, so it is not tied to one QQ build

## How it works

1. **Read** — UI Automation (RawView) reads conversation names and the last-message preview; a changed preview means a new message.
2. **Write** — brings the chat window to the foreground, types with `SendInput`, presses the send shortcut, then checks the input box is empty to confirm.

Before sending, it verifies the window currently shows the intended conversation and aborts otherwise.

## Requirements

- Windows 10 / 11, Python 3.9+ (developed on 3.13)
- QQ 9.x / QQ NT / TIM desktop client, logged in
- QQ and the script must run at the same privilege level (both normal, or both elevated)

Verified on QQ 9.7.25 (legacy DirectUI client). QQ NT (9.9+, Chromium) and TIM use a structural fallback that has **not** been verified on real hardware — run the self-check after switching versions.

## Quick start

```powershell
git clone https://github.com/yang18729941810-dot/qq-auto-reply.git
cd qq-auto-reply
pip install -r requirements.txt

copy auto_reply_config.example.json auto_reply_config.json
notepad auto_reply_config.json      # set api_key / base_url / model
python auto_reply.py
```

Run the self-check first:

```powershell
python auto_reply.py doctor
```

## Choosing who gets replies

```powershell
python auto_reply.py list     # every conversation, with whitelist/group marks
python auto_reply.py pick     # pick by number, range, all/none, or by name
python auto_reply.py check    # are the whitelisted friends still there?
```

Also `add`, `del`, `ignore`, `unignore`, `show`. Names are matched against the remark shown in the conversation list; matching ignores spaces and zero-width characters and never does substring matching.

`targets` takes precedence over `listen_all`. Whitelist mode does **not** filter groups — adding a group means replying to that group.

With multiple accounts signed in, `account_markers` selects the instance whose conversation list contains a given chat.

## Config highlights

| Key | Meaning |
|---|---|
| `api_key` / `base_url` / `model` | any OpenAI-compatible endpoint; empty key = echo mode |
| `targets` | whitelist; empty means `listen_all` decides |
| `listen_all` | reply to every conversation (risky, off by default) |
| `account_markers` | identify the right QQ instance when multi-account |
| `ignore` | blacklist, highest priority |
| `send_key` | `auto` / `enter` / `ctrl+enter`; auto learns the client's setting |
| `min_reply_interval`, `global_reply_limit`, `quiet_hours` | throttling |
| `manual_typing_enabled` | pause while you type in that chat |
| `manual_mode` | listen only, never reply |
| `vision_enabled` | image understanding; uploads images to your model provider |

`api_key`, prompts, model parameters, `targets`, `ignore`, `listen_all`, `send_key` and `account_markers` are hot-reloaded; throttling/vision settings need a restart.

## Stickers

Put images under `images/<category>/`; categories are whatever the model may write. When the reply ends with `[表情:category]`, the bot strips the tag, sends the text, then one image. No images are bundled.

## Known limitations

- New messages are detected from preview changes, so two identical consecutive messages may be seen as one
- Message direction is not exposed; the bot's own echoes are claimed by content matching
- Group vs. direct chat is a heuristic based on the `sender: ` preview prefix
- QQ NT needs Chromium to build an accessibility tree; the bot pokes it with `WM_GETOBJECT`, which is not an official interface
- Two accounts with same-named chats cannot be told apart by `account_markers`

## License

[MIT](LICENSE)
