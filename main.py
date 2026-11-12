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
from urllib.parse import urlparse, parse_qs

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

# ============================================================
# CLIPMASTER AI - Telegram bot para Railway
# Variables requeridas: BOT_TOKEN, TWELVELABS_API_KEY
# Variable opcional: BGUTIL_POT_URL
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
TWELVELABS_API_KEY = os.getenv("TWELVELABS_API_KEY", "").strip()
BGUTIL_POT_URL = os.getenv(
    "BGUTIL_POT_URL",
    "http://bgutil-ytdlp-pot-provider.railway.internal:4416",
).strip()

MODELO = "pegasus1.6"
CANTIDAD_CLIPS = 3
DURACION_MINIMA = 30
DURACION_MAXIMA = 60
DURACION_MAXIMA_VIDEO = 3600
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
TAMANO_MAXIMO_CLIP = 45 * 1024 * 1024
TIEMPO_MAXIMO_DESCARGA = 1800
TIEMPO_MAXIMO_ANALISIS = 3600

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("clipmaster")

cliente_twelvelabs = None


def obtener_cliente():
    global cliente_twelvelabs
    if cliente_twelvelabs is None:
        if not TWELVELABS_API_KEY:
            raise RuntimeError("Falta configurar TWELVELABS_API_KEY en Railway.")
        cliente_twelvelabs = TwelveLabs(api_key=TWELVELABS_API_KEY)
    return cliente_twelvelabs


def ejecutar_comando(comando, timeout):
    proceso = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proceso.returncode != 0:
        detalle = (proceso.stderr or proceso.stdout or "Error sin detalle")[-3500:]
        raise RuntimeError(detalle)
    return proceso.stdout


def validar_youtube(url):
    if not isinstance(url, str):
        return False
    url = url.strip()
    try:
        datos = urlparse(url)
        host = (datos.netloc or "").lower().split(":")[0]
        hosts_validos = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "www.youtu.be",
            "youtube-nocookie.com",
            "www.youtube-nocookie.com",
        }
        return datos.scheme in ("http", "https") and host in hosts_validos
    except Exception:
        return False


def obtener_duracion(ruta):
    salida = ejecutar_comando(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            ruta,
        ],
        60,
    )
    return float(salida.strip())


def localizar_video(carpeta):
    candidatos = []
    for archivo in glob.glob(os.path.join(carpeta, "video.*")):
        if os.path.isfile(archivo) and not archivo.endswith((".part", ".ytdl")):
            candidatos.append(archivo)
    if not candidatos:
        raise RuntimeError("YouTube no entregó un archivo de video descargado.")
    return max(candidatos, key=os.path.getsize)


def descargar_video(url, carpeta):
    patron = os.path.join(carpeta, "video.%(ext)s")
    comando = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--retries", "5",
        "--fragment-retries", "5",
        "--socket-timeout", "30",
        "--max-filesize", str(MAX_VIDEO_BYTES),
        "--extractor-args",
        "youtubepot-bgutilhttp:base_url=" + BGUTIL_POT_URL,
        "-f",
        "best[height<=720][ext=mp4]/best[height<=720]/best",
        "--merge-output-format", "mp4",
        "-o", patron,
        url,
    ]
    ejecutar_comando(comando, TIEMPO_MAXIMO_DESCARGA)
    ruta = localizar_video(carpeta)
    tamano = os.path.getsize(ruta)
    if tamano > MAX_VIDEO_BYTES:
        raise RuntimeError("El video supera el límite de 2 GB.")
    duracion = obtener_duracion(ruta)
    if duracion <= 0:
        raise RuntimeError("No pude determinar la duración del video.")
    if duracion > DURACION_MAXIMA_VIDEO:
        raise RuntimeError("El video supera el límite de 60 minutos.")
    return ruta, duracion


def esperar_asset(cliente, asset_id):
    inicio = time.time()
    while time.time() - inicio < TIEMPO_MAXIMO_ANALISIS:
        asset = cliente.assets.retrieve(asset_id)
        estado = str(getattr(asset, "status", "")).lower()
        if estado == "ready":
            return
        if estado in ("failed", "error"):
            raise RuntimeError("TwelveLabs no pudo procesar el video.")
        time.sleep(8)
    raise TimeoutError("TwelveLabs tardó demasiado procesando el video.")


