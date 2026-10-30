@echo off
set "CHROME_PROFILE=%LOCALAPPDATA%\CodexVideoPublisher\ChromeProfile"
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --new-window --remote-debugging-address=127.0.0.1 --remote-debugging-port=9224 --user-data-dir="%CHROME_PROFILE%" "https://channels.weixin.qq.com/platform/post/create"
