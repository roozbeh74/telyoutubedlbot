import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request
import imageio_ffmpeg
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("download-center")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "@rooznewss1").strip()
FORCE_JOIN_URL = os.getenv("FORCE_JOIN_URL", "https://t.me/rooznewss1").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "45"))
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "20"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "900"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
YOUTUBE_COOKIES_B64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "")

app = Flask(__name__)
tg_app = None
bot_loop = None
bot_start_thread = None
bot_ready = threading.Event()
bot_start_error = None
bot_start_lock = threading.Lock()
started_at = time.time()
rate_state = {}
url_cache = {}
stats = {"users": set(), "downloads": 0, "errors": 0}
URL_RE = re.compile(r"https?://[^\s<>]+")


def is_valid_url(value):
    try:
        p = urlparse(value)
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False


def cache_url(user_id, url):
    key = hashlib.sha256(f"{user_id}:{url}:{time.time_ns()}".encode()).hexdigest()[:16]
    url_cache[key] = (user_id, url, time.time())
    if len(url_cache) > 5000:
        cutoff = time.time() - 3600
        for k, (_, _, ts) in list(url_cache.items()):
            if ts < cutoff:
                url_cache.pop(k, None)
    return key


def get_cached_url(user_id, key):
    item = url_cache.get(key)
    return item[1] if item and item[0] == user_id else None


def rate_limited(user_id):
    now = time.time()
    last = rate_state.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    rate_state[user_id] = now
    return False


def cookie_file():
    if not (YOUTUBE_COOKIES_B64 or YOUTUBE_COOKIES):
        return None
    path = Path(tempfile.gettempdir()) / "download_center_youtube_cookies.txt"
    try:
        if YOUTUBE_COOKIES_B64:
            data = base64.b64decode(YOUTUBE_COOKIES_B64, validate=True)
            path.write_bytes(data)
        else:
            path.write_text(YOUTUBE_COOKIES, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path)
    except Exception:
        log.exception("Could not prepare YouTube cookies")
        return None


def friendly_error(exc):
    text = str(exc)
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low or "confirm you’re not a bot" in low:
        return ("⚠️ یوتیوب دسترسی این سرور ابری را محدود کرده است.\n\n"
                "یک لینک عمومی دیگر امتحان کن. اگر کوکی شخصی خودت را در Render تنظیم کرده‌ای، "
                "ممکن است کمک کند؛ اما رفع این محدودیت تضمین‌شده نیست و کوکی را در چت ارسال نکن.")
    if "private video" in low or "login required" in low:
        return "🔒 این محتوا خصوصی یا نیازمند ورود است. فقط لینک عمومی و مجاز ارسال کن."
    if "unsupported url" in low:
        return "❌ این نوع لینک پشتیبانی نمی‌شود. لینک مستقیم و عمومی دیگری امتحان کن."
    if "max-filesize" in low or "file is larger" in low:
        return f"📦 حجم فایل از سقف {MAX_FILE_MB} مگابایت بیشتر است."
    if "timed out" in low or "timeout" in low:
        return "⏱️ زمان دانلود تمام شد. لینک کوتاه‌تر یا کیفیت پایین‌تر را امتحان کن."
    return f"جزئیات فنی: {text[:600]}"


async def is_member(user_id, context):
    try:
        member = await context.bot.get_chat_member(FORCE_JOIN_CHANNEL, user_id)
        return member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
    except Exception as exc:
        log.warning("Membership check failed: %s", exc)
        return False


def join_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📢 عضویت در کانال", url=FORCE_JOIN_URL)], [InlineKeyboardButton("✅ عضو شدم؛ بررسی کن", callback_data="check_join")]])


async def require_join(update, context):
    user = update.effective_user
    if not user:
        return False
    if await is_member(user.id, context):
        return True
    text = "🔒 ابتدا در کانال ما عضو شو و سپس روی «عضو شدم؛ بررسی کن» بزن."
    if update.callback_query:
        await update.callback_query.answer("ابتدا عضو کانال شو.", show_alert=True)
        await update.callback_query.message.reply_text(text, reply_markup=join_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=join_keyboard())
    return False


