# 视频号自动发布与交付

本项目用户已明确要求：视频完成后，由固定脚本打开 Chrome 视频号；需要登录时通过 Telegram 向本人发送实际登录二维码，等待网页确认登录成功，再按参考截图填写内容并点击“发表”。此授权适用于本技能每轮完成的视频，无需每轮再次询问是否发布。Codex 负责准备本轮视频与发布资料，`social_upload_adapter.py` 负责登录判断、二维码通知、上传、填表、提交和平台结果检查；正常发布不由 Codex逐项操作网页。

## 发布入口约束

以下要求仅适用于本技能的视频号发布与恢复：

- 正式发布只执行 `.venv-social/Scripts/python.exe social_upload_adapter.py --run <批次> --token <执行凭证>`。Chrome 的启动或复用由该入口负责，必要时调用 `打开视频号.bat`；登录、二维码截取、上传、填表、提交和结果核验均交给脚本。
- 不得另用 `cua_repl` 等浏览器工具打开视频号、测试访问、检查登录、截图或手工发布，也不得把这些操作作为固定入口的前置检查。核验历史阻塞同样不能增加浏览器预检；`ownership`、`publish-status` 与离线 `--smoke` 仍按原流程使用。
- 本节后续涉及查看实际页面、等待登录及核对作品列表的步骤均由固定脚本承担。脚本报告阻塞时按现有状态机保存并说明，不自行用浏览器工具补检、补点或换入口；遇到指令冲突先说明具体来源，不擅自改换流程。
- 在现有发布事件的 `evidence` 与结果报告中区分实际执行入口、返回状态和平台证据。未执行正式脚本时明确写“未执行正式发布脚本”及原因，不能把浏览器工具拒绝写成脚本失败，也不能把 `--smoke` 离线检查写成正式发布验证。

设置参考：[用户提供的发布页面截图](../assets/video-publish-settings.png)。由固定脚本按当前页面控件识别操作，不照搬截图坐标。浏览器工具或网站当次明确拒绝访问时，保留成片并记录实际阻塞；不绕过限制，也不把某次失败写成永久站点结论。

## 进度与恢复

全程持有本轮 `execution.token`，每次浏览器副作用前核对 `ownership`，失去执行权立即停止。`finish` 为新完成的视频创建独立的 `publish-progress.json`，制作状态仍为 done。发布或通知交付没有完成时，后续 `begin` 恢复同一批次，不重新出片。升级前没有发布进度的历史成片不会被自动补发。

```powershell
python codex_video.py publish-status --run <批次>
python codex_video.py publish-init --run <批次> --token <执行凭证> --file <发布资料JSON>
python codex_video.py publish-step --run <批次> --token <执行凭证> --file <事件JSON>
```

发布进度以 `publish-progress.json` 为准，不手改状态。`publish-init` 保存文案、设置和视频哈希，相同资料重复执行返回原进度，不重置已提交状态；首次提交前为满足页面字数限制修改文案，可再次初始化并重新核对表单，曾经提交后禁止覆盖原资料。事件 JSON 至少含 `status` 与真实 `evidence`，可附页面地址、截图路径、二维码事件键等必要依据；不要保存 Cookie 或登录凭据。进度依次为 `awaiting_publish`、`prepared`、`waiting_login`（需要登录时）、`uploading`、`ready`、`submitting`、`published`、`delivered`（通知交付完成）；提交前的阻塞记为 `blocked`，提交结果不明记为 `uncertain`。

- `done` 且发布未完成：直接恢复本节，不再生图、render 或清理素材。
- `waiting_login`：找回本次 Chrome 页面和二维码通知回执，继续等登录；同一码不重复发。
- `uploading` 或 `ready`：重新读实际页面，确认文件和表单是否还在，不能仅按本地状态判断上传已完成。
- `submitting` 或 `uncertain`：先查平台结果或作品列表，禁止直接再次点击发布。取得匹配本条视频的成功记录则登记 published；只有明确确认未提交成功时，才从 uncertain 用 `confirmed_not_submitted: true` 和实际证据重新登记 ready；若仍是 submitting，先登记 uncertain。列表暂时没出现不能证明未提交。
- `published`：不再上传或发表，只补发尚未完成的通知。

## 准备发布资料

在本批次目录准备 `publish-info.json` 和便于复制的 `发布文案.txt`。成片审核完成后，根据本轮全部画面的共同主题拟定发布句。文案以引起普通观众共鸣为第一目标，使用自然、朴素、像日常交谈一样的表达，让观众容易联想到自己的村庄、童年或熟悉的人。优先采用“以前与现在”“热闹与冷清”“地方还在但人少了”等具体的人间变化，不刻意追求文学感，不堆砌“岁月、旧梦、重逢、归途、光阴”等抽象抒情词，也不写机械的内容说明。不得为了煽情虚构真实经历或把生成场景说成历史纪实、实地拍摄或真人经历。

先拟定三个候选句，再选择最符合以下条件的一句：普通人一看就懂，读起来像自然说出口的话，能让观众产生“我们村以前也是这样”的联想，准确概括整条视频而非只对应一个场景，且去除标点和空格后为 7~15 个汉字、字母或数字。`description` 使用选中句子的原文，可带逗号、句号等标点；`short_title` 使用同一句话去掉全部标点和空格后的版本；话题只放在 `hashtags`，由发布器追加到视频描述。平台“视频标注”会声明虚构内容，不再把“AI 创作场景”等说明塞进描述。

