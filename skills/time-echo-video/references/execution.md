# 本地命令

所有命令在 `D:/AI创作生图项目` 运行。PowerShell 示例中的 `<批次>`、`<编号>` 要换成真实返回值。文本和 JSON 文件使用 UTF-8，通过文件参数传入，避免 shell 引号破坏提示词。脚本只负责本地工作，调用生图与看图仍由 Codex 完成。

```powershell
python codex_video.py begin
python codex_video.py history
python codex_video.py status --run <批次>
python codex_video.py plan --run <批次> --pair 1 --file <选题JSON>
python codex_video.py attempt --run <批次> --pair 1 --kind old --file <完整提示词TXT>
# 接着调用内置 image_gen，并实际查看工具生成的图片。
python codex_video.py record --run <批次> --attempt <编号> --image <实际本地图片路径> --file <审核JSON>
# 审核 old 合格后，重复 attempt / image_gen / 看图 / record，kind 改为 now。
python codex_video.py fail --run <批次> --attempt <编号> --reason '请求中断或真实错误'
python codex_video.py replace --run <批次> --pair 1 --reason '多次修正后仍未达到时间变化要求'
python codex_video.py block --run <批次> --reason '明确阻塞原因'
python codex_video.py render --run <批次>
python codex_video.py finish --run <批次> --file <成片审核JSON>
```

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
