import os
import re
import json
import glob
import time
import shutil
import asyncio
import tempfile
import subprocess
import logging

from pathlib import Path
from urllib.parse import urlparse

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from twelvelabs import TwelveLabs
from twelvelabs.types import VideoContext_AssetId


# ==========================================
# CONFIGURACIÓN
# ==========================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("TWELVELABS_API_KEY", "").strip()

BGUTIL_POT_URL = os.getenv(
    "BGUTIL_POT_URL",
    "http://bgutil-ytdlp-pot-provider.railway.internal:4416"
).strip()

# Cookies opcionales de YouTube.
# Se guardan como variable secreta en Railway.
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "")

MODELO = "pegasus1.6"

CANTIDAD_CLIPS = 3
DURACION_MINIMA = 30
DURACION_MAXIMA = 60
DURACION_MAXIMA_VIDEO = 3600

MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_CLIP_BYTES = 45 * 1024 * 1024

TIEMPO_DESCARGA = 1800
TIEMPO_ANALISIS = 3600


# ==========================================
# LOGS
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("CLIPMASTER_AI")

cliente = None


# ==========================================
# TWELVELABS
# ==========================================

def obtener_cliente():

    global cliente

    if cliente is None:

        if not API_KEY:
            raise RuntimeError(
                "Falta TWELVELABS_API_KEY en Railway."
            )

        cliente = TwelveLabs(api_key=API_KEY)

    return cliente


# ==========================================
# EJECUTAR COMANDOS
# ==========================================

def ejecutar_comando(comando, timeout=900):

    proceso = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        timeout=timeout
    )

    salida = (proceso.stdout or "") + "\n" + (proceso.stderr or "")

    if comando and Path(str(comando[0])).name.lower().startswith("yt-dlp"):

        for linea in salida.splitlines():

            if any(x in linea.lower() for x in [
                "po token",
                "bgutil",
                "generating",
                "player client",
                "login_required",
                "cookies"
            ]):

                logger.info("YT-DLP: %s", linea[-1200:])

    if proceso.returncode != 0:

        detalle = (
            proceso.stderr
            or proceso.stdout
            or "Error desconocido"
        )

        raise RuntimeError(detalle[-5000:])

    return proceso.stdout


# ==========================================
# VALIDAR YOUTUBE
# ==========================================

def validar_youtube(url):

    try:

        datos = urlparse(url)

        host = datos.netloc.lower().split(":")[0]

        permitidos = [
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "www.youtu.be"
        ]

        return (
            datos.scheme in ["http", "https"]
            and host in permitidos
        )

    except Exception:

        return False


# ==========================================
# DESCARGA DE YOUTUBE
# ==========================================

def descargar_video(url, carpeta):

    patron = os.path.join(
        carpeta,
        "video.%(ext)s"
    )

    comando = [
        "yt-dlp",

        "--verbose",
        "--force-ipv4",
        "--no-playlist",
        "--no-warnings",

        "--retries", "5",
        "--fragment-retries", "5",
        "--socket-timeout", "30",

        "--max-filesize", str(MAX_VIDEO_BYTES),

        "--extractor-args",
        f"youtubepot-bgutilhttp:base_url={BGUTIL_POT_URL}",

        "--extractor-args",
        "youtube:player-client=mweb,tv,web_safari",

        "-f",
        "best[height<=720][ext=mp4]/best[height<=720]/best",

        "--merge-output-format",
        "mp4",

        "-o",
        patron
    ]

    archivo_cookies = None

    if YOUTUBE_COOKIES.strip():

        archivo_cookies = os.path.join(
            carpeta,
            "youtube_cookies.txt"
        )

        with open(
            archivo_cookies,
            "w",
            encoding="utf-8"
        ) as archivo:

            archivo.write(YOUTUBE_COOKIES.strip())

        comando.extend([
            "--cookies",
            archivo_cookies
        ])

        logger.info("Cookies de YouTube configuradas.")

    comando.append(url)

    logger.info("Iniciando descarga de YouTube.")

    ejecutar_comando(
        comando,
        TIEMPO_DESCARGA
    )

    candidatos = glob.glob(
        os.path.join(carpeta, "video.*")
    )

    candidatos = [
        x for x in candidatos
        if os.path.isfile(x)
        and not x.endswith((".part", ".ytdl"))
    ]

    if not candidatos:

        raise RuntimeError(
            "YouTube no entregó ningún archivo."
        )

    video = max(
        candidatos,
        key=os.path.getsize
    )

    if os.path.getsize(video) > MAX_VIDEO_BYTES:

        raise RuntimeError(
            "El video supera los 2 GB."
        )

    salida = ejecutar_comando([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video
    ], 60)

    duracion = float(salida.strip())

    if duracion <= 0:

        raise RuntimeError(
            "No se pudo determinar la duración."
        )

    if duracion > DURACION_MAXIMA_VIDEO:

        raise RuntimeError(
            "El video supera el límite de 60 minutos."
        )

    return video, duracion


# ==========================================
# SUBIR VIDEO A TWELVELABS
# ==========================================

