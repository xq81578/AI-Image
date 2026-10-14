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

选题 JSON（以下只演示字段结构，不作为固定选题模板）：

```json
{
  "old_year": 2004,
  "title": "本组标题",
  "old_life": "旧照中仍在运行的具体乡土生活或使用状态",
  "loss": "多年后消失的生活、关系或功能",
  "change_route": "主体原功能退出的现实路径，以及外围各自继续使用、维护、更新或失管的路径",
  "first_glance_contrast": "不看年份与文案时，主体或前景中第一眼可见的强反差",
  "long_term_neglect": "符合年份、材料和气候的功能退出、维护中断与自然演替链",
  "caption": "根据本组实际画面写的两行文案",
  "anchors": ["地点连续线索一", "地点连续线索二"],
  "changes": [
    "[功能] 旧照中的具体活动如何终止、迁移或改作，并留下可见证据",
    "[结构/维护] 建筑材料、门窗、屋面、铺装或维护状态的持久变化",
    "[环境/设施/用途] 主体周边的环境、基础设施、管理方式或空间用途变化",
    "[环境/设施/用途] 外围：可见邻屋与环境的位置、旧状态 → 预期新状态及原因"
  ]
}
```

`old_year` 由选题决定，必须与本批 `new_year` 至少相隔十年。默认使用 2000 年以后；仅在年代质感与地点连续性可靠时使用 1980、1990 年代。旧批次或旧调用未提供 `old_year` 时保留 `begin` 创建的兼容年份。
`first_glance_contrast` 与 `long_term_neglect` 是必填字符串，不能只写“更空、没人、关门”；前者说明普通预览尺寸中位于主体或前景的显著变化，后者说明至少两项当天清场无法造成的持久失管痕迹及其因果。`changes` 必须分别包含以 `[功能]`、`[结构/维护]`、`[环境/设施/用途]` 开头的项目，且至少两项是持久痕迹。多人、桌椅、工具同时消失只能合并算一项 `[功能]` 变化；灰尘、调色、单独加草或单独加裂缝不能独立计数。`change_route` 写清导致变化的现实路径；若新桥、搬迁、设施替代或改作等原因无法在今照中直接看见，也看不见其结果，就不能依赖文案补足。旧式计划缺少新字段或三类前缀时不得开始今照请求，应通过 `replace` 换题并按新结构重新策划，不能静默绕过。

外围策划沿用 `change_route` 与 `changes`，按 [creative-review.md](creative-review.md) 分别交代主体与外围的路径；有可辨认的外围房屋和环境时单列对应项目，不能用主体长草替代。合理稳定、远景细节不可辨或没有可见外围时，在 `change_route` 如实说明，省略不存在的外围变化项；不为凑示例强行加建筑或工程。

旧照审核 JSON（以下只是结构示例，不得未经看图直接套用通过）：

```json
{
  "passed": true,
  "checks": {"realism": true, "era": true, "anatomy": true, "composition": true, "no_text": true, "rural": true, "lived_in": true},
  "observations": "写当前图片实际可见的人物、相纸、场景结构与画质情况"
}
```

今照在 checks 中使用 `rural`、`same_place`、`time_change`、`loss`、`abandonment`、`first_glance_contrast`、`long_term_neglect`、`object_change`、`viewpoint`、`season_match`，不使用 `lived_in`；并增加 `visible_changes` 字符串数组，以及 `loss_evidence`、`first_glance_evidence`、`long_term_neglect_evidence` 三个字符串。`visible_changes` 至少各有一项 `[功能]`、`[结构/维护]`、`[环境/设施/用途]`，用“旧照实际可见状态 → 今照实际可见状态”描述，并至少包含两项当天清场无法造成的持久变化。`loss_evidence` 说明失去的具体活动或关系及长期退出痕迹；`first_glance_evidence` 说明普通预览尺寸最先看到的主体反差；`long_term_neglect_evidence` 说明至少两项持久失管证据及其时间、材料和环境因果。计划中有而实际图片没有、看不清、只靠文案成立或与年份不匹配的内容不能作为证据。不通过项填 false，passed=false，写清返工原因；本地脚本只验证字段结构，语义真实性必须由 Codex 看图判断。

