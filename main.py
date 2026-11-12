import os
import re
import json
import glob
import asyncio
import tempfile
import subprocess
import traceback

from google import genai

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)


# ==================================================
# CONFIGURACIÓN
# ==================================================

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MODELO = "gemini-3.8-flash"

CANTIDAD_CLIPS = 3

DURACION_MINIMA = 30
DURACION_MAXIMA = 60

DURACION_MAXIMA_VIDEO = 3600

TAMANO_MAXIMO_CLIP = 45 * 1024 * 1024


# ==================================================
# EJECUTAR COMANDOS
# ==================================================

def ejecutar(comando, timeout=1800):

    return subprocess.run(
        comando,
        capture_output=True,
        text=True,
        timeout=timeout
    )


# ==================================================
# VALIDAR ENLACES
# ==================================================

def validar_youtube(url):

    patron = (
        r"^https?://"
        r"(www\.)?"
        r"(youtube\.com|youtu\.be)/"
    )

    return re.match(patron, url) is not None


# ==================================================
# COMANDO START
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 BIENVENIDO A CLIPMASTER AI\n\n"
        "Envíame un enlace público de YouTube.\n\n"
        "🧠 Inteligencia artificial Gemini\n"
        "🎬 Selección automática de momentos\n"
        "🔥 3 clips automáticos\n"
        "⏱️ Videos de hasta 60 minutos\n"
        "📱 Formato vertical 9:16\n"
        "🎞️ Clips de 30 a 60 segundos"
    )


# ==================================================
# ANALIZAR VIDEO CON GEMINI
# ==================================================

def analizar_video_gemini(url):

    client = genai.Client(
        api_key=GEMINI_API_KEY
    )

    instrucciones = """
Analiza el contenido del video completo de YouTube.

Encuentra los tres mejores momentos para crear
videos atractivos para TikTok, Instagram Reels
y Facebook Reels.

Busca:

- Momentos sorprendentes.
- Escenas emocionantes.
- Conversaciones interesantes.
- Frases impactantes.
- Historias que despierten curiosidad.
- Momentos con potencial para captar la atención.

REGLAS:

1. Busca momentos en diferentes partes del video.
2. No selecciones solamente la introducción.
3. Cada clip debe durar entre 30 y 60 segundos.
4. Los tiempos deben expresarse en segundos.
5. No inventes marcas de tiempo.
6. Evita seleccionar momentos repetidos.
7. Los tiempos deben corresponder al video real.

Devuelve exclusivamente JSON válido:

{
  "clips": [
    {
      "inicio": 120,
      "fin": 165,
      "titulo": "Título atractivo",
      "motivo": "Explicación del momento"
    },
    {
      "inicio": 350,
      "fin": 395,
      "titulo": "Segundo momento",
      "motivo": "Explicación del momento"
    },
    {
      "inicio": 700,
      "fin": 745,
      "titulo": "Tercer momento",
      "motivo": "Explicación del momento"
    }
  ]
}
"""

    respuesta = client.interactions.create(
        model=MODELO,
        input=[
            {
                "type": "text",
                "text": instrucciones
            },
            {
                "type": "video",
                "uri": url
            }
        ]
    )

    texto = respuesta.output_text

    if not texto:

        raise RuntimeError(
            "Gemini no devolvió contenido."
        )

    # Limpiar posibles bloques Markdown

    texto = texto.strip()

    texto = re.sub(
        r"^```(?:json)?\s*",
        "",
        texto
    )

    texto = re.sub(
        r"\s*```$",
        "",
        texto
    )

    datos = json.loads(texto)

    clips = datos.get("clips", [])

    if not isinstance(clips, list):

        raise RuntimeError(
            "Gemini devolvió una lista inválida."
        )

    clips_validos = []

    for clip in clips:

        if not isinstance(clip, dict):
            continue

        try:

            inicio = float(clip["inicio"])
            fin = float(clip["fin"])

            duracion = fin - inicio

            if inicio < 0:
                continue

            if DURACION_MINIMA <= duracion <= DURACION_MAXIMA:

                clips_validos.append({
                    "inicio": inicio,
                    "fin": fin,
                    "titulo": str(
                        clip.get(
                            "titulo",
                            "Mejor momento"
                        )
                    ),
                    "motivo": str(
                        clip.get("motivo", "")
                    )
                })

        except (KeyError, TypeError, ValueError):

            continue

    if not clips_validos:

        raise RuntimeError(
            "Gemini no encontró momentos válidos."
        )

    return clips_validos[:CANTIDAD_CLIPS]


# ==================================================
# DESCARGAR VIDEO
# ==================================================

