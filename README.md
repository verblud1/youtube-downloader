# youtube-downloader


https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc?hl=en


фикс ошибки yt-dlp не видит Node.js
через powershell или через проводник %APPDATA%\yt-dlp\config.txt
New-Item -ItemType Directory -Path "$Home\AppData\Roaming\yt-dlp" -Force; Set-Content -Path "$Home\AppData\Roaming\yt-dlp\config.txt" -Value "--js-runtimes node"

pip install "yt-dlp[default]"
