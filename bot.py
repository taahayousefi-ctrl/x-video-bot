import os
import re
import logging
import tempfile

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
        output_template = os.path.join(tmp_dir, "%(id)s.%(ext)s")
        ydl_opts = {
            "outtmpl": output_template,
            "format": "best",
            "postprocessors": [],
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info)
        except Exception as e:
            logger.exception("خطا در دانلود ویدیو")
            await status_msg.edit_text(f"دانلود ویدیو ناموفق بود:\n{e}")
            return

        if not os.path.exists(file_path):
            await status_msg.edit_text("ویدیویی توی این پست پیدا نشد.")
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
