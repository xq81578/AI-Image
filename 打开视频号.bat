@echo off
set "EDGE_PROFILE=%LOCALAPPDATA%\CodexVideoPublisher\EdgeProfile"
start "" msedge --new-window --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 --user-data-dir="%EDGE_PROFILE%" "https://channels.weixin.qq.com/platform/post/create"
