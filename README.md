# Download Center Bot

Telegram media downloader/converter using Python, python-telegram-bot, yt-dlp and FFmpeg.

## Included

- Mandatory membership check for @rooznewss1
- YouTube / Instagram / TikTok / Pinterest / SoundCloud and other public yt-dlp-compatible URLs
- 360p / 480p / 720p video choices
- Real MP3 extraction with FFmpeg
- Metadata/caption handling
- Download progress updates
- Per-user rate limit
- Temporary-file cleanup
- Admin stats
- Render webhook deployment
- Environment-variable secrets; no bot token in source

## Important

The bot is intentionally limited to public content. It does not bypass private access controls, authentication, DRM, CAPTCHA or other technical restrictions.

For force-join to work reliably, the bot should be an administrator of the target channel.

Render Free web services can spin down after 15 minutes without inbound traffic, so this is suitable for testing/hobby use rather than guaranteed 24/7 operation.

## Required Render variables

BOT_TOKEN
FORCE_JOIN_CHANNEL=@rooznewss1
FORCE_JOIN_URL=https://t.me/rooznewss1
WEBHOOK_SECRET=<long random value>
WEBHOOK_BASE_URL=https://<your-service>.onrender.com

Optional:
ADMIN_IDS=123456789,987654321
MAX_FILE_MB=45
RATE_LIMIT_SECONDS=20
DOWNLOAD_TIMEOUT=900
