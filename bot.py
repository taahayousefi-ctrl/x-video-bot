import asyncio
import difflib
import json
import html
import logging
import os
import re
import tempfile
import time
import urllib.error
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
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(1 + attempt)
    raise last_error


def get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(1 + attempt)
    raise last_error


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9آ-ی]+", " ", (value or "").lower()).strip()


def match_score(title: str, artist: str, candidate_title: str, candidate_artist: str) -> float:
    wanted_title, wanted_artist = normalized(title), normalized(artist)
    got_title, got_artist = normalized(candidate_title), normalized(candidate_artist)
    title_score = difflib.SequenceMatcher(None, wanted_title, got_title).ratio()
    artist_score = difflib.SequenceMatcher(None, wanted_artist, got_artist).ratio() if wanted_artist else 0
    return title_score * 0.7 + artist_score * 0.3


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
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 4,
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
        page = get_text(url)
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


def download_audio_file(audio_url: str, path: str) -> None:
    request = urllib.request.Request(audio_url, headers={"User-Agent": "spotify-telegram-bot/1.0"})
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, open(path, "wb") as output:
                while chunk := response.read(1024 * 256):
                    output.write(chunk)
                    if output.tell() > MAX_FILE_SIZE_MB * 1024 * 1024:
                        raise ValueError("فایل صوتی بزرگ‌تر از سقف مجاز تلگرام است")
            return
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(1 + attempt)
    raise last_error


def find_jamendo_audio(title: str, artist: str, tmp_dir: str) -> tuple[str | None, str | None, str | None]:
    """Search Jamendo and accept only a close title/artist match with a license."""
    if not JAMENDO_CLIENT_ID:
        return None, None, None
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
    candidates = sorted(
        data.get("results", []),
        key=lambda track: match_score(title, artist, track.get("name", ""), track.get("artist_name", "")),
        reverse=True,
    )
    for track in candidates[:3]:
        audio_url = track.get("audiodownload") or track.get("audio")
        # Jamendo supplies the license URL; only accept tracks with an explicit license.
        score = match_score(title, artist, track.get("name", ""), track.get("artist_name", ""))
        if not audio_url or not track.get("license_ccurl") or score < 0.72:
            continue
        safe_name = re.sub(r"[^\w.-]+", "_", f"{artist}-{title}", flags=re.UNICODE).strip("_")
        path = os.path.join(tmp_dir, f"{safe_name or 'spotify-track'}.mp3")
        download_audio_file(audio_url, path)
        return path, track.get("license_ccurl"), "Jamendo"
    return None, None, None


def find_archive_audio(title: str, artist: str, tmp_dir: str) -> tuple[str | None, str | None, str | None]:
    """Search Internet Archive audio items with explicit Creative Commons/public-domain rights."""
    query = urllib.parse.quote(
        f'mediatype:audio AND (title:"{title}" OR creator:"{artist}")', safe=""
    )
    search_url = (
        "https://archive.org/advancedsearch.php?q=" + query
        + "&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=licenseurl&output=json&rows=15"
    )
    data = get_json(search_url)
    docs = data.get("response", {}).get("docs", [])
    candidates = []
    for doc in docs:
        license_url = doc.get("licenseurl") or ""
        if not ("creativecommons.org" in license_url or "publicdomain" in license_url.lower()):
            continue
        creator = doc.get("creator", "")
        creator = creator[0] if isinstance(creator, list) else creator
        score = match_score(title, artist, doc.get("title", ""), creator)
        candidates.append((score, doc, license_url))
    for score, doc, license_url in sorted(candidates, reverse=True, key=lambda item: item[0]):
        if score < 0.72:
            continue
        metadata = get_json("https://archive.org/metadata/" + urllib.parse.quote(doc["identifier"], safe=""))
        files = metadata.get("files", [])
        audio = next(
            (item for item in files if str(item.get("name", "")).lower().endswith((".mp3", ".ogg", ".wav", ".flac"))),
            None,
        )
        if not audio:
            continue
        name = os.path.basename(audio["name"])
        path = os.path.join(tmp_dir, re.sub(r"[^\w.-]+", "_", name))
        download_audio_file(
            "https://archive.org/download/" + urllib.parse.quote(doc["identifier"], safe="") + "/" + urllib.parse.quote(audio["name"], safe="/"),
            path,
        )
        return path, license_url, "Internet Archive"
    return None, None, None


def find_licensed_audio(title: str, artist: str, tmp_dir: str) -> tuple[str | None, str | None, str | None]:
    for finder in (find_jamendo_audio, find_archive_audio):
        try:
            path, license_url, source = finder(title, artist, tmp_dir)
            if path:
                return path, license_url, source
        except Exception:
            logger.warning("منبع صوتی %s موقتاً در دسترس نیست", finder.__name__, exc_info=True)
    return None, None, None


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
            file_path = await asyncio.to_thread(download_video, url, tmp_dir)
        except Exception:
            logger.exception("خطا در دانلود ویدیو")
        if not file_path:
            match = TWEET_ID_PATTERN.search(url)
            quoted_url = await asyncio.to_thread(get_quoted_tweet_url, match.group(1)) if match else None
            if quoted_url:
                try:
                    file_path = await asyncio.to_thread(download_video, quoted_url, tmp_dir)
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
    status_msg = await update.message.reply_text("در حال بررسی چند منبع مجاز صوتی... ⏳")
    try:
        metadata = await asyncio.to_thread(spotify_metadata, url)
        title, artist = metadata["title"], metadata["artist"]
    except Exception:
        logger.exception("خطا در خواندن اطلاعات Spotify")
        await status_msg.edit_text("خواندن اطلاعات این لینک Spotify ناموفق بود.")
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            file_path, license_url, source = await asyncio.to_thread(
                find_licensed_audio, title, artist, tmp_dir
            )
        except Exception as error:
            logger.exception("خطا در جست‌وجوی منبع صوتی مجاز")
            await status_msg.edit_text(f"جست‌وجوی فایل صوتی ناموفق بود:\n{error}")
            return
        if not file_path:
            if not JAMENDO_CLIENT_ID:
                reason = "برای فعال‌سازی جست‌وجوی فایل‌های مجاز، متغیر JAMENDO_CLIENT_ID روی سرور تنظیم نشده است."
            else:
                reason = "نسخه‌ی مجاز و قابل‌دانلود این آهنگ در Jamendo یا Internet Archive پیدا نشد."
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
                    caption=f"منبع: {source}\nمجوز: {license_url}",
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
