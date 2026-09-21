import asyncio
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
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

app = Flask(__name__)
tg_app: Application | None = None
bot_loop: asyncio.AbstractEventLoop | None = None
bot_start_thread: threading.Thread | None = None
bot_ready = threading.Event()
bot_start_error: str | None = None
bot_start_lock = threading.Lock()
started_at = time.time()
last_request = time.time()
rate_state: dict[int, float] = {}
url_cache: dict[str, tuple[int, str, float]] = {}
stats = {"users": set(), "downloads": 0, "errors": 0}

URL_RE = re.compile(r"https?://[^\s<>]+")

def is_valid_url(value: str) -> bool:
    try:
        p = urlparse(value)
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False

def cache_url(user_id: int, url: str) -> str:
    key = hashlib.sha256(f"{user_id}:{url}:{time.time_ns()}".encode()).hexdigest()[:16]
    url_cache[key] = (user_id, url, time.time())
    # Keep the cache bounded.
    if len(url_cache) > 5000:
        cutoff = time.time() - 3600
        for k, (_, _, ts) in list(url_cache.items()):
            if ts < cutoff:
                url_cache.pop(k, None)
    return key

def get_cached_url(user_id: int, key: str) -> str | None:
    item = url_cache.get(key)
    if not item or item[0] != user_id:
        return None
    return item[1]

def rate_limited(user_id: int) -> bool:
    now = time.time()
    last = rate_state.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    rate_state[user_id] = now
    return False

async def is_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(FORCE_JOIN_CHANNEL, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except Exception as exc:
        log.warning("Membership check failed: %s", exc)
        return False

def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال", url=FORCE_JOIN_URL)],
        [InlineKeyboardButton("✅ عضو شدم؛ بررسی کن", callback_data="check_join")],
    ])

async def require_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    if await is_member(user.id, context):
        return True
    text = (
        "🔒 برای استفاده از «دانلود سنتر» ابتدا باید در کانال ما عضو شوی.\n\n"
        "بعد از عضویت روی «عضو شدم؛ بررسی کن» بزن."
    )
    if update.callback_query:
        await update.callback_query.answer("ابتدا عضو کانال شو.", show_alert=True)
        await update.callback_query.message.reply_text(text, reply_markup=join_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=join_keyboard())
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    stats["users"].add(update.effective_user.id)
    await update.effective_message.reply_text(
        "🚀 خوش آمدی به «دانلود سنتر»\n\n"
        "لینک عمومی YouTube، Instagram، TikTok، Pinterest، SoundCloud و سرویس‌های سازگار با yt-dlp را بفرست.\n\n"
        "بعد از ارسال لینک، کیفیت یا MP3 را انتخاب کن."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    await update.effective_message.reply_text(
        "📥 راهنما\n\n"
        "• یک لینک عمومی رسانه بفرست\n"
        "• کیفیت 360/480/720 یا MP3 را انتخاب کن\n"
        "• فایل بعد از پردازش برایت ارسال می‌شود\n\n"
        "⚠️ محتوای خصوصی، محدودشده یا دارای حفاظت فنی پشتیبانی نمی‌شود."
    )

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await is_member(q.from_user.id, context):
        await q.answer("عضویت تأیید شد ✅")
        await q.message.reply_text("عالی! حالا لینک رسانه را بفرست. 🚀")
    else:
        await q.answer("هنوز عضویت تأیید نشد.", show_alert=True)

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    user_id = update.effective_user.id
    stats["users"].add(user_id)
    if rate_limited(user_id):
        await update.effective_message.reply_text(
            f"⏳ برای جلوگیری از فشار روی سرویس، هر {RATE_LIMIT_SECONDS} ثانیه یک دانلود مجاز است."
        )
        return

    match = URL_RE.search(update.effective_message.text or "")
    if not match or not is_valid_url(match.group(0)):
        await update.effective_message.reply_text("❌ لینک معتبر HTTP/HTTPS پیدا نشد.")
        return

    url = match.group(0)
    key = cache_url(user_id, url)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("360p", callback_data=f"dl|360|{key}"),
            InlineKeyboardButton("480p", callback_data=f"dl|480|{key}"),
            InlineKeyboardButton("720p", callback_data=f"dl|720|{key}"),
        ],
        [InlineKeyboardButton("🎵 MP3", callback_data=f"dl|mp3|{key}")],
    ])
    await update.effective_message.reply_text(
        "🎛️ فرمت/کیفیت را انتخاب کن:", reply_markup=keyboard
    )

def ffmpeg_path() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()

def download_media(url: str, mode: str, progress=None) -> tuple[Path, dict, Path | None]:
    temp_dir = Path(tempfile.mkdtemp(prefix="download_center_"))
    out = temp_dir / "%(id)s.%(ext)s"
    ffmpeg = ffmpeg_path()

    def hook(data):
        if progress and data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes", 0)
            if total:
                progress(int(done * 100 / total))

    if mode == "mp3":
        fmt = "bestaudio/best"
        post = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        height = int(mode)
        fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        post = []

    opts = {
        "outtmpl": str(out),
        "format": fmt,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 30,
        "merge_output_format": "mp4" if mode != "mp3" else None,
        "ffmpeg_location": ffmpeg,
        "progress_hooks": [hook],
        "postprocessors": post,
        "restrictfilenames": True,
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
    }
    opts = {k: v for k, v in opts.items() if v is not None}

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        requested = Path(ydl.prepare_filename(info))
        candidates = list(temp_dir.glob("*"))
        if mode == "mp3":
            media = next((p for p in candidates if p.suffix.lower() == ".mp3"), requested)
        else:
            media = next((p for p in candidates if p.suffix.lower() in {".mp4", ".mkv", ".webm"}), requested)
        thumb = next((p for p in candidates if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}), None)
        return media, info, thumb