推荐“以前这里很热闹，现在没人了。”“村子还在，熟悉的人少了。”“这些地方，也曾经挤满了人。”这类朴素口语；避免“旧地方还在，等一场不会来的重逢。”“岁月斑驳了故乡的旧梦。”“光阴深处，再也回不去的归途。”这类刻意文学化表达。示例结构：

```json
{
  "run_id": "本轮批次",
  "video": "本批次实际成片绝对路径",
  "short_title": "以前这里很热闹现在没人了",
  "description": "以前这里很热闹，现在没人了。",
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

用 `publish-init` 登记后按返回的 `info` 填表，不能在普通重试时换成另一条视频或另一份文案。首次提交前需要按页面字数限制调整文案时，更新本轮资料并再次 publish-init，重新登记 uploading、核对表单并 ready。若提交结果曾为 uncertain，只有先取得明确的未提交证据并以 `confirmed_not_submitted: true` 恢复 ready，才允许修正文案后重新 publish-init；视频仍禁止更换，不能修改进度绕过检查。

## 固定脚本执行发布

1. 按项目 `social-upload-install.md` 安装固定 SHA、补丁与独立 `.venv-social`。运行 `.venv-social/Scripts/python.exe social_upload_adapter.py --smoke` 只验证版本、补丁及真实腾讯模块导入，不联网、不启动浏览器、不发送通知。
2. 运行 `.venv-social/Scripts/python.exe social_upload_adapter.py --run <批次> --token <执行凭证>`。脚本连接 `打开视频号.bat` 启动的持久 Chrome，在同一个页面完成登录、上传和结果核验；登录态同时保存在 Chrome 用户目录和工作区共享 `.social-upload-account/social-account.json`。脚本结束时只断开自动化连接，不主动关闭 Chrome、页面或浏览器上下文；这些会话数据均不放入进度事件或提交 Git。
3. 登录图片来自上游 qrcode_callback 的实际图片路径，经图片格式校验后以 Telegram document 子进程发送。持久 SHA256、事件键、sent/already_sent 回执用于同码去重；约每 60 秒刷新新码。适配器按 --login-timeout 设置登录轮询次数，每 3 秒检查一次，默认总等待上限为 900 秒。每次回调及退出均清理二维码文件。
4. 上游负责真实上传；适配器在上游提交钩子内填写并验证简短描述、话题、无符号短标题、位置、合集、无链接、活动、定时、原创和视频标注。原创声明必须完成二次确认：勾选协议、等待确认按钮启用、点击“声明原创”并确认弹窗关闭。适配器还要确认页面没有标题错误且“发表”按钮未处于禁用状态；任一条件不成立都在提交前阻塞。
5. 状态机先持久保存 ready 和 submitting，然后只授权一次 click。上游补丁禁止发表重试与 JavaScript 补点。没有明确平台成功提示时，脚本直接在同一页面进入作品列表，按完整文案、话题和最近30分钟发布时间核对；找到匹配作品才登记 published，找不到才进入 uncertain，并让 Chrome 停留在列表页供人工核对。submitting/uncertain 再次执行仍禁止重复上传或点击发表。
6. --dry-run 会真实上传填表，停在 ready；不是离线测试。离线测试使用 `python -m unittest discover -s tests -p 'test_*.py'`。发布后的 Telegram 结果通知仍按下节执行。

## 发布结果与交付

脚本登记 `published` 后，将发布结果通过 Telegram 发给本人，事件键 `time-echo:<run_id>:published`，提供标题、账号、平台状态与可用作品链接。没有平台成功证据时，不能发送“发布成功”。

## 成片通知与结束

成片审核后可先发送完成消息与 MP4，再执行自动发布。完成消息应说明“视频已完成，接下来发布”，而不是“已经发布”。事件键继续使用 `time-echo:<run_id>:complete` 和 `time-echo:<run_id>:video`；正文分别保存到 `telegram-complete.txt`、`telegram-video.txt`。MP4 超过通知技能附件限制时保留本机文件并说明。

通知返回 sent/already_sent 后保存真实回执；不确定或失败按通知技能处理。通知失败不重做视频、不重发作品；published 批次恢复时仅补通知。成片完成消息、MP4 和发布结果通知都取得回执后，登记 delivered 事件：notification_receipts 包含 complete、video、published 三项，每项保留脚本返回的 status（sent 或 already_sent）与 message_id。视频因超过附件上限而未发送时，仅 video 可使用 status=skipped_oversize 并记录 reason；仍需成功发送说明与发布结果。只有 delivered 后下一次 begin 才开始新视频。后台状态无变化保持安静，只在二维码需要操作、完成、失败或其他实质变化时提醒。

提交前卡点用 publish-step 记 blocked，已提交但结果不确定用 uncertain。发送简短原因、保留的进度和用户需要做的一步操作，事件键 `time-echo:<run_id>:blocked:<原因短码>`，相同卡点不重复通知。成片后的问题不使用制作阶段 block 改坏 done 状态。完成交付或保存阻塞并通知后 release；正在等扫码时仍持有执行权。Codex 本身停止运行时不能保证继续等登录或发送提醒，下次从本批次恢复。

登录态跨批复用；临时二维码只允许共享账户目录内固定前缀 PNG，清理不会删除 storage state。上游登录循环使用 asyncio.sleep，外层超时可取消；Telegram 同步 subprocess 通知本身最多等待60秒，因此正在发送时总超时可能最多延后约60秒。smoke 不证明浏览器已能启动。
