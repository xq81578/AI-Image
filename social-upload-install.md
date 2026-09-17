# social-auto-upload 视频号安装与验证

上游仓库：https://github.com/dreammis/social-auto-upload

固定 SHA：`0012d2c355f88f683cc38dde2a2db209e14091bc`。主仓仅保留适配器、版本锁、补丁和测试；第三方源码与 venv 被忽略。补丁修改真实腾讯模块的提交钩子、二维码刷新和异常处理，并将终端二维码解码依赖改为按需导入。腾讯登录校验不注入上游 stealth 脚本。无需安装其他平台、Web 后端或 Node 依赖。

在项目目录执行以下 PowerShell 命令。已有上游目录时先检查本地差异，不覆盖未知修改。

```powershell
git clone https://github.com/dreammis/social-auto-upload social-auto-upload
git -C social-auto-upload checkout --detach 0012d2c355f88f683cc38dde2a2db209e14091bc
git -C social-auto-upload apply --check ../social-upload-upstream.patch
git -C social-auto-upload apply ../social-upload-upstream.patch
C:/Python313/python.exe -m venv .venv-social
.venv-social/Scripts/python.exe -m pip install -r requirements-social-upload.txt
.venv-social/Scripts/python.exe social_upload_adapter.py --smoke
.venv-social/Scripts/python.exe -m unittest discover -s tests -p 'test_*.py'
```

适配器核验 SHA、补丁文件的 SHA256 和 `patchright==1.62.3`；哈希按 LF 规范化，以兼容 Git 的 Windows 换行设置。固定浏览器路径为 `C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe`，不存在则明确失败。适配器连接 `打开视频号.bat` 启动的持久 Edge，复用同一个页面完成登录、上传和结果核验；仅在当前 Python 进程提供腾讯所需配置。没有下载其他浏览器、启动后台服务或安装定时任务。

正式入口为 `.venv-social/Scripts/python.exe social_upload_adapter.py --run <批次> --token <执行凭证>`；非 Codex 环境还需 `--owner <真实任务ID>`。`--smoke` 无浏览器、无网络、无发送。`--dry-run` 会真实上传填表但不发表，不可当作离线验证。原 `publish_video.py` 保留状态机与二维码通知公共函数，新的入口不调用其 ChannelsPage 或旧发布执行流程。

二维码回调读取上游真实图片，经 Pillow 格式校验后调用项目 Telegram CLI 的 document 模式；复用已有持久事件、哈希、回执去重。二维码与临时说明在回调结束或失败时清理；登录态同时保存在持久 Edge 用户目录和被忽略的工作区共享文件 `.social-upload-account/social-account.json`，不写入版本库或事件。登录、上传和核验结束后只断开自动化连接，不关闭 Edge、页面或浏览器上下文。

固定设置逐项真实填入并核验。控件缺失或值不一致阻塞；平台页面改版需重新校准定位器。提交意图先由原状态机原子写盘，然后上游仅尝试一次 click。点击超时不补点；缺少明确成功提示时，脚本在同一 Edge 页面直接打开作品列表，按完整文案、话题和最近30分钟发布时间核对。找到匹配作品才登记 published；找不到才进入 uncertain，并把浏览器停留在作品列表供人工核对。正式平台页面和 Telegram 真实送达未在离线测试中验证。

登录态跨批复用；临时二维码只允许共享账户目录内固定前缀 PNG，清理不会删除 storage state。上游登录循环使用 asyncio.sleep，外层超时可取消；Telegram 同步 subprocess 通知本身最多等待60秒，因此正在发送时总超时可能最多延后约60秒。smoke 不证明浏览器已能启动。