async def run_download(query, context: ContextTypes.DEFAULT_TYPE, mode: str, url: str):
    user_id = query.from_user.id
    message = query.message
    await query.answer("شروع شد…")
    progress_message = await message.reply_text("⬇️ در حال دانلود… 0%")
    loop = asyncio.get_running_loop()
    last_progress = [0]

    def progress(percent: int):
        if percent >= last_progress[0] + 10 or percent == 100:
            last_progress[0] = percent
            asyncio.run_coroutine_threadsafe(
                progress_message.edit_text(f"⬇️ در حال دانلود… {percent}%"),
                loop,
            )

    media = None
    try:
        media, info, thumb = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: download_media(url, mode, progress)),
            timeout=DOWNLOAD_TIMEOUT,
        )
        size_mb = media.stat().st_size / 1024 / 1024
        if size_mb > MAX_FILE_MB:
            raise ValueError(f"حجم فایل {size_mb:.1f}MB است و سقف ارسال {MAX_FILE_MB}MB است.")

        title = info.get("title") or "Download Center"
        caption = f"🎬 {title}\n\n📥 دانلود شده با Download Center"
        if mode == "mp3":
            await message.reply_audio(
                audio=media.open("rb"),
                title=title[:64],
                performer=(info.get("artist") or info.get("uploader") or "")[:64],
                caption=caption[:1024],
            )
        else:
            await message.reply_video(
                video=media.open("rb"),
                caption=caption[:1024],
                supports_streaming=True,
            )
        stats["downloads"] += 1
        await progress_message.delete()
    except Exception as exc:
        stats["errors"] += 1
        log.exception("download failed")
        await progress_message.edit_text(
            "❌ دانلود انجام نشد.\n\n"
            f"جزئیات: {str(exc)[:700]}\n\n"
            "اگر لینک خصوصی یا محدود است، لینک عمومی دیگری ارسال کن."
        )
    finally:
        # media lives in a per-job temp directory; locate it from the path when present.
        try:
            parent = media.parent
            shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data == "check_join":
        await check_join(update, context)
        return
    if not await require_join(update, context):
        return
    parts = q.data.split("|")
    if len(parts) != 3 or parts[0] != "dl":
        await q.answer("درخواست نامعتبر است.", show_alert=True)
        return
    mode, key = parts[1], parts[2]
    url = get_cached_url(q.from_user.id, key)
    if not url:
        await q.answer("این لینک منقضی شده؛ دوباره URL را بفرست.", show_alert=True)
        return
    await run_download(q, context, mode, url)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.effective_message.reply_text(
        f"👥 کاربران این نمونه: {len(stats['users'])}\n"
        f"📥 دانلودها: {stats['downloads']}\n"
        f"❌ خطاها: {stats['errors']}"
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("telegram handler error", exc_info=context.error)

def build_bot() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    application.add_error_handler(error_handler)
    return application

def bot_thread():
    global bot_loop, tg_app, bot_start_error
    try:
        bot_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(bot_loop)
        tg_app = build_bot()

        async def init():
            await tg_app.initialize()
            await tg_app.start()
            webhook_url = f"{WEBHOOK_BASE_URL}/telegram/webhook/{WEBHOOK_SECRET}"
            await tg_app.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            log.info("Telegram webhook configured: %s", webhook_url)

        bot_loop.run_until_complete(init())
        bot_ready.set()
        bot_loop.run_forever()
    except Exception as exc:
        bot_start_error = str(exc)
        log.exception("Telegram bot thread failed to start")
        bot_ready.set()
        raise


def ensure_bot_started(wait: float = 15.0) -> bool:
    global bot_start_thread
    if not BOT_TOKEN or not WEBHOOK_BASE_URL or not WEBHOOK_SECRET:
        return False
    if bot_ready.is_set() and tg_app is not None and bot_loop is not None and not bot_loop.is_closed():
        return True
    with bot_start_lock:
        if bot_start_thread is None or not bot_start_thread.is_alive():
            bot_ready.clear()
            bot_start_thread = threading.Thread(target=bot_thread, daemon=True, name="telegram-bot")
            bot_start_thread.start()
    bot_ready.wait(timeout=wait)
    return tg_app is not None and bot_loop is not None and not bot_loop.is_closed()

@app.get("/")
def home():
    return jsonify({"service": "Download Center", "status": "ok"})

@app.get("/health")
def health():
    ready = ensure_bot_started(wait=15.0) if BOT_TOKEN else False
    return jsonify({
        "status": "ok",
        "bot_configured": bool(BOT_TOKEN),
        "webhook_configured": bool(WEBHOOK_BASE_URL and WEBHOOK_SECRET),
        "telegram_ready": ready,
        "telegram_error": bot_start_error,
        "uptime_seconds": int(time.time() - started_at),
        "last_request_age": int(time.time() - last_request),
    })

@app.post("/telegram/webhook/<secret>")
def telegram_webhook(secret: str):
    global last_request
    last_request = time.time()
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        return ("not found", 404)
    if not ensure_bot_started(wait=15.0):
        log.error("Telegram webhook unavailable: bot_ready=%s tg_app=%s bot_loop=%s error=%s",
                  bot_ready.is_set(), tg_app is not None, bot_loop is not None, bot_start_error)
        return ("starting", 503)
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, tg_app.bot)
        bot_loop.call_soon_threadsafe(tg_app.update_queue.put_nowait, update)
        return ("ok", 200)
    except Exception:
        log.exception("webhook error")
        return ("bad request", 400)

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is not set. Add it as a Render secret before expecting Telegram updates.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
