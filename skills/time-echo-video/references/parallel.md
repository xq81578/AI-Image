# 组间并发制作

本技能只并发同一批次内的独立组；仍然只创建一批次、只输出一条视频。并发边界是“组间并行、组内串行”：同一组最多一个 `pending`，且今照必须等待本组旧照审核合格；不同组可以同时有 `pending`。并发减少等待，不保证按组数线性提速，更不能承诺六倍提速。

## 角色和生命周期

主 Codex 是统筹者，持有一次 `begin` 返回的整轮执行凭证，负责恢复、策划、委派、总体验收、合成、通知和发布。制作子智能体是一组一个，固定负责一个 `pair`，只负责本组提示词、内置生图、真实看图审核、定向返工或换题，以及本组素材目录。

统筹按以下顺序推进：

1. `begin` 取得本轮 `run_id`、目录和统筹凭证，读取批次的组数（随机 5 或 6）。先依照创作审核要求完成全部组的策划，并逐组执行 `plan` 保存；所有计划保存前不得创建生图请求。
2. 新批次按本轮实际组数一次创建全部制作子智能体；恢复时复用合格组和仍可继续的原子智能体，只为缺少有效 worker 的未完成组创建替代者。创建提示只要求子智能体报告自己的真实 `CODEX_THREAD_ID` 和 `image_gen` 是否可用；此阶段禁止生图、写本批素材、调用 `plan`/`attempt`，也不允许子智能体代替统筹领取执行权。
3. 等待所有子智能体都报告 `ready` 后，统筹对每个未完成组执行一次 `delegate`，登记返回的独立组凭证；随后对每个子智能体各 `followup_task` 一次，明确可以开工。不要把统筹凭证发给子智能体。
4. 子智能体使用自己的真实任务 ID、canonical agent name 和组凭证工作。它可以为本组维护独立的 `prompts/`、`reviews/` 等目录，并用现有 `plan`（仅在换题后保存替代计划）、`attempt`、内置 `image_gen`、真实 `view_image`、`record`、`fail`、`replace` 完成本组；`record` 仍由项目文件锁统一登记。旧照通过后才能请求今照；成像或反差不合格时只针对实际问题返工，反复失败按现有规则换题。
5. 子智能体不得执行 `begin`、`takeover`、`release`、任何 heartbeat、`render`、`finish`、Telegram 通知或视频号发布，也不向统筹逐步申请许可。除 `ready`、最终本组状态、明确阻塞或需要统筹处理的异常外，不回报每一步制作细节。
6. 统筹等待各组结果，只有全部旧照和今照均审核合格后，才复核组号、旧今配对、总调用预算和整体重复度，按组编号执行 `render`，查看成片并继续现有 `finish`、Telegram，再由统筹按 [publishing.md](publishing.md) 使用默认浏览器全程发布。任一组仍有明确阻塞或需处理的异常时，先处理该组或保存阻塞，不跳过该组直接合成。

## 创建和容量门槛

新批次需要的子智能体数量等于 `manifest` 的组数：5 组创建 5 个，6 组创建 6 个；不把统筹者计入这个数量。恢复时全部未完成组仍须一组一个有效 worker，已合格组无需再创建 worker。项目 `.codex/config.toml` 的 `max_concurrent_threads_per_session = 6` 只表达期望的子智能体上限，不能代替当前宿主实际容量。运行时必须确认每个子智能体真实创建成功并报告 `image_gen` 可用；如果宿主不足以一次创建全部所需数量，立即保存明确阻塞并报告实际可用名额，不静默改成 2 或 3 个排队，也不开始任何实际生图。

## 委派记录

每个委派写入该批次 `manifest.json` 的对应 `pairs[i].worker`，保存子智能体真实 `thread_id`、canonical `agent`、独立 `token`、父代际 `parent_token`、`status` 和 `delegated_at`。批次从 manifest 顶层 `run_id` 取得，组号从对应 `pairs[i].index` 取得；`ready` 回报由统筹在执行 `delegate` 前核实。统筹占用本身写在输出目录的 `execution.json`。组凭证只给对应子智能体；统筹凭证只留在统筹者。组目录只能写本组提示词、审核和中间素材，不能写其他组的文件。整轮默认最多 36 次生图调用，预算由批次统一计数，不能按子智能体各自分配 36 次，也不能新建批次绕过上限。

## 子智能体失联与重派

如果子智能体无法再收到 `followup_task`，先由统筹用 `wait_threads` 或 `read_thread` 确认它已停止。只要该组仍有 `pending`，统筹必须使用自己的有效统筹 token 找回实际工具结果，对原 `attempt` 看图后执行 `record` 或 `fail`；不能先换 worker，不能新增 `attempt`，不能重复生图。原结果登记完且该组无 pending 后，才创建替代子智能体；替代者先只报告真实 ID、canonical name 和 `image_gen` 可用性，统筹再用 `delegate --file <旧worker停止核验JSON>` 旋转组 token，随后只发送一次开工任务。

同一统筹代际的 `delegate --file` 停止核验必须包含旧 worker 的 `thread_id`、`observed_status`、`source`、带时区的 `checked_at` 和非空 `evidence`，核验不得超过 60 秒。成功 `takeover` 后，旧统筹及旧 worker token 全部失效；接管核验已覆盖旧 worker 停止，新统筹对无 pending 的未完成组创建新 worker、等待 ready 后直接 `delegate`，再发送开工任务。多 worker 接管核验的完整 `workers` 映射示例见 [execution.md](execution.md#执行权核验与接管)。

## 与旧批次兼容

没有委派记录的旧批次，由统筹恢复原凭证并先处理原 pending、复用已合格素材，再为全部未完成组按本节建立并发委派；不为补建委派记录重新生成图片，也不因批次较旧默认退回串行。已有委派记录的批次恢复时，先找回每个子智能体和其实际工具结果，再决定是否需要重新委派未完成组。
