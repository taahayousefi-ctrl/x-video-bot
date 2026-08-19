import os
import re
import json
import logging
import tempfile
import urllib.request

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

X_LINK_PATTERN = re.compile(
    r"https?://(www\.)?(x\.com|twitter\.com)/\S+", re.IGNORECASE
)

MAX_FILE_SIZE_MB = 50

TWEET_ID_PATTERN = re.compile(r"status/(\d+)")


def get_quoted_tweet_url(tweet_id: str) -> str | None:
    """
    اگه توییت داده‌شده یه کوت‌توییت باشه، لینک توییت اصلی (که ویدیو توشه) رو برمی‌گردونه.
    از API غیررسمی syndication توییتر استفاده می‌کنه (همونی که برای embed کردن به کار می‌ره).
    """
    endpoint = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=a"
    try:
        req = urllib.request.Request(
            endpoint, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        logger.exception("خطا در گرفتن اطلاعات توییت کوت‌شده")
        return None

    quoted = data.get("quoted_tweet")
    if not quoted:
        return None

    quoted_id = quoted.get("id_str") or quoted.get("id")
    author = (quoted.get("user") or {}).get("screen_name")
    if not quoted_id or not author:
        return None

    return f"https://x.com/{author}/status/{quoted_id}"


def download_video(url: str, tmp_dir: str) -> str | None:
    """
    ویدیو رو دانلود می‌کنه و مسیر فایل واقعی دانلودشده رو برمی‌گردونه.
    اگه هیچ فایل ویدیویی دانلود نشه (مثلاً پست فقط عکس/متن بوده)، None برمی‌گردونه.
    """
    downloaded_files = []

    def hook(d):
        if d.get("status") == "finished" and d.get("filename"):
            downloaded_files.append(d["filename"])

    ydl_opts = {
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": "best",
        "postprocessors": [],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [hook],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    video_extensions = (".mp4", ".mov", ".webm", ".mkv")
    video_files = [
        f for f in downloaded_files
        if f.lower().endswith(video_extensions) and os.path.exists(f)
    ]
    if not video_files:
        return None

    return max(video_files, key=os.path.getsize)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "سلام! فقط کافیه لینک ویدیوی ایکس (X/Twitter) رو برام بفرستی تا دانلودش کنم و برات ارسال کنم."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    match = X_LINK_PATTERN.search(text)

    if not match:
        await update.message.reply_text(
            "این یه لینک معتبر از ایکس (X/Twitter) نیست. لطفاً لینک پست حاوی ویدیو رو بفرست."
        )
        return

    url = match.group(0)
    status_msg = await update.message.reply_text("در حال دانلود ویدیو... ⏳")
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = None
        try:
            file_path = download_video(url, tmp_dir)
        except Exception:
            logger.exception("خطا در دانلود ویدیو")

        if not file_path:
            id_match = TWEET_ID_PATTERN.search(url)
            quoted_url = get_quoted_tweet_url(id_match.group(1)) if id_match else None

            if quoted_url:
                try:
                    file_path = download_video(quoted_url, tmp_dir)
                except Exception:
                    logger.exception("خطا در دانلود ویدیوی توییت کوت‌شده")
                    file_path = None

            if not file_path:
                await status_msg.edit_text(
                    "ویدیویی توی این پست پیدا نشد (نه در خود پست، نه در توییت کوت‌شده احتمالی)."
                )
                return

        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"حجم ویدیو ({file_size_mb:.1f} مگابایت) بیشتر از سقف مجاز تلگرام "
                f"({MAX_FILE_SIZE_MB} مگابایت) برای ارسال از طریق رباته."
            )
            return

        await status_msg.edit_text("در حال ارسال ویدیو... 📤")
        try:
            with open(file_path, "rb") as video_file:
                await update.message.reply_video(video=video_file)
            await status_msg.delete()
        except Exception as e:
            logger.exception("خطا در ارسال ویدیو")
            await status_msg.edit_text(f"ارسال ویدیو ناموفق بود:\n{e}")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "متغیر محیطی BOT_TOKEN تنظیم نشده. قبل از اجرا export BOT_TOKEN=... رو بزن."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
