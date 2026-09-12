# 本地命令

所有命令在 `D:/AI创作生图项目` 运行。PowerShell 示例中的 `<批次>`、`<编号>` 要换成真实返回值。文本和 JSON 文件使用 UTF-8，通过文件参数传入，避免 shell 引号破坏提示词。脚本只负责本地工作，调用生图与看图仍由 Codex 完成。`begin` 返回的 `execution.token` 是本轮执行凭证，以下 `<执行凭证>` 均使用这个值；不要使用其他任务的身份或凭证。

```powershell
python codex_video.py begin
python codex_video.py history
python codex_video.py status --run <批次>
python codex_video.py ownership
python codex_video.py plan --run <批次> --pair 1 --file <选题JSON> --token <执行凭证>
python codex_video.py attempt --run <批次> --pair 1 --kind old --file <完整提示词TXT> --token <执行凭证>
# 接着调用内置 image_gen，并实际查看工具生成的图片。
python codex_video.py record --run <批次> --attempt <编号> --image <实际本地图片路径> --file <审核JSON> --token <执行凭证>
# 审核 old 合格后，重复 attempt / image_gen / 看图 / record，kind 改为 now。
python codex_video.py fail --run <批次> --attempt <编号> --reason '请求中断或真实错误' --token <执行凭证>
python codex_video.py replace --run <批次> --pair 1 --reason '多次修正后仍未达到时间变化要求' --token <执行凭证>
python codex_video.py block --run <批次> --reason '明确阻塞原因' --token <执行凭证>
python codex_video.py render --run <批次> --token <执行凭证>
python codex_video.py finish --run <批次> --file <成片审核JSON> --token <执行凭证>
```

`begin`、`takeover`、`release` 与所有修改命令通过 `Workspace.execute` 在同一项目文件锁中执行；旧的 `Workspace` 制作方法是内部实现，不得绕过命令入口直接调用。

`attempt` 在调用前计数并返回 id；失败也消耗一次，避免中断后无限调用。每次最多一个 pending，请求返回后及时登记。`record` 自动复制、标准化成 PNG 并保存哈希；后续审核通过的文件若被修改会拒绝合成。失败图保留到出片清理。

选题 JSON：

```json
{
  "title": "末班车再也不从这里开了",
  "old_life": "母亲和孩子拎着编织袋在候车廊等车回家",
  "loss": "过去可以等车回家的地方，已经不再发车",
  "change_route": "停运后改成储物间",
  "caption": "以前嫌车来得慢。\n后来，这里再也没有车来了。",
  "anchors": ["廊柱与低平雨棚轮廓", "右侧台阶位置"],
  "changes": ["线路牌拆走留色差", "窗口换成封闭百叶板", "长凳拆走留下修补痕"]
}
```

旧照审核 JSON（以下只是结构示例，不得未经看图直接套用通过）：

```json
{
  "passed": true,
  "checks": {"realism": true, "era": true, "anatomy": true, "composition": true, "no_text": true},
  "observations": "写当前图片实际可见的人物、相纸、场景结构与画质情况"
}
```

今照在 checks 中增加 `same_place`、`time_change`、`loss` 布尔项，并增加 `visible_changes` 字符串数组（至少三项）及 `loss_evidence` 字符串。不通过项填 false，passed=false，写清返工原因。

成片审核 JSON：

```json
{"framing": true, "years": true, "order": true, "transitions": true, "observations": "实际查看预览和抽样转场后的观察；不能声称检查过未查看内容"}
```

`render` 是同步编码命令，命令工具如返回会话 ID，继续轮询该会话，不重复启动。锁覆盖编码过程，异常退出后操作系统释放锁；重新 render 从已审核素材重做编码，音乐和文件名保持不变。输出检查通过后才将 partial MP4 改名。已有成片会返回原文件，不产生重复视频。
默认保存目录 `outputs/codex-time-echo/<批次>/`，以真实命令输出为准。`preview.jpg` 从成片抽帧，便于检查每组的最终画面；必要时用 FFmpeg 抽取转场附近帧，随后用 view_image 查看。
`finish` 成功后只清理本轮 assets 中的 PNG，保留 MP4、预览、manifest、提示词与审核文件。日常产物已被 Git 忽略；运行技能并未修改项目代码时不需要制造空提交。

