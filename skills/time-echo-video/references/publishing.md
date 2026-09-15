# 视频号自动发布与交付

本项目用户已明确要求：视频完成后，由固定脚本打开 Edge 视频号；需要登录时通过 Telegram 向本人发送实际登录二维码，等待网页确认登录成功，再按参考截图填写内容并点击“发表”。此授权适用于本技能每轮完成的视频，无需每轮再次询问是否发布。Codex 负责准备本轮视频与发布资料，`social_upload_adapter.py` 负责登录判断、二维码通知、上传、填表、提交和平台结果检查；正常发布不由 Codex逐项操作网页。

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

在本批次目录准备 `publish-info.json` 和便于复制的 `发布文案.txt`。先根据本轮画面写一句简短、自然、有情绪的发布句，不写机械的内容说明。`description` 使用这句原文，可带逗号、句号等标点；`short_title` 使用同一句话去掉全部标点和空格后的版本，只能含 7~15 个汉字、字母或数字；话题只放在 `hashtags`，由发布器追加到视频描述。平台“视频标注”会声明虚构内容，不再把“AI 创作场景”等说明塞进描述。不要把生成场景写成真实历史纪实、实地拍摄或真人经历。示例结构：

```json
{
  "run_id": "本轮批次",
  "video": "本批次实际成片绝对路径",
  "short_title": "地方还在旧日难回",
  "description": "地方还在，旧日难回。",
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

1. 按项目 `social-upload-install.md` 安装固定 SHA、补丁与独立 `.venv-social`。运行 `.venv-social/Scripts/python.exe social_upload_adapter.py --smoke` 只验证版本、补丁及真实腾讯模块导入，不联网、不启动浏览器、不发送通知。
2. 运行 `.venv-social/Scripts/python.exe social_upload_adapter.py --run <批次> --token <执行凭证>`。真实 TencentVideo/tencent_setup 使用 Edge 和工作区共享 `.social-upload-account/social-account.json` 保存登录态；该文件是敏感会话数据，不放入进度事件或提交 Git。
3. 登录图片来自上游 qrcode_callback 的实际图片路径，经图片格式校验后以 Telegram document 子进程发送。持久 SHA256、事件键、sent/already_sent 回执用于同码去重；约每 60 秒刷新新码。默认上游最多检查 100 次（约 300 秒），外层 --login-timeout 默认 900 秒为总上限。每次回调及退出均清理二维码文件。
4. 上游负责真实上传；适配器在上游提交钩子内填写并验证简短描述、话题、无符号短标题、位置、合集、无链接、活动、定时、原创和视频标注。原创声明必须完成二次确认：勾选协议、等待确认按钮启用、点击“声明原创”并确认弹窗关闭。适配器还要确认页面没有标题错误且“发表”按钮未处于禁用状态；任一条件不成立都在提交前阻塞。
5. 状态机先持久保存 ready 和 submitting，然后只授权一次 click。上游补丁禁止发表重试与 JavaScript 补点；必须看到明确平台成功提示才登记 published。退出码 0、按钮消失或跳页均不是成功证据。submitting/uncertain 再次执行只返回 UNCERTAIN_SUBMISSION，不打开上传流程。
6. --dry-run 会真实上传填表，停在 ready；不是离线测试。离线测试使用 `python -m unittest discover -s tests -p 'test_*.py'`。发布后的 Telegram 结果通知仍按下节执行。

## 发布结果与交付

脚本登记 `published` 后，将发布结果通过 Telegram 发给本人，事件键 `time-echo:<run_id>:published`，提供标题、账号、平台状态与可用作品链接。没有平台成功证据时，不能发送“发布成功”。

## 成片通知与结束

成片审核后可先发送完成消息与 MP4，再执行自动发布。完成消息应说明“视频已完成，接下来发布”，而不是“已经发布”。事件键继续使用 `time-echo:<run_id>:complete` 和 `time-echo:<run_id>:video`；正文分别保存到 `telegram-complete.txt`、`telegram-video.txt`。MP4 超过通知技能附件限制时保留本机文件并说明。

通知返回 sent/already_sent 后保存真实回执；不确定或失败按通知技能处理。通知失败不重做视频、不重发作品；published 批次恢复时仅补通知。成片完成消息、MP4 和发布结果通知都取得回执后，登记 delivered 事件：notification_receipts 包含 complete、video、published 三项，每项保留脚本返回的 status（sent 或 already_sent）与 message_id。视频因超过附件上限而未发送时，仅 video 可使用 status=skipped_oversize 并记录 reason；仍需成功发送说明与发布结果。只有 delivered 后下一次 begin 才开始新视频。后台状态无变化保持安静，只在二维码需要操作、完成、失败或其他实质变化时提醒。

提交前卡点用 publish-step 记 blocked，已提交但结果不确定用 uncertain。发送简短原因、保留的进度和用户需要做的一步操作，事件键 `time-echo:<run_id>:blocked:<原因短码>`，相同卡点不重复通知。成片后的问题不使用制作阶段 block 改坏 done 状态。完成交付或保存阻塞并通知后 release；正在等扫码时仍持有执行权。Codex 本身停止运行时不能保证继续等登录或发送提醒，下次从本批次恢复。

登录态跨批复用；临时二维码只允许共享账户目录内固定前缀 PNG，清理不会删除 storage state。上游登录循环使用 asyncio.sleep，外层超时可取消；Telegram 同步 subprocess 通知本身最多等待60秒，因此正在发送时总超时可能最多延后约60秒。smoke 不证明浏览器已能启动。