async def descargar_video(url, folder):

    patron = os.path.join(
        folder,
        "video.%(ext)s"
    )

    comando = [
        "yt-dlp",
        "--no-playlist",
        "--max-filesize", "2G",
        "-f",
        "best[height<=720][ext=mp4]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "-o",
        patron,
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
        archivo
        for archivo in archivos
        if not archivo.endswith(
            (".part", ".ytdl")
        )
    ]

    if resultado.returncode != 0 or not archivos:

        error = resultado.stderr[-1800:]

        raise RuntimeError(
            "No se pudo descargar el video:\n" + error
        )

    return archivos[0]


# ==================================================
# OBTENER DURACIÓN
# ==================================================

def obtener_duracion(video):

    resultado = ejecutar([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video
    ])

    if resultado.returncode != 0:

        raise RuntimeError(
            "No se pudo obtener la duración del video."
        )

    return float(
        resultado.stdout.strip()
    )


# ==================================================
# CREAR CLIPS
# ==================================================

async def crear_clips(
    update,
    video,
    momentos,
    folder
):

    duracion_video = await asyncio.to_thread(
        obtener_duracion,
        video
    )

    if duracion_video > DURACION_MAXIMA_VIDEO:

        raise RuntimeError(
            "El video supera el límite de 60 minutos."
        )

    enviados = 0

    for numero, momento in enumerate(
        momentos,
        start=1
    ):

        inicio = momento["inicio"]
        fin = momento["fin"]

        duracion = fin - inicio

        if inicio < 0 or fin > duracion_video:
            continue

        if not DURACION_MINIMA <= duracion <= DURACION_MAXIMA:
            continue

        clip = os.path.join(
            folder,
            f"clip_{numero}.mp4"
        )

        await update.message.reply_text(
            f"🎬 Creando clip {numero} de {len(momentos)}..."
        )

        comando = [
            "ffmpeg",
            "-y",
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

            print(
                resultado.stderr[-2000:],
                flush=True
            )

            await update.message.reply_text(
                f"⚠️ Error creando el clip {numero}."
            )

            continue

        if not os.path.exists(clip):
            continue

        if os.path.getsize(clip) > TAMANO_MAXIMO_CLIP:

            await update.message.reply_text(
                f"⚠️ El clip {numero} supera el tamaño permitido."
            )

            continue

        titulo = momento["titulo"]
        motivo = momento["motivo"]

        with open(clip, "rb") as archivo:

            await update.message.reply_video(
                video=archivo,
                caption=(
                    "🔥 CLIPMASTER AI\n\n"
                    f"🎬 {titulo}\n"
                    f"💡 {motivo}\n\n"
                    "📱 Formato vertical 9:16"
                )
            )

        enviados += 1

    await update.message.reply_text(
        "✅ PROCESAMIENTO TERMINADO\n\n"
        f"Clips enviados: {enviados}/{CANTIDAD_CLIPS}"
    )


# ==================================================
# PROCESAR VIDEO
# ==================================================

async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    url = update.message.text.strip()

    if not validar_youtube(url):

        await update.message.reply_text(
            "❌ Envíame un enlace válido de YouTube."
        )

        return

    mensaje = await update.message.reply_text(
        "🧠 GEMINI ESTÁ ANALIZANDO EL VIDEO...\n\n"
        "Buscando los mejores momentos."
    )

    try:

        # ANALIZAR VIDEO

        momentos = await asyncio.wait_for(
            asyncio.to_thread(
                analizar_video_gemini,
                url
            ),
            timeout=600
        )

        await mensaje.edit_text(
            "🔥 GEMINI ENCONTRÓ LOS MOMENTOS.\n\n"
            "Ahora estoy preparando los clips..."
        )

        # DESCARGAR Y CREAR CLIPS

        with tempfile.TemporaryDirectory() as folder:

            video = await descargar_video(
                url,
                folder
            )

            await crear_clips(
                update,
                video,
                momentos,
                folder
            )

    except asyncio.TimeoutError:

        await update.message.reply_text(
            "⏱️ El análisis tardó demasiado.\n\n"
            "Inténtalo nuevamente más tarde."
        )

    except Exception as error:

        print(
            "ERROR COMPLETO:",
            flush=True
        )

        traceback.print_exc()

        await update.message.reply_text(
            "⚠️ ERROR DE PROCESAMIENTO:\n\n"
            f"{type(error).__name__}: "
            f"{str(error)[:1500]}"
        )


# ==================================================
# INICIAR BOT
# ==================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "Falta configurar BOT_TOKEN en Railway."
        )

    if not GEMINI_API_KEY:

        raise RuntimeError(
            "Falta configurar GEMINI_API_KEY en Railway."
        )

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            process_video
        )
    )

    print(
        "CLIPMASTER AI CON GEMINI INICIADO",
        flush=True
    )

    app.run_polling()


# ==================================================
# EJECUCIÓN
# ==================================================

if __name__ == "__main__":

    main()