async def start(update, context):
    if not await require_join(update, context): return
    stats["users"].add(update.effective_user.id)
    await update.effective_message.reply_text("🚀 به دانلود سنتر خوش آمدی!\n\nلینک عمومی رسانه را بفرست؛ سپس کیفیت یا MP3 را انتخاب کن.")


async def help_cmd(update, context):
    if not await require_join(update, context): return
    await update.effective_message.reply_text("📥 لینک عمومی YouTube، Instagram، TikTok، Pinterest، SoundCloud یا سرویس سازگار را بفرست.\n\n⚠️ محتوای خصوصی، DRM و محدودشده پشتیبانی نمی‌شود.")


async def check_join(update, context):
    q = update.callback_query
    if await is_member(q.from_user.id, context):
        await q.answer("عضویت تأیید شد ✅")
        await q.message.reply_text("عالی! حالا لینک را بفرست 🚀")
    else:
        await q.answer("هنوز عضویت تأیید نشد.", show_alert=True)


async def receive_url(update, context):
    if not await require_join(update, context): return
    user_id = update.effective_user.id
    stats["users"].add(user_id)
    if rate_limited(user_id):
        await update.effective_message.reply_text(f"⏳ هر {RATE_LIMIT_SECONDS} ثانیه یک دانلود مجاز است.")
        return
    match = URL_RE.search(update.effective_message.text or "")
    if not match or not is_valid_url(match.group(0)):
        await update.effective_message.reply_text("❌ لینک معتبر HTTP/HTTPS پیدا نشد.")
        return
    key = cache_url(user_id, match.group(0))
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("360p", callback_data=f"dl|360|{key}"), InlineKeyboardButton("480p", callback_data=f"dl|480|{key}"), InlineKeyboardButton("720p", callback_data=f"dl|720|{key}")], [InlineKeyboardButton("🎵 MP3", callback_data=f"dl|mp3|{key}")]])
    await update.effective_message.reply_text("🎛️ کیفیت یا فرمت را انتخاب کن:", reply_markup=keyboard)


def ffmpeg_path():
    return imageio_ffmpeg.get_ffmpeg_exe()


def download_media(url, mode, progress=None):
    temp_dir = Path(tempfile.mkdtemp(prefix="download_center_"))
    out = temp_dir / "%(id)s.%(ext)s"
    def hook(data):
        if progress and data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            if total: progress(int(data.get("downloaded_bytes", 0) * 100 / total))
    if mode == "mp3":
        fmt, post = "bestaudio/best", [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        height = int(mode)
        fmt, post = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best", []
    opts = {"outtmpl": str(out), "format": fmt, "noplaylist": True, "quiet": True, "no_warnings": True, "retries": 2, "socket_timeout": 30, "merge_output_format": "mp4" if mode != "mp3" else None, "ffmpeg_location": ffmpeg_path(), "progress_hooks": [hook], "postprocessors": post, "restrictfilenames": True, "max_filesize": MAX_FILE_MB * 1024 * 1024}
    cookies = cookie_file()
    if cookies: opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL({k: v for k, v in opts.items() if v is not None}) as ydl:
        info = ydl.extract_info(url, download=True)
        requested = Path(ydl.prepare_filename(info))
        candidates = list(temp_dir.glob("*"))
        media = next((p for p in candidates if p.suffix.lower() == ".mp3"), requested) if mode == "mp3" else next((p for p in candidates if p.suffix.lower() in {".mp4", ".mkv", ".webm"}), requested)
        thumb = next((p for p in candidates if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}), None)
        return media, info, thumb