外围审核沿用 `time_change`、`observations` 和 `visible_changes`，不增加 JSON 字段。`observations` 必须单列外围核对及稳定、不可见范围，`visible_changes` 单列实际发生的外围变化；示例中的外围项仅在像素确有证据时填写。外围机械冻结或无因果地统一荒废时，`time_change=false`、`passed=false`；合理稳定或本就不可见不因此失败。此语义门槛由 Codex 实际看图执行，本地字段校验不能代替。

今照审核 JSON（以下只是结构示例，不得未经看图直接套用通过）：

```json
{
  "passed": true,
  "checks": {
    "realism": true,
    "era": true,
    "anatomy": true,
    "composition": true,
    "no_text": true,
    "rural": true,
    "same_place": true,
    "time_change": true,
    "loss": true,
    "abandonment": true,
    "first_glance_contrast": true,
    "long_term_neglect": true,
    "object_change": true,
    "viewpoint": true,
    "season_match": true
  },
  "observations": "写实际整体状态和不足；外围核对：可见房屋与环境的变化、合理稳定部分及不可见范围",
  "visible_changes": [
    "[功能] 旧照实际状态 → 今照实际状态",
    "[结构/维护] 旧照实际状态 → 今照实际状态",
    "[环境/设施/用途] 主体周边旧照实际状态 → 今照实际状态",
    "[环境/设施/用途] 外围：具体房屋或环境的旧照实际状态 → 今照实际状态"
  ],
  "loss_evidence": "失去的具体生活及长期退出证据",
  "first_glance_evidence": "普通预览尺寸最先看到的主体反差",
  "long_term_neglect_evidence": "至少两项持久失管痕迹及其因果"
}
```

成片审核 JSON：

```json
{"framing": true, "years": true, "order": true, "transitions": true, "observations": "实际查看预览和抽样转场后的观察；不能声称检查过未查看内容"}
```

`render` 是同步编码命令，命令工具如返回会话 ID，继续轮询该会话，不重复启动。锁覆盖编码过程，异常退出后操作系统释放锁；重新 render 从已审核素材重做编码，音乐和文件名保持不变。输出检查通过后才将 partial MP4 改名。已有成片会返回原文件，不产生重复视频。
默认保存目录 `outputs/codex-time-echo/<批次>/`，以真实命令输出为准。`preview.jpg` 从成片抽帧，便于检查每组的最终画面；必要时用 FFmpeg 抽取转场附近帧，随后用 view_image 查看。
`finish` 成功后只清理本轮 assets 中的 PNG，保留 MP4、预览、manifest、提示词与审核文件，同时为新完成视频创建待发布进度。发布命令和浏览器步骤见 [publishing.md](publishing.md)。日常产物已被 Git 忽略；运行技能并未修改项目代码时不需要制造空提交。

配置 `codex-video.json` 只影响新批次，未完成批次使用其创建时保存的配置。默认无需改配置即可运行。次数用尽后不改 manifest 偷偷重置预算。


## 执行权核验与接管

整轮占用保存在输出目录的 `execution.json`，与对话历史分开。单次命令锁仍保护读写和编码；整轮占用覆盖思考、生图工具等待、审核和交付间隙，不按时间自动过期。`ownership`、`status`、`history` 是只读命令。占用冲突返回退出码 2 和 `status=busy`；本地命令锁正忙时 `execution` 可能为空，不能据此判断无人执行。

- 同一执行者继续本轮时可用 `begin --token <执行凭证>`，即使视频已完成也返回原批次；凭证遗失时只可从 `ownership` 取回与当前任务 ID 完全相符的凭证。
- 正常结束、暂停或保存阻塞后，用 `release --run <批次> --token <执行凭证> --reason '已保存进度并结束本轮执行'`。释放不改图片、次数和制作状态。存在 pending 请求时释放会被拒绝，先处理真实结果。
- A 已释放：B 直接 `begin`。制作或发布未完成的批次从原进度继续；发布和通知均完成（delivered）后创建下一批。升级前没有发布进度的历史 done 批次仍按已完成处理，不自动补发。
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