def subir_video(video):

    api = obtener_cliente()

    resultado = api.multipart_upload.upload_file(
        video,
        file_type="video",
        max_workers=3,
        max_retries=3
    )

    asset_id = getattr(
        resultado,
        "asset_id",
        None
    )

    if not asset_id:

        raise RuntimeError(
            "TwelveLabs no devolvió asset_id."
        )

    inicio = time.time()

    while time.time() - inicio < TIEMPO_ANALISIS:

        asset = api.assets.retrieve(asset_id)

        estado = str(
            getattr(asset, "status", "")
        ).lower()

        logger.info(
            "Estado del video: %s",
            estado
        )

        if estado == "ready":
            return asset_id

        if estado in ["failed", "error"]:

            raise RuntimeError(
                "TwelveLabs no pudo procesar el video."
            )

        time.sleep(8)

    raise TimeoutError(
        "Tiempo agotado esperando el procesamiento."
    )


# ==========================================
# ANALIZAR VIDEO
# ==========================================

def analizar_video(asset_id, duracion):

    api = obtener_cliente()

    prompt = f"""
Analiza el video completo.

Encuentra exactamente 3 momentos interesantes
que puedan convertirse en clips virales.

Cada clip debe durar entre 30 y 60 segundos.

Busca momentos:
- Impactantes.
- Emocionantes.
- Sorprendentes.
- Graciosos.
- Con frases interesantes.
- Que atrapen la atención.

El video dura {duracion} segundos.

Devuelve solamente JSON válido:

{{
 "clips": [
  {{
   "inicio": 10,
   "fin": 50,
   "titulo": "Título del clip",
   "motivo": "Motivo de selección"
  }}
 ]
}}

Los tiempos deben estar expresados en segundos.

No inventes escenas.
No uses tiempos fuera del video.
Evita momentos repetidos.
"""

    tarea_creada = api.analyze_async.tasks.create(
        model_name=MODELO,
        video=VideoContext_AssetId(
            asset_id=asset_id
        ),
        prompt=prompt,
        max_tokens=4096
    )

    task_id = getattr(
        tarea_creada,
        "task_id",
        None
    )

    if not task_id:

        raise RuntimeError(
            "TwelveLabs no devolvió task_id: "
            + repr(tarea_creada)
        )

    inicio = time.time()

    while time.time() - inicio < TIEMPO_ANALISIS:

        tarea = api.analyze_async.tasks.retrieve(
            task_id
        )

        estado = str(
            getattr(tarea, "status", "")
        ).lower()

        logger.info(
            "Estado del análisis: %s",
            estado
        )

        if estado in [
            "ready",
            "completed",
            "succeeded"
        ]:

            resultado = getattr(
                tarea,
                "result",
                None
            )

            if resultado is None:

                raise RuntimeError(
                    "El análisis terminó sin resultados."
                )

            datos = getattr(
                resultado,
                "data",
                None
            )

            return datos if datos is not None else str(resultado)

        if estado in [
            "failed",
            "canceled",
            "error"
        ]:

            raise RuntimeError(
                "TwelveLabs no pudo completar el análisis."
            )

        time.sleep(8)

    raise TimeoutError(
        "Tiempo agotado durante el análisis."
    )


# ==========================================
# LEER RESPUESTA JSON
# ==========================================

def extraer_json(texto):

    if isinstance(texto, dict):
        return texto

    texto = str(texto).strip()

    texto = re.sub(
        r"^```(?:json)?\s*",
        "",
        texto,
        flags=re.IGNORECASE
    )

    texto = re.sub(
        r"\s*```$",
        "",
        texto
    )

    try:

        return json.loads(texto)

    except json.JSONDecodeError:

        inicio = texto.find("{")
        final = texto.rfind("}")

        if inicio >= 0 and final > inicio:

            return json.loads(
                texto[inicio:final + 1]
            )

        raise RuntimeError(
            "La IA no devolvió JSON válido."
        )


# ==========================================
# VALIDAR CLIPS
# ==========================================

def validar_clips(resultado, duracion_video):

    datos = extraer_json(resultado)

    clips = datos.get("clips", [])

    validos = []

    for clip in clips:

        try:

            inicio = float(clip["inicio"])
            fin = float(clip["fin"])

            if inicio < 0:
                continue

            if fin > duracion_video:
                continue

            if fin <= inicio:
                continue

            duracion = fin - inicio

            if duracion < DURACION_MINIMA:
                continue

            if duracion > DURACION_MAXIMA:
                continue

            validos.append({
                "inicio": inicio,
                "fin": fin,
                "titulo": str(
                    clip.get("titulo", "Clip")
                ),
                "motivo": str(
                    clip.get("motivo", "")
                )
            })

        except Exception:

            continue

    seleccionados = []

    validos.sort(
        key=lambda x: x["inicio"]
    )

    for clip in validos:

        se_cruza = any(
            not (
                clip["fin"] <= otro["inicio"]
                or clip["inicio"] >= otro["fin"]
            )
            for otro in seleccionados
        )

        if not se_cruza:

            seleccionados.append(clip)

        if len(seleccionados) == CANTIDAD_CLIPS:
            break

    if not seleccionados:

        raise RuntimeError(
            "No se encontraron momentos válidos."
        )

    return seleccionados