def subir_a_twelvelabs(ruta):
    cliente = obtener_cliente()
    resultado = cliente.multipart_upload.upload_file(
        ruta,
        file_type="video",
        max_workers=3,
        max_retries=3,
    )
    asset_id = getattr(resultado, "asset_id", None)
    if not asset_id:
        raise RuntimeError("TwelveLabs no devolvió el ID del video.")
    esperar_asset(cliente, asset_id)
    return asset_id


def analizar_video(asset_id, duracion):
    cliente = obtener_cliente()
    prompt = f"""
Analiza el video completo y encuentra exactamente {CANTIDAD_CLIPS} momentos
que tengan alto potencial para convertirse en clips cortos virales.
Prioriza momentos impactantes, sorprendentes, emocionantes, graciosos,
útiles o con una frase que atrape al público desde el primer segundo.
Cada clip debe durar entre {DURACION_MINIMA} y {DURACION_MAXIMA} segundos.
El video dura aproximadamente {duracion:.1f} segundos.

Devuelve SOLAMENTE un JSON válido, sin introducción ni Markdown, con este formato:
{{
  "clips": [
    {{
      "inicio": 12.5,
      "fin": 55.0,
      "titulo": "Título breve en español",
      "motivo": "Por qué este momento puede funcionar"
    }}
  ]
}}
Los tiempos inicio y fin deben estar expresados en segundos desde el inicio
del video. No inventes escenas ni uses tiempos fuera de la duración.
Devuelve exactamente {CANTIDAD_CLIPS} clips distintos y evita solapamientos.
"""
    tarea_creada = cliente.analyze_async.tasks.create(
        model_name=MODELO,
        video=VideoContext_AssetId(asset_id=asset_id),
        prompt=prompt,
        max_tokens=4096,
    )

    # TwelveLabs devuelve task_id (no id) al crear una tarea.
    task_id = getattr(tarea_creada, "task_id", None)
    if not task_id:
        raise RuntimeError(
            "TwelveLabs no devolvió task_id al crear el análisis. "
            f"Respuesta recibida: {tarea_creada!r}"
        )

    inicio = time.time()
    while time.time() - inicio < TIEMPO_MAXIMO_ANALISIS:
        tarea = cliente.analyze_async.tasks.retrieve(task_id)
        estado = str(getattr(tarea, "status", "")).lower()

        if estado in ("ready", "completed", "succeeded"):
            resultado = getattr(tarea, "result", None)
            if resultado is None:
                raise RuntimeError("El análisis terminó sin devolver resultados.")
            datos = getattr(resultado, "data", None)
            if datos is None:
                datos = str(resultado)
            return datos

        if estado in ("failed", "canceled", "error"):
            error_info = getattr(tarea, "error", None)
            detalle = getattr(error_info, "message", None) or str(error_info or estado)
            raise RuntimeError(f"TwelveLabs no pudo completar el análisis: {detalle}")
        time.sleep(8)

    raise TimeoutError("El análisis superó el tiempo máximo de espera.")


