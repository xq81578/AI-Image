# 视频号自动发布与交付

本项目用户已明确要求：视频完成后，由固定脚本打开 Edge 视频号；需要登录时通过 Telegram 向本人发送实际登录二维码，等待网页确认登录成功，再按参考截图填写内容并点击“发表”。此授权适用于本技能每轮完成的视频，无需每轮再次询问是否发布。Codex 负责准备本轮视频与发布资料，`publish_video.py` 负责登录判断、二维码通知、上传、填表、提交和平台结果检查；正常发布不由 Codex逐项操作网页。

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
    "ai_label": "内容为虚构剧情，仅供娱乐"
  }
}
```

用 `publish-init` 登记后按返回的 `info` 填表，不能在重试时换成另一条视频或另一份文案。首次提交前需要按页面字数限制调整文案时，更新本轮资料并再次 publish-init，重新登记 uploading、核对表单并 ready；不能换视频或修改进度绕过检查。

## 固定脚本执行发布

1. 确认已安装 `requirements-publish.txt` 中的依赖；当前机器使用 `C:/Python313/python.exe`。需要只检查连接和当前页面时运行 `C:/Python313/python.exe publish_video.py --probe`，该模式不发送二维码、不上传、不填表、不发布。
2. 正常发布只运行一次：`C:/Python313/python.exe publish_video.py --run <批次> --token <执行凭证>`。脚本会按需调用 `打开视频号.bat`，以 `%LOCALAPPDATA%/CodexVideoPublisher/EdgeProfile` 启动独立、可持久保存登录状态的 Edge，并打开 `https://channels.weixin.qq.com/platform/post/create`。
3. 页面跳到 `https://channels.weixin.qq.com/login.html` 时，脚本处理“加载失败，点击重试”，从微信 OAuth iframe 截取可见二维码，以 `document` 调用 Telegram CLI发送，随后等待网页离开登录页。仍未登录时每隔 60 秒重新加载登录页，截取二维码并通过回调发送新码，不限制刷新次数；总等待时间仍受 `--login-timeout` 限制（默认 900 秒），刷新不会重置总超时。同一码复用同一事件键。每次获取新码前清理旧二维码截图和临时说明文件，登录成功、超时或异常退出登录等待时也会清理，包括未完成的截图文件。
4. 页面已登录时，脚本直接进入发表动态页，上传 `info.video`，填写描述、话题、短标题和本项目固定设置，等待上传完成及“发表”按钮可用。
5. 脚本通过现有 `codex_video.py` 状态机依次登记 `uploading`、`ready`、`submitting`、`published`。`submitting` 必须先落盘才点击一次“发表”；点击后没有明确结果时登记 `uncertain`，后续执行不会再次点击。
6. 首次校准页面可使用 `--dry-run`：它会真实上传并填表，但停在 `ready`，不点击“发表”。正常定时任务不要使用 `--dry-run`。
7. 脚本输出单行 JSON。`PUBLISHED` 表示已有平台成功证据；`READY` 表示试运行停在提交前；`LOGIN_TIMEOUT`、`QR_NOT_FOUND`、`FORM_CONTROL_NOT_FOUND` 等表示本轮已保留进度；`UNCERTAIN_SUBMISSION` 表示提交结果不明，只能核对平台作品，禁止直接重跑提交。

```powershell
C:/Python313/python.exe publish_video.py --probe
C:/Python313/python.exe publish_video.py --run <批次> --token <执行凭证>
C:/Python313/python.exe publish_video.py --run <批次> --token <执行凭证> --dry-run
```

## 发布结果与交付

脚本登记 `published` 后，将发布结果通过 Telegram 发给本人，事件键 `time-echo:<run_id>:published`，提供标题、账号、平台状态与可用作品链接。没有平台成功证据时，不能发送“发布成功”。

## 成片通知与结束

成片审核后可先发送完成消息与 MP4，再执行自动发布。完成消息应说明“视频已完成，接下来发布”，而不是“已经发布”。事件键继续使用 `time-echo:<run_id>:complete` 和 `time-echo:<run_id>:video`；正文分别保存到 `telegram-complete.txt`、`telegram-video.txt`。MP4 超过通知技能附件限制时保留本机文件并说明。

通知返回 sent/already_sent 后保存真实回执；不确定或失败按通知技能处理。通知失败不重做视频、不重发作品；published 批次恢复时仅补通知。成片完成消息、MP4 和发布结果通知都取得回执后，登记 delivered 事件：notification_receipts 包含 complete、video、published 三项，每项保留脚本返回的 status（sent 或 already_sent）与 message_id。视频因超过附件上限而未发送时，仅 video 可使用 status=skipped_oversize 并记录 reason；仍需成功发送说明与发布结果。只有 delivered 后下一次 begin 才开始新视频。后台状态无变化保持安静，只在二维码需要操作、完成、失败或其他实质变化时提醒。

提交前卡点用 publish-step 记 blocked，已提交但结果不确定用 uncertain。发送简短原因、保留的进度和用户需要做的一步操作，事件键 `time-echo:<run_id>:blocked:<原因短码>`，相同卡点不重复通知。成片后的问题不使用制作阶段 block 改坏 done 状态。完成交付或保存阻塞并通知后 release；正在等扫码时仍持有执行权。Codex 本身停止运行时不能保证继续等登录或发送提醒，下次从本批次恢复。
