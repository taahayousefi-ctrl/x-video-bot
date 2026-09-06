import json
import html
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
JAMENDO_CLIENT_ID = os.environ.get("JAMENDO_CLIENT_ID")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))

X_LINK_PATTERN = re.compile(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/\S+", re.I)
SPOTIFY_LINK_PATTERN = re.compile(r"https?://open\.spotify\.com/(?:intl-[^/]+/)?(?:track|album|playlist)/[^\s?]+(?:\?[^\s]+)?", re.I)
TWEET_ID_PATTERN = re.compile(r"status/(\d+)")


def get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def get_quoted_tweet_url(tweet_id: str) -> str | None:
    endpoint = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=a"
    try:
        data = get_json(endpoint)
    except Exception:
        logger.exception("خطا در گرفتن اطلاعات توییت کوت‌شده")
        return None
    quoted = data.get("quoted_tweet")
    if not quoted:
        return None
    quoted_id = quoted.get("id_str") or quoted.get("id")
    author = (quoted.get("user") or {}).get("screen_name")
    return f"https://x.com/{author}/status/{quoted_id}" if quoted_id and author else None


def download_video(url: str, tmp_dir: str) -> str | None:
    downloaded_files = []

    def hook(data):
        if data.get("status") == "finished" and data.get("filename"):
            downloaded_files.append(data["filename"])

    ydl_opts = {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": "best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)
    video_extensions = (".mp4", ".mov", ".webm", ".mkv")
    files = [f for f in downloaded_files if f.lower().endswith(video_extensions) and os.path.exists(f)]
    return max(files, key=os.path.getsize) if files else None


def spotify_metadata(url: str) -> dict:
    """Read public Spotify oEmbed metadata; no Spotify account token is required."""
    endpoint = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")
    data = get_json(endpoint)
    # Spotify's oEmbed response sometimes omits author_name. The public HTML
    # metadata contains the artist and remains usable without OAuth credentials.
    artist = data.get("author_name") or ""
    if not artist:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            page = response.read().decode("utf-8", errors="replace")
        match = re.search(r'<meta[^>]+(?:name|property)="(?:music:musician_description|description)"[^>]+content="([^"]+)"', page, re.I)
        if match:
            description = html.unescape(match.group(1))
            artist = description.split(" · ", 1)[0].replace("Listen to ", "").strip()
    return {
        "title": data.get("title", ""),
        "artist": artist,
        "thumbnail": data.get("thumbnail_url"),
        "spotify_url": data.get("url", url),
    }


def find_licensed_audio(title: str, artist: str, tmp_dir: str) -> tuple[str | None, str | None]:
    """Search Jamendo's licensed catalog. Requires a Jamendo client ID."""
    if not JAMENDO_CLIENT_ID:
        return None, None
    query = urllib.parse.urlencode(
        {
            "client_id": JAMENDO_CLIENT_ID,
            "format": "json",
            "limit": 5,
            "audioformat": "mp32",
            "search": f"{title} {artist}".strip(),
        }
    )
    data = get_json(f"https://api.jamendo.com/v3.0/tracks/?{query}")
    for track in data.get("results", []):
        audio_url = track.get("audiodownload") or track.get("audio")
        # Jamendo supplies the license URL; only accept tracks with an explicit license.
        if not audio_url or not track.get("license_ccurl"):
            continue
        safe_name = re.sub(r"[^\w.-]+", "_", f"{artist}-{title}", flags=re.UNICODE).strip("_")
        path = os.path.join(tmp_dir, f"{safe_name or 'spotify-track'}.mp3")
        request = urllib.request.Request(audio_url, headers={"User-Agent": "spotify-telegram-bot/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, open(path, "wb") as output:
            while chunk := response.read(1024 * 256):
                output.write(chunk)
                if output.tell() > MAX_FILE_SIZE_MB * 1024 * 1024:
                    raise ValueError("فایل صوتی بزرگ‌تر از سقف مجاز تلگرام است")
        return path, track.get("license_ccurl")
    return None, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "سلام! لینک ویدیوی X یا لینک آهنگ Spotify را بفرست. "
        "برای Spotify فقط فایل‌های دارای مجوز از کاتالوگ مجاز ارسال می‌شوند."
    )


async def handle_x(update: Update, url: str) -> None:
    status_msg = await update.message.reply_text("در حال دانلود ویدیو... ⏳")
    await update.get_bot().send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = None
        try:
            file_path = download_video(url, tmp_dir)
        except Exception:
            logger.exception("خطا در دانلود ویدیو")
        if not file_path:
            match = TWEET_ID_PATTERN.search(url)
            quoted_url = get_quoted_tweet_url(match.group(1)) if match else None
            if quoted_url:
                try:
                    file_path = download_video(quoted_url, tmp_dir)
                except Exception:
                    logger.exception("خطا در دانلود ویدیوی توییت کوت‌شده")
        if not file_path:
            await status_msg.edit_text("ویدیویی در این پست پیدا نشد.")
            return
        if os.path.getsize(file_path) > MAX_FILE_SIZE_MB * 1024 * 1024:
            await status_msg.edit_text(f"حجم ویدیو بیشتر از سقف {MAX_FILE_SIZE_MB} مگابایت است.")
            return
        await status_msg.edit_text("در حال ارسال ویدیو... 📤")
        try:
            with open(file_path, "rb") as video_file:
                await update.message.reply_video(video=video_file)
            await status_msg.delete()
        except Exception as error:
            logger.exception("خطا در ارسال ویدیو")
            await status_msg.edit_text(f"ارسال ویدیو ناموفق بود:\n{error}")


async def handle_spotify(update: Update, url: str) -> None:
    status_msg = await update.message.reply_text("در حال بررسی لینک Spotify و پیدا کردن نسخه‌ی مجاز... ⏳")
    try:
        metadata = spotify_metadata(url)
        title, artist = metadata["title"], metadata["artist"]
    except Exception:
        logger.exception("خطا در خواندن اطلاعات Spotify")
        await status_msg.edit_text("خواندن اطلاعات این لینک Spotify ناموفق بود.")
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            file_path, license_url = find_licensed_audio(title, artist, tmp_dir)
        except Exception as error:
            logger.exception("خطا در جست‌وجوی منبع صوتی مجاز")
            await status_msg.edit_text(f"جست‌وجوی فایل صوتی ناموفق بود:\n{error}")
            return
        if not file_path:
            if not JAMENDO_CLIENT_ID:
                reason = "برای فعال‌سازی جست‌وجوی فایل‌های مجاز، متغیر JAMENDO_CLIENT_ID روی سرور تنظیم نشده است."
            else:
                reason = "نسخه‌ی مجاز و قابل‌دانلود این آهنگ در کاتالوگ موجود پیدا نشد."
            await status_msg.edit_text(
                f"🎵 {title}\n👤 {artist}\n\n{reason}\n\n🔗 پخش رسمی: {metadata['spotify_url']}"
            )
            return
        await status_msg.edit_text("فایل مجاز پیدا شد؛ در حال ارسال... 📤")
        try:
            with open(file_path, "rb") as audio_file:
                await update.message.reply_audio(
                    audio=audio_file,
                    title=title[:64] or "Spotify track",
                    performer=artist[:64] or None,
                    caption=f"منبع مجاز: {license_url}",
                )
            await status_msg.delete()
        except Exception as error:
            logger.exception("خطا در ارسال فایل صوتی")
            await status_msg.edit_text(f"ارسال فایل صوتی ناموفق بود:\n{error}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    spotify_match = SPOTIFY_LINK_PATTERN.search(text)
    if spotify_match:
        await handle_spotify(update, spotify_match.group(0))
        return
    x_match = X_LINK_PATTERN.search(text)
    if x_match:
        await handle_x(update, x_match.group(0))
        return
    await update.message.reply_text("لطفاً یک لینک معتبر از X/Twitter یا Spotify بفرست.")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("متغیر محیطی BOT_TOKEN تنظیم نشده است.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