配置 `codex-video.json` 只影响新批次，未完成批次使用其创建时保存的配置。默认无需改配置即可运行。次数用尽后不改 manifest 偷偷重置预算。


## 执行权核验与接管

整轮占用保存在输出目录的 `execution.json`，与对话历史分开。单次命令锁仍保护读写和编码；整轮占用覆盖思考、生图工具等待、审核和交付间隙，不按时间自动过期。`ownership`、`status`、`history` 是只读命令。占用冲突返回退出码 2 和 `status=busy`；本地命令锁正忙时 `execution` 可能为空，不能据此判断无人执行。

- 同一执行者继续本轮时可用 `begin --token <执行凭证>`，即使视频已完成也返回原批次；凭证遗失时只可从 `ownership` 取回与当前任务 ID 完全相符的凭证。
- 正常结束、暂停或保存阻塞后，用 `release --run <批次> --token <执行凭证> --reason '已保存进度并结束本轮执行'`。释放不改图片、次数和制作状态。存在 pending 请求时释放会被拒绝，先处理真实结果。
- A 已释放：B 直接 `begin`。未完成批次从原进度继续；已完成批次创建下一批。
- A 未释放：B 读取占用，再用 Codex `wait_threads`（`timeoutMs: 0`）查询 `owner_thread_id` 的当前状态；必要时用 `read_thread` 核实最新一轮和工具是否已结束。原任务仍运行、等待工具、状态未知、查询失败或工具不可用时，不接管，不因记录陈旧而判断已停止，也不主动打断原任务。
- 只有最新状态明确显示原任务已空闲、完成或中断且没有仍运行的工具时，才准备下面的核验文件并执行 `takeover`。这是 Codex 对真实工具结果的核验记录，脚本不能独立查询任务状态；禁止根据时间、猜测或下方示例填写“已停止”。

核验文件保存在当前任务自己的临时目录，不能覆盖原批次文件。占用中的 `owner_thread_id`、`token`、`revision` 原样带入；`checked_at` 为实际核验时间（ISO 8601，含时区），`source` 为实际使用的 `wait_threads` 或 `read_thread`，`evidence` 摘录当前状态与工具已结束的依据。`observed_status` 仅允许 `idle`、`completed`、`interrupted`，必须对应实际结果。

```json
{
  "owner_thread_id": "原执行任务ID",
  "token": "读取到的原执行凭证",
  "revision": 1,
  "observed_status": "interrupted",
  "source": "wait_threads",
  "checked_at": "2026-09-12T12:00:00+00:00",
  "evidence": "填写实际工具返回的当前任务状态及工具已结束依据；不能直接使用示例"
}
```

```powershell
python codex_video.py takeover --run <批次> --file <核验JSON>
python codex_video.py release --run <批次> --token <执行凭证> --reason '已保存进度并结束本轮执行'
```

核验超过 60 秒、原执行者再次 begin 或进行了修改操作，接管会被拒绝，须重新查询状态。成功后使用返回的新 `execution.token`；原凭证立即失效。接管保留原 manifest、pending 请求、图片、年份与预算，先找回未登记结果，再决定是否登记失败；不自动重发生图。旧任务后来恢复也必须重新检查占用，不得更改身份、改写 `execution.json` 或删除锁文件夺回执行权。

没有 `CODEX_THREAD_ID` 的环境必须用 `--owner <当前真实任务ID>`；环境已有身份时不能用此参数冒用另一个任务。升级前未建立占用记录的旧批次仍可由首次 `begin` 认领，启用前应确保旧版执行已停止；旧版本不具备这套凭证检查，不能与新版同时执行。
