# 视频号自动发布与交付

本项目用户已明确要求：视频完成后，用项目 `打开视频号.bat` 打开 Edge 视频号；需要登录时通过 Telegram 向本人发送实际登录二维码，等待网页确认登录成功，再按参考截图填写内容并点击发布。此授权适用于本技能每轮完成的视频，无需每轮再次询问是否发布。由 Codex 操作浏览器；本地 Python 只维护发布资料、进度和重复提交保护，不代替浏览器完成登录、上传或发表。

设置参考：[用户提供的发布页面截图](../assets/video-publish-settings.png)。按当前页面控件识别操作，不照搬截图坐标。浏览器工具或网站当次明确拒绝访问时，保留成片并记录实际阻塞；不绕过限制，也不把某次失败写成永久站点结论。

## 进度与恢复

全程持有本轮 `execution.token`，每次浏览器副作用前核对 `ownership`，失去执行权立即停止。`finish` 为新完成的视频创建独立的 `publish-progress.json`，制作状态仍为 done。发布或通知交付没有完成时，后续 `begin` 恢复同一批次，不重新出片。升级前没有发布进度的历史成片不会被自动补发。

```powershell
python codex_video.py publish-status --run <批次>
python codex_video.py publish-init --run <批次> --token <执行凭证> --file <发布资料JSON>
python codex_video.py publish-step --run <批次> --token <执行凭证> --file <事件JSON>
```

发布进度以 `publish-progress.json` 为准，不手改状态。`publish-init` 保存文案、设置和视频哈希，相同资料重复执行返回原进度，不重置已提交状态；首次提交前为满足页面字数限制修改文案，可再次初始化并重新核对表单，曾经提交后禁止覆盖原资料。事件 JSON 至少含 `status` 与真实 `evidence`，可附页面地址、截图路径、二维码事件键等必要依据；不要保存 Cookie 或登录凭据。进度依次为 `awaiting_publish`、`prepared`、`waiting_login`（需要登录时）、`uploading`、`ready`、`submitting`、`published`、`delivered`（通知交付完成）；提交前的阻塞记为 `blocked`，提交结果不明记为 `uncertain`。

- `done` 且发布未完成：直接恢复本节，不再生图、render 或清理素材。
- `waiting_login`：找回本次 Edge 页面和二维码通知回执，继续等登录；同一码不重复发。
- `uploading` 或 `ready`：重新读实际页面，确认文件和表单是否还在，不能仅按本地状态判断上传已完成。
- `submitting` 或 `uncertain`：先查平台结果或作品列表，禁止直接再次点击发布。取得匹配本条视频的成功记录则登记 published；只有明确确认未提交成功时，才从 uncertain 用 `confirmed_not_submitted: true` 和实际证据重新登记 ready；若仍是 submitting，先登记 uncertain。列表暂时没出现不能证明未提交。
- `published`：不再上传或发表，只补发尚未完成的通知。

## 准备发布资料

在本批次目录准备 `publish-info.json` 和便于复制的 `发布文案.txt`，根据实际选题写短标题、描述和话题，描述标明 AI 创作场景。不要把生成场景写成真实历史纪实、实地拍摄或真人经历。示例结构：

```json
{
  "run_id": "本轮批次",
  "video": "本批次实际成片绝对路径",
  "short_title": "地方还在，旧日难回",
  "description": "根据本轮内容创作的描述，标明 AI 创作场景",
  "hashtags": ["旧时光", "物是人非", "怀旧", "时光对照"],
  "settings": {
    "location": "不显示位置",
    "collection": "旧时光",
    "link": null,
    "activity": "不参与活动",
    "schedule": "不定时",
    "original_requested": true,
    "ai_label": "含AI生成内容"
  }
}
```

用 `publish-init` 登记后按返回的 `info` 填表，不能在重试时换成另一条视频或另一份文案。首次提交前需要按页面字数限制调整文案时，更新本轮资料并再次 publish-init，重新登记 uploading、核对表单并 ready；不能换视频或修改进度绕过检查。

## 打开 Edge、发送二维码并等登录

1. 复用已经由本轮打开的 Edge 视频号标签；没有时在项目目录执行用户提供的启动器：`& '.\打开视频号.bat'`。该脚本只打开 `https://channels.weixin.qq.com/platform`，不会自动上传或发布。
2. 用浏览器工具列出可控制页面，接入启动器打开的 Edge 标签。Edge 未连接时记录 blocked 并通过 Telegram 说明需接通 Edge 浏览器控制，不能声称已看到二维码或已登录；用户已指定改用 Edge，不自行换用其他浏览器。
3. 若已登录，记录当前视频号名称并进入发表页面。若显示登录二维码，保存当前真实页面截图为本批次的 `login-qr-<序号>.png`，用视觉检查确认二维码完整、清晰、未过期，随后登记 waiting_login。只发送登录所需截图，不包含浏览器其他页面或无关个人信息。
4. 加载 `$telegram-notify` 并运行 `status`。已 ready 时，用 `--kind document` 将二维码 PNG 发给已绑定本人私聊，避免图片压缩；说明这是本次视频号登录、需要本人用微信扫码并在手机确认。正文保存在本批次 `telegram-login-<序号>.txt`，事件键为 `time-echo:<run_id>:login-qr:<序号>`。保存工具的真实发送回执到 `notification-receipts.json`，追加记录，不能覆盖已有回执。通知未配置或发送失败时，保留 waiting_login 和二维码，在当前任务说明，不能声称手机已收到。
5. 发送后持续等待用户操作，每隔约 15 秒读取 Edge 的真实页面状态；每次等待不超过 60 秒。扫码提示、用户说“扫了”或 Telegram 回执都不等于登录成功，必须看到已登录后台和视频号身份才继续。无需读取 Telegram 回复。
6. 页面明确提示二维码过期时刷新取得新码、增加序号并发送一次；同一码不因轮询而重发。网络发送结果不确定时遵守通知技能的去重规则，不擅自换事件键补发。Edge 控制丢失或工具明确不可用时保存阻塞并报告；用户主动停止或任务中断则保留断点供下次恢复。