def extraer_json(texto):
    if isinstance(texto, dict):
        return texto
    texto = str(texto).strip()
    texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\s*```$", "", texto)
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        inicio = texto.find("{")
        fin = texto.rfind("}")
        if inicio >= 0 and fin > inicio:
            return json.loads(texto[inicio:fin + 1])
        raise RuntimeError("La IA no devolvió un JSON válido de clips.")


def validar_clips(texto, duracion_video):
    datos = extraer_json(texto)
    clips = datos.get("clips", []) if isinstance(datos, dict) else []
    validos = []

    for clip in clips:
        try:
            inicio = float(clip.get("inicio"))
            fin = float(clip.get("fin"))
            titulo = str(clip.get("titulo", "Clip de video"))[:90]
            motivo = str(clip.get("motivo", ""))[:250]

            if inicio < 0 or fin > duracion_video or fin <= inicio:
                continue
            duracion = fin - inicio
            if duracion < DURACION_MINIMA or duracion > DURACION_MAXIMA:
                continue

            validos.append({
                "inicio": inicio,
                "fin": fin,
                "titulo": titulo,
                "motivo": motivo,
            })
        except (TypeError, ValueError):
            continue

    validos.sort(key=lambda x: x["inicio"])
    seleccionados = []
    for clip in validos:
        if all(
            clip["fin"] <= otro["inicio"] or clip["inicio"] >= otro["fin"]
            for otro in seleccionados
        ):
            seleccionados.append(clip)
        if len(seleccionados) == CANTIDAD_CLIPS:
            break

    if not seleccionados:
        raise RuntimeError(
            "No encontré momentos válidos de 30 a 60 segundos. "
            "Prueba con otro video."
        )
    return seleccionados


def crear_clip_vertical(video, clip, carpeta, numero):
    salida = os.path.join(carpeta, f"clip_{numero}.mp4")
    duracion = clip["fin"] - clip["inicio"]

    comando = [
        "ffmpeg",
        "-y",
        "-ss", f"{clip['inicio']:.3f}",
        "-i", video,
        "-t", f"{duracion:.3f}",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920",
        "-r", "30",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-maxrate", "2500k",
        "-bufsize", "5000k",
        salida,
    ]
    ejecutar_comando(comando, 900)

    if not os.path.exists(salida):
        raise RuntimeError("FFmpeg no generó el clip.")
    if os.path.getsize(salida) > TAMANO_MAXIMO_CLIP:
        # Segunda compresión para cumplir el límite de envío de Telegram.
        comando[comando.index("-crf") + 1] = "32"
        comando[comando.index("-maxrate") + 1] = "1500k"
        ejecutar_comando(comando, 900)

    if os.path.getsize(salida) > TAMANO_MAXIMO_CLIP:
        raise RuntimeError("El clip sigue superando el límite de 45 MB.")
    return salida


async def enviar_estado(update, texto):
    await update.message.reply_text(texto)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "🎬 ¡Bienvenido a CLIPMASTER AI!\n\n"
        "Envíame un enlace público de YouTube y prepararé hasta 3 clips "
        "verticales 9:16 de entre 30 y 60 segundos.\n\n"
        "Límite: videos de hasta 60 minutos."
    )
    await update.message.reply_text(mensaje)


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Envía un enlace de YouTube (youtube.com o youtu.be). "
        "El bot analizará el video y te enviará los mejores clips."
    )


async def procesar_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (update.message.text or "").strip()
    coincidencia = re.search(r"https?://\S+", mensaje)
    url = coincidencia.group(0).rstrip(").,]}>") if coincidencia else ""

    if not validar_youtube(url):
        await update.message.reply_text(
            "⚠️ Envíame un enlace válido de YouTube."
        )
        return

    carpeta = tempfile.mkdtemp(prefix="clipmaster_")
    try:
        await update.message.reply_text(
            "📥 Recibí el enlace. Estoy descargando el video..."
        )
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )

        video, duracion = await asyncio.to_thread(
            descargar_video, url, carpeta
        )
        await update.message.reply_text(
            f"✅ Video descargado ({int(duracion // 60)} min "
            f"{int(duracion % 60)} s).\n"
            "🧠 Ahora la IA está analizando los mejores momentos..."
        )

        asset_id = await asyncio.to_thread(subir_a_twelvelabs, video)
        resultado = await asyncio.to_thread(
            analizar_video, asset_id, duracion
        )
        clips = validar_clips(resultado, duracion)

        await update.message.reply_text(
            f"✂️ Encontré {len(clips)} momentos. Creando clips verticales..."
        )

        enviados = 0
        for numero, clip in enumerate(clips, start=1):
            ruta_clip = await asyncio.to_thread(
                crear_clip_vertical, video, clip, carpeta, numero
            )
            caption = (
                f"🎬 CLIP {numero}\n"
                f"{clip['titulo']}\n"
                f"⏱ {int(clip['inicio'])}s - {int(clip['fin'])}s"
            )
            with open(ruta_clip, "rb") as archivo:
                await update.message.reply_video(
                    video=archivo,
                    caption=caption[:1024],
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=60,
                )
            enviados += 1

        await update.message.reply_text(
            f"✅ ¡Proceso terminado! Te envié {enviados} clip(s) en formato 9:16."
        )

    except subprocess.TimeoutExpired:
        logger.exception("Tiempo agotado en un proceso externo")
        await update.message.reply_text(
            "⏳ Una operación tardó demasiado. Inténtalo otra vez con otro video."
        )
    except Exception as error:
        logger.exception("Error procesando video")
        detalle = str(error).strip()
        if len(detalle) > 900:
            detalle = detalle[-900:]
        await update.message.reply_text(
            "❌ No pude completar el procesamiento.\n\n"
            f"Detalle: {detalle or 'Error desconocido'}"
        )
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Error no controlado del bot", exc_info=context.error)


def main():
    if not TOKEN:
        raise RuntimeError("Falta configurar BOT_TOKEN en Railway.")
    if not TWELVELABS_API_KEY:
        raise RuntimeError("Falta configurar TWELVELABS_API_KEY en Railway.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_video))
    app.add_error_handler(error_handler)

    logger.info("CLIPMASTER AI iniciado correctamente.")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