# ==========================================
# CREAR CLIP VERTICAL
# ==========================================

def crear_clip(video, clip, carpeta, numero):

    salida = os.path.join(
        carpeta,
        f"clip_{numero}.mp4"
    )

    duracion = (
        clip["fin"] - clip["inicio"]
    )

    comando = [
        "ffmpeg",
        "-y",

        "-ss",
        str(clip["inicio"]),

        "-i",
        video,

        "-t",
        str(duracion),

        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",

        "-r", "30",

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",

        "-c:a", "aac",
        "-b:a", "128k",

        "-movflags", "+faststart",

        "-maxrate", "2500k",
        "-bufsize", "5000k",

        salida
    ]

    ejecutar_comando(
        comando,
        900
    )

    if not os.path.exists(salida):

        raise RuntimeError(
            "FFmpeg no creó el clip."
        )

    if os.path.getsize(salida) > MAX_CLIP_BYTES:

        comando[comando.index("-crf") + 1] = "32"

        comando[
            comando.index("-maxrate") + 1
        ] = "1500k"

        ejecutar_comando(
            comando,
            900
        )

    if os.path.getsize(salida) > MAX_CLIP_BYTES:

        raise RuntimeError(
            "El clip supera el límite de 45 MB."
        )

    return salida


# ==========================================
# TELEGRAM: INICIO
# ==========================================

async def start(update, context):

    await update.message.reply_text(
        "🎬 ¡Bienvenido a CLIPMASTER AI!\n\n"
        "Envíame un enlace público de YouTube.\n\n"
        "Prepararé hasta 3 clips verticales 9:16 "
        "de entre 30 y 60 segundos.\n\n"
        "Límite: videos de hasta 60 minutos."
    )


async def ayuda(update, context):

    await update.message.reply_text(
        "Envíame un enlace público de YouTube "
        "para comenzar."
    )


# ==========================================
# PROCESAR VIDEO
# ==========================================

async def procesar_video(update, context):

    mensaje = (
        update.message.text or ""
    ).strip()

    coincidencia = re.search(
        r"https?://\S+",
        mensaje
    )

    url = (
        coincidencia.group(0).rstrip(").,]}>")
        if coincidencia
        else ""
    )

    if not validar_youtube(url):

        await update.message.reply_text(
            "⚠️ Envíame un enlace válido de YouTube."
        )

        return

    carpeta = tempfile.mkdtemp(
        prefix="clipmaster_"
    )

    try:

        await update.message.reply_text(
            "📥 Descargando video..."
        )

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING
        )

        video, duracion = await asyncio.to_thread(
            descargar_video,
            url,
            carpeta
        )

        await update.message.reply_text(
            "✅ Video descargado.\n"
            "🧠 Analizando con TwelveLabs..."
        )

        asset_id = await asyncio.to_thread(
            subir_video,
            video
        )

        resultado = await asyncio.to_thread(
            analizar_video,
            asset_id,
            duracion
        )

        clips = validar_clips(
            resultado,
            duracion
        )

        await update.message.reply_text(
            f"✂️ Encontré {len(clips)} momentos.\n"
            "Preparando clips verticales..."
        )

        enviados = 0

        for numero, clip in enumerate(
            clips,
            start=1
        ):

            ruta = await asyncio.to_thread(
                crear_clip,
                video,
                clip,
                carpeta,
                numero
            )

            caption = (
                f"🎬 CLIP {numero}\n"
                f"{clip['titulo']}\n"
                f"⏱ {int(clip['inicio'])}s - "
                f"{int(clip['fin'])}s"
            )

            with open(ruta, "rb") as archivo:

                await update.message.reply_video(
                    video=archivo,
                    caption=caption[:1024],
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=60
                )

            enviados += 1

        await update.message.reply_text(
            f"✅ ¡Terminado!\n"
            f"Te envié {enviados} clips."
        )

    except subprocess.TimeoutExpired:

        logger.exception(
            "Tiempo agotado."
        )

        await update.message.reply_text(
            "⏳ La operación tardó demasiado. "
            "Intenta con otro video."
        )

    except Exception as error:

        logger.exception(
            "Error procesando video."
        )

        detalle = str(error)

        await update.message.reply_text(
            "❌ No pude completar el procesamiento.\n\n"
            f"Detalle: {detalle[-1500:]}"
        )

    finally:

        shutil.rmtree(
            carpeta,
            ignore_errors=True
        )


# ==========================================
# ERRORES
# ==========================================

async def error_handler(update, context):

    logger.exception(
        "Error no controlado.",
        exc_info=context.error
    )


# ==========================================
# INICIAR BOT
# ==========================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "Falta BOT_TOKEN en Railway."
        )

    if not API_KEY:

        raise RuntimeError(
            "Falta TWELVELABS_API_KEY en Railway."
        )

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("ayuda", ayuda)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            procesar_video
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "CLIPMASTER AI iniciado correctamente."
    )

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":

    main()