二维码发送命令：

```powershell
C:/Python313/python.exe skills/telegram-notify/scripts/telegram_notify.py send --event <本批次二维码事件键> --text-file <登录说明TXT> --kind document --file <真实二维码PNG>
```

截图保存、上传文件都必须使用当前工具实际支持的能力；无法导出二维码文件或操作文件选择器时，报告具体缺失能力，不宣称已完成，不猜测工具 API。

## 上传、填写并发表

1. 在已登录 Edge 中打开发布视频页面，登记 uploading，用文件上传控件选择 `info.video`。等待实际上传和处理完成，核对预览确为本轮视频；不要把仅选中文件当成上传成功。
2. 填入本轮视频描述、话题与短标题。话题控件若需要点选确认，完成实际选择；不填截图中的占位文字。字数、话题数量按当前页面提示调整，调整内容需保持本轮主题和 AI 标识。
3. 逐项设置：位置“不显示位置”；合集“旧时光”；链接留空；活动“不参与活动”；发表“不定时”；按用户要求勾选“声明原创”；视频标注“含AI生成内容”。合集不存在时报告具体问题，不擅自新建或选择其他合集。若页面出现额外协议或工具要求用户处理的步骤，保存已填内容并按实际要求处理。
4. 重新查看表单和预览，把实际账号、视频、文案与所有设置的核对结果写入 ready 事件。下面是结构示例，checks 必须由当前页面观察填写，不能复制示例当作已核对。

```json
{
  "status": "ready",
  "account": "实际视频号名称",
  "checks": {
    "video": true, "upload_complete": true, "description": true,
    "short_title": true, "location": true, "collection": true,
    "link": true, "activity": true, "schedule": true,
    "original": true, "ai_label": true
  },
  "evidence": "实际看到的文件、表单值和可点击的发布按钮；附本轮截图路径"
}
```

5. 点击发布前核对占用，先成功登记 submitting 事件，再点击一次“发布”。用户已授权这一步，不增加重复的发布确认。按页面提示完成正常发表流程并等待结果。
6. 页面显示发表成功，或作品列表出现与账号、本轮标题、视频及时间匹配的记录后，登记 published，并在事件 `receipt` 字符串中保存成功提示、作品 ID/地址或匹配记录及截图路径。若平台显示审核中，如实记录审核中，不能称已经公开可见；若明确拒绝发表或结果无法确认，登记 uncertain 并核实，不重复点击。
7. 将发布结果通过 Telegram 发给本人，事件键 `time-echo:<run_id>:published`，提供标题、账号、平台状态与可用作品链接。没有平台成功证据时，不能发送“发布成功”。

## 成片通知与结束

成片审核后可先发送完成消息与 MP4，再执行自动发布。完成消息应说明“视频已完成，接下来发布”，而不是“已经发布”。事件键继续使用 `time-echo:<run_id>:complete` 和 `time-echo:<run_id>:video`；正文分别保存到 `telegram-complete.txt`、`telegram-video.txt`。MP4 超过通知技能附件限制时保留本机文件并说明。

通知返回 sent/already_sent 后保存真实回执；不确定或失败按通知技能处理。通知失败不重做视频、不重发作品；published 批次恢复时仅补通知。成片完成消息、MP4 和发布结果通知都取得回执后，登记 delivered 事件：notification_receipts 包含 complete、video、published 三项，每项保留脚本返回的 status（sent 或 already_sent）与 message_id。视频因超过附件上限而未发送时，仅 video 可使用 status=skipped_oversize 并记录 reason；仍需成功发送说明与发布结果。只有 delivered 后下一次 begin 才开始新视频。后台状态无变化保持安静，只在二维码需要操作、完成、失败或其他实质变化时提醒。

提交前卡点用 publish-step 记 blocked，已提交但结果不确定用 uncertain。发送简短原因、保留的进度和用户需要做的一步操作，事件键 `time-echo:<run_id>:blocked:<原因短码>`，相同卡点不重复通知。成片后的问题不使用制作阶段 block 改坏 done 状态。完成交付或保存阻塞并通知后 release；正在等扫码时仍持有执行权。Codex 本身停止运行时不能保证继续等登录或发送提醒，下次从本批次恢复。
