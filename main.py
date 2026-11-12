import os
import re
import json
import glob
import asyncio
import tempfile
import subprocess

from google import genai
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MODELO = "gemini-3.8-flash"
CANTIDAD_CLIPS = 3
DURACION_CLIP = 45


def ejecutar(command, timeout=1800):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 CLIPMASTER AI\n\n"
        "Envíame un enlace público de YouTube.\n\n"
        "🧠 Análisis inteligente con Gemini\n"
        "🎬 Detección de mejores momentos\n"
        "📱 Clips verticales 9:16\n"
        "🔥 3 clips de 45 segundos"
    )


def analizar_video_gemini(url):

    client = genai.Client(api_key=GEMINI_API_KEY)

    instrucciones = """
    Analiza el video completo de YouTube.

    Tu objetivo es encontrar los 3 momentos más interesantes
    para crear videos virales de TikTok, Facebook Reels y Shorts.

    Busca:
    - Momentos sorprendentes.
    - Conversaciones interesantes.
    - Escenas emocionantes.
    - Frases impactantes.
    - Momentos que despierten curiosidad.
    - Historias con principio y final.

    Cada fragmento debe durar aproximadamente 45 segundos.

    No selecciones los primeros segundos por defecto.
    Busca momentos distribuidos por todo el video.

    Devuelve EXCLUSIVAMENTE JSON con esta estructura:

    {
      "clips": [
        {
          "inicio": 120,
          "fin": 165,
          "titulo": "Título atractivo",
          "motivo": "Por qué es interesante"
        }
      ]
    }

    Los tiempos deben expresarse en segundos.
    No inventes marcas de tiempo.
    """

    respuesta = client.interactions.create(
        model=MODELO,
        input=[
            {
                "type": "video",
                "uri": url
            },
            {
                "type": "text",
                "text": instrucciones
            }
        ]
    )

    texto = respuesta.output_text

    coincidencia = re.search(
        r"\{.*\}",
        texto,
        re.DOTALL
    )

    if not coincidencia:
        raise RuntimeError(
            "Gemini no devolvió una selección válida."
        )

    datos = json.loads(coincidencia.group())

    clips = datos.get("clips", [])

    if not clips:
        raise RuntimeError("Gemini no encontró momentos.")

    return clips[:CANTIDAD_CLIPS]


async def descargar_video(url, folder):

    patron = os.path.join(folder, "video.%(ext)s")

    comando = [
        "yt-dlp",
        "--no-playlist",
        "--max-filesize", "2G",
        "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "-o", patron,
        url
    ]

    resultado = await asyncio.to_thread(
        ejecutar,
        comando,
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

        raise RuntimeError(
            "No se pudo obtener el video original.\n"
            + resultado.stderr[-1200:]
        )

    return archivos[0]


async def crear_clips(update, video, momentos, folder):

    enviados = 0

    for numero, momento in enumerate(momentos, start=1):

        inicio = max(0, float(momento["inicio"]))
        fin = float(momento["fin"])

        duracion = fin - inicio

        if duracion < 20 or duracion > 90:
            continue

        clip = os.path.join(
            folder,
            f"clip_{numero}.mp4"
        )

        comando = [
            "ffmpeg", "-y",
            "-ss", str(inicio),
            "-i", video,
            "-t", str(duracion),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-vf",
            "scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "27",
            "-c:a", "aac",
            "-b:a", "96k",
            "-threads", "2",
            "-movflags", "+faststart",
            clip
        ]

        resultado = await asyncio.to_thread(
            ejecutar,
            comando,
            600
        )

        if resultado.returncode != 0:
            continue

        if not os.path.exists(clip):
            continue

        if os.path.getsize(clip) > 45 * 1024 * 1024:
            await update.message.reply_text(
                f"⚠️ El clip {numero} supera el límite de Telegram."
            )
            continue

        titulo = momento.get(
            "titulo",
            f"Mejor momento {numero}"
        )

        with open(clip, "rb") as archivo:

            await update.message.reply_video(
                video=archivo,
                caption=(
                    f"🔥 CLIPMASTER AI\n"
                    f"🎬 {titulo}\n"
                    f"📱 Formato 9:16"
                )
            )

        enviados += 1

    await update.message.reply_text(
        f"✅ PROCESAMIENTO TERMINADO\n"
        f"Clips enviados: {enviados}/{CANTIDAD_CLIPS}"
    )


async def process_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = update.message.text.strip()

    if not re.match(
        r"https?://(www\.)?(youtube\.com|youtu
