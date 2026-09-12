---
name: telegram-notify
description: 在 Windows 本机通过已绑定的 Telegram 机器人给用户本人发送任务完成、失败、卡点或需操作的通知，支持文字、图片、MP4 和文件。用于用户要求飞机提醒、远程接收成品或已授权任务的异常通知。
---

# Telegram 通知

使用本技能目录下 `scripts/telegram_notify.py`，以本机 Python 3 执行（当前机器 `C:/Python313/python.exe`）。不需要模型 API Key；通知使用 Telegram 官方 Bot API。无需常驻服务。

## 首次配置

1. 运行 `status`。`not_configured` 时让用户在官方 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建专用机器人。一次只问一个缺失信息。不要让用户把 Token 发进聊天、命令行或项目文件。
2. 运行 `setup-gui` 让用户在本机密码输入框粘贴 Token。可让用户在 PowerShell 执行该命令；从工具启动窗口时使用 `Start-Process -WindowStyle Hidden` 启动 Python，Tk 输入框仍供用户交互。不要截图或读取输入框。脚本验证机器人并用当前 Windows 用户的 DPAPI 加密保存凭据，位置 `%LOCALAPPDATA%/CodexNotifications/telegram/credentials.dpapi`，不在仓库内。已有 webhook 会停止，不删除或替换原服务。
3. 运行 `bind-start`，把返回的 `https://t.me/...` 链接给用户，让其在本人 Telegram 中打开并点击开始。此链接用于绑定收件人，不转发给别人。随后运行 `bind-poll`，单次最多约 30 秒；未操作时不要紧密轮询，等用户反馈再查。过期后重新 `bind-start`。只接受携带一次性随机码的本人私聊，不凭用户名猜 chat_id。
4. `ready` 后按已授权目的发送一条连接测试，取得消息编号才报告已发出。用户确认手机收到后，才称端到端测试通过。没有凭据或没有完成绑定时明确说明未接通。

配置只在原 Windows 用户下可解密；换电脑/用户需重新配置。该机器人作为专用通知机器人，不与别的轮询程序共用。技能可发送消息，不提供 Telegram 回复遥控、持续收信或自动唤醒；用户后续操作通常仍在 Codex 中告知。

## 发消息

先将要发送的正文保存为 UTF-8 文本，再执行：

```powershell
C:/Python313/python.exe <技能目录>/scripts/telegram_notify.py send --event <任务唯一标识>:<事件> --text-file <正文绝对路径>
C:/Python313/python.exe <技能目录>/scripts/telegram_notify.py send --event <任务唯一标识>:video --text-file <简短说明绝对路径> --kind video --file <成品MP4绝对路径>
```

图片使用 `--kind photo`（PNG/JPG，10 MB 内），原样传二维码或文档可使用 `--kind document`（50 MB 内），MP4 使用 `--kind video`（50 MB 内）。长描述单独发文字，附件说明不超过 1024 字符，文字不超过 4096 字符。超限文件留在本地并通知，不擅自公开上传网盘。二维码只能来自实际允许访问且用户授权的页面；若来源工具禁止访问，不通过通知脚本、其他浏览器或网络请求绕过。

正文用简体中文写清任务、结果或卡点、已经保留的进度、需要用户做的一步操作。仅发送当前任务必要的信息与获授权的附件。此技能绑定本人私聊，不能据此联系其他人；普通任务是否通知以用户请求/既有授权为准，不把本项目约定泛化到所有会话。

当前用户已授权时光对照出片流程的完成、失败、需操作通知及成品发送，不需每次重新确认。其他任务中用户要求“卡住发飞机”时也可复用。后台无实质变化保持安静，不为每个内部步骤发进度。

## 回执与恢复

- `sent` 表示 Telegram 返回消息编号，`already_sent` 表示相同事件已经发送；都不意味着用户已阅读。
- 同一实际事件重复执行时复用同一事件键。正文需要变化时先判断是否真的发生了新事件，不为避开去重而随意换键。
- 发送前持久化 `pending`，成功记录 `sent`。网络中断或进程终止留下 `uncertain/pending` 时不能自动重发；请用户先在 Telegram 核对。确认没有收到并要求重发后，才用带 `retry-1` 的新事件键重发。
- 明确拒绝记为 `failed`，至少间隔 60 秒并解决原因后最多重试一次。无凭据、网络不可达、Token 失效或通知发送失败时，继续保留原任务成果，在当前 Codex 任务报告；不要重新生成视频，不创建轮询任务刷请求。
- 回执库 `deliveries.sqlite3` 与加密凭据在同一目录，只存事件键、摘要、状态和消息编号，不记录 Token、正文或附件。不要输出原始 HTTP 异常、请求 URL 或配置内容。

协议参考：[Telegram Bot API](https://core.telegram.org/bots/api)。
