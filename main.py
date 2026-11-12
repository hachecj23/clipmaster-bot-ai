import os
import re
import glob
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

DURACION_CLIP = 30
CANTIDAD_CLIPS = 3


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 BIENVENIDO A CLIPMASTER AI\n\n"
        "Puedes enviarme:\n\n"
        "🔗 Enlaces de YouTube\n"
        "📱 Videos desde tu teléfono\n"
        "📎 Videos como documentos\n\n"
        "🎬 3 clips automáticos\n"
        "⏱️ 30 segundos cada uno\n"
        "📐 Formato vertical 9:16"
    )


def ejecutar_comando(command, timeout=1800):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout
    )


def obtener_duracion(video):
    resultado = ejecutar_comando([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video
    ])

    if resultado.returncode != 0:
        raise RuntimeError("No se pudo leer la duración del video.")

    return float(resultado.stdout.strip())


def calcular_momentos(duracion):

    if duracion < DURACION_CLIP:
        raise RuntimeError("El video debe durar al menos 30 segundos.")

    max_inicio = duracion - DURACION_CLIP

    return [
        max_inicio * porcentaje
        for porcentaje in [0.15, 0.45, 0.75]
    ]


async def crear_clips(update, video, folder):

    duracion = await asyncio.to_thread(
        obtener_duracion,
        video
    )

    momentos = calcular_momentos(duracion)

    await update.message.reply_text(
        f"🎬 Video recibido.\n"
        f"⏱️ Duración: {int(duracion // 60)} minutos.\n"
        f"🔥 Creando {CANTIDAD_CLIPS} clips..."
    )

    enviados = 0

    for numero, inicio in enumerate(momentos, start=1):

        clip = os.path.join(folder, f"clip_{numero}.mp4")

        ffmpeg = [
            "ffmpeg", "-y",
            "-ss", str(inicio),
            "-i", video,
            "-t", str(DURACION_CLIP),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-vf",
            "scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "96k",
            "-threads", "2",
            "-movflags", "+faststart",
            clip
        ]

        resultado = await asyncio.to_thread(
            ejecutar_comando,
            ffmpeg,
            600
        )

        if resultado.returncode != 0 or not os.path.exists(clip):
            await update.message.reply_text(
                f"⚠️ No se pudo crear el clip {numero}."
            )
            continue

        if os.path.getsize(clip) > 45 * 1024 * 1024:
            await update.message.reply_text(
                f"⚠️ El clip {numero} supera el tamaño permitido."
            )
            continue

        with open(clip, "rb") as archivo:
            await update.message.reply_video(
                video=archivo,
                caption=f"🔥 CLIPMASTER AI | Clip {numero}/3"
            )

        enviados += 1

    await update.message.reply_text(
        f"✅ Procesamiento terminado.\n"
        f"Clips enviados: {enviados}/3"
    )


async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    mensaje = update.message

    archivo_telegram = mensaje.video or mensaje.document

    if not archivo_telegram:
        return

    if mensaje.document:
        mime = mensaje.document.mime_type or ""

        if not mime.startswith("video/"):
            await mensaje.reply_text(
                "❌ El documento debe ser un archivo de video."
            )
            return

    if archivo_telegram.file_size and archivo_telegram.file_size > 20 * 1024 * 1024:
        await mensaje.reply_text(
            "❌ Este video supera los 20 MB permitidos por la descarga "
            "estándar de Telegram.\n\n"
            "Necesitamos configurar el servidor de archivos grandes."
        )
        return

    await mensaje.reply_text(
        "📥 Recibiendo tu video. Preparando el procesamiento..."
    )

    with tempfile.TemporaryDirectory() as folder:

        try:
            ruta = os.path.join(folder, "video.mp4")

            archivo = await archivo_telegram.get_file()

            await archivo.download_to_drive(custom_path=ruta)

            await crear_clips(update, ruta, folder)

        except Exception as error:
            await mensaje.reply_text(
                f"⚠️ Error procesando el video:\n{str(error)[:500]}"
            )


async def process_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = update.message.text.strip()

    if not re.match(r"https?://", url):
        await update.message.reply_text(
            "❌ Envíame un enlace válido."
        )
        return

    await update.message.reply_text(
        "⏳ Recibí tu enlace. Descargando el video..."
    )

    with tempfile.TemporaryDirectory() as folder:

        try:
            patron = os.path.join(folder, "video.%(ext)s")

            command = [
                "yt-dlp",
                "--no-playlist",
                "--max-filesize", "500M",
                "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
                "--merge-output-format", "mp4",
                "-o", patron,
                url
            ]

            resultado = await asyncio.to_thread(
                ejecutar_comando,
                command,
                1800
            )

            archivos = glob.glob(
                os.path.join(folder, "video.*")
            )

            archivos = [
                archivo for archivo in archivos
                if not archivo.endswith((".part", ".ytdl"))
            ]

            if resultado.returncode != 0 or not archivos:

                error = resultado.stderr[-2000:]

                await update.message.reply_text(
                    "❌ ERROR DE DESCARGA:\n\n" + error[:3000]
                )
                return

            await crear_clips(
                update,
                archivos[0],
                folder
            )

        except Exception as error:
            await update.message.reply_text(
                f"⚠️ Error procesando el enlace:\n{str(error)[:500]}"
            )


def main():

    if not TOKEN:
        raise RuntimeError("Falta configurar BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            recibir_video
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_youtube
        )
    )

    print("CLIPMASTER AI iniciado")

    app.run_polling()


if __name__ == "__main__":
    main()