async def run_download(query, context, mode, url):
    await query.answer("شروع شد…")
    message = query.message
    progress_message = await message.reply_text("⬇️ در حال دانلود… 0%")
    loop = asyncio.get_running_loop(); last_progress = [0]; media = None
    def progress(percent):
        if percent >= last_progress[0] + 10 or percent == 100:
            last_progress[0] = percent
            asyncio.run_coroutine_threadsafe(progress_message.edit_text(f"⬇️ در حال دانلود… {percent}%"), loop)
    try:
        media, info, _ = await asyncio.wait_for(loop.run_in_executor(None, lambda: download_media(url, mode, progress)), timeout=DOWNLOAD_TIMEOUT)
        size_mb = media.stat().st_size / 1024 / 1024
        if size_mb > MAX_FILE_MB: raise ValueError(f"حجم فایل {size_mb:.1f}MB است.")
        title = info.get("title") or "Download Center"
        caption = f"🎬 {title}\n\n📥 دانلود شده با Download Center"
        if mode == "mp3":
            with media.open("rb") as f: await message.reply_audio(audio=f, title=title[:64], performer=(info.get("artist") or info.get("uploader") or "")[:64], caption=caption[:1024])
        else:
            with media.open("rb") as f: await message.reply_video(video=f, caption=caption[:1024], supports_streaming=True)
        stats["downloads"] += 1
        await progress_message.delete()
    except Exception as exc:
        stats["errors"] += 1
        log.exception("download failed")
        await progress_message.edit_text("❌ دانلود انجام نشد.\n\n" + friendly_error(exc) + "\n\nاگر لینک خصوصی یا محدود است، لینک عمومی دیگری ارسال کن.")
    finally:
        if media:
            shutil.rmtree(media.parent, ignore_errors=True)


async def callback_router(update, context):
    q = update.callback_query
    if q.data == "check_join": return await check_join(update, context)
    if not await require_join(update, context): return
    parts = q.data.split("|")
    if len(parts) != 3 or parts[0] != "dl":
        await q.answer("درخواست نامعتبر است.", show_alert=True); return
    url = get_cached_url(q.from_user.id, parts[2])
    if not url:
        await q.answer("لینک منقضی شده؛ دوباره ارسال کن.", show_alert=True); return
    await run_download(q, context, parts[1], url)


async def stats_cmd(update, context):
    if update.effective_user.id in ADMIN_IDS:
        await update.effective_message.reply_text(f"👥 کاربران: {len(stats['users'])}\n📥 دانلودها: {stats['downloads']}\n❌ خطاها: {stats['errors']}")


async def error_handler(update, context):
    log.error("telegram handler error: %s", context.error, exc_info=True)


def build_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start)); application.add_handler(CommandHandler("help", help_cmd)); application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CallbackQueryHandler(callback_router)); application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)); application.add_error_handler(error_handler)
    return application


def bot_thread():
    global bot_loop, tg_app, bot_start_error
    try:
        bot_loop = asyncio.new_event_loop(); asyncio.set_event_loop(bot_loop); tg_app = build_bot()
        async def init():
            await tg_app.initialize(); await tg_app.start()
            await tg_app.bot.set_webhook(url=f"{WEBHOOK_BASE_URL}/telegram/webhook/{WEBHOOK_SECRET}", secret_token=WEBHOOK_SECRET, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        bot_loop.run_until_complete(init()); bot_ready.set(); bot_loop.run_forever()
    except Exception as exc:
        bot_start_error = str(exc); log.exception("bot startup failed")


def ensure_bot_started(wait=15):
    global bot_start_thread
    with bot_start_lock:
        if not bot_start_thread or not bot_start_thread.is_alive():
            bot_start_thread = threading.Thread(target=bot_thread, daemon=True); bot_start_thread.start()
    bot_ready.wait(wait)
    return bot_ready.is_set()


@app.get("/")
def home(): return jsonify({"service": "download-center", "status": "ok"})

@app.get("/health")
def health():
    return jsonify({"status": "ok", "telegram_ready": bot_ready.is_set(), "telegram_error": bot_start_error})

@app.post("/telegram/webhook/<secret>")
def webhook(secret):
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET: return jsonify({"ok": False}), 403
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET: return jsonify({"ok": False}), 403
    if not ensure_bot_started(): return jsonify({"ok": False, "error": "bot starting"}), 503
    try:
        update = Update.de_json(request.get_json(force=True), tg_app.bot)
        future = asyncio.run_coroutine_threadsafe(tg_app.process_update(update), bot_loop)
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return jsonify({"ok": True})
    except Exception:
        log.exception("webhook processing failed"); return jsonify({"ok": False}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
