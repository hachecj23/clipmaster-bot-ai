import os
import re
import asyncio
import tempfile
import subprocess

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

TOKEN = os.getenv("BOT_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 BIENVENIDO A CLIPMASTER AI\n\n"
        "Envíame un enlace de YouTube para comenzar.\n\n"
        "🎬 Clips automáticos\n"
        "📱 Formato vertical\n"
        "⏱️ Clips de 30, 60 y 90 segundos\n"
        "📝 Próximamente: subtítulos inteligentes."
    )


async def process_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = update.message.text.strip()

    if not re.match(r"https?://", url):
        await update.message.reply_text("❌ Envíame un enlace válido.")
        return

    await update.message.reply_text(
        "⏳ Recibí tu enlace. Preparando el video..."
    )

    with tempfile.TemporaryDirectory() as folder:

        output = os.path.join(folder, "video.mp4")

        try:
            command = [
                "yt-dlp",
                "--no-playlist",
                "--max-filesize", "500M",
                "-f", "best[height<=720][ext=mp4]/best[height<=720]",
                "--merge-output-format", "mp4",
                "-o", output,
                url
            ]

            result = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                timeout=1800
            )

            if result.returncode != 0 or not os.path.exists(output):
                error = result.stderr[-2500:] if result.stderr else "Error desconocido"

                await update.message.reply_text(
                    "❌ ERROR DE DESCARGA:\n\n" + error[:3500]
                )
                return
            await update.message.reply_text(
                "🎬 Video recibido. Creando clips..."
            )

            clip = os.path.join(folder, "clip.mp4")

            ffmpeg = [
                "ffmpeg", "-y",
                "-ss", "0",
                "-i", output,
                "-t", "45",
                "-vf",
                "scale=720:1280:force_original_aspect_ratio=increase,"
                "crop=720:1280",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "28",
                "-c:a", "aac",
                "-b:a", "96k",
                "-movflags", "+faststart",
                clip
            ]

            process = await asyncio.to_thread(
                subprocess.run,
                ffmpeg,
                capture_output=True,
                text=True,
                timeout=600
            )

            if process.returncode != 0 or not os.path.exists(clip):
                await update.message.reply_text(
                    "❌ Error al crear el clip."
                )
                return

            if os.path.getsize(clip) > 45 * 1024 * 1024:
                await update.message.reply_text(
                    "❌ El clip supera el tamaño permitido."
                )
                return

            with open(clip, "rb") as video:
                await update.message.reply_video(
                    video=video,
                    caption="🔥 CLIPMASTER AI | Clip vertical"
                )

        except Exception as error:
            await update.message.reply_text(
                f"⚠️ Error procesando el video: {str(error)[:300]}"
            )


def main():
    if not TOKEN:
        raise RuntimeError("Falta configurar BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, process_video)
    )

    print("CLIPMASTER AI iniciado")

    app.run_polling()


if __name__ == "__main__":
    main()
