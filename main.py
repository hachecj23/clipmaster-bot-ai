import os
import re
import json
import glob
import time
import asyncio
import tempfile
import subprocess
import traceback

from twelvelabs import TwelveLabs
from twelvelabs.types import VideoContext_AssetId

from telegram import Update
from telegram.ext import (
Application,
CommandHandler,
MessageHandler,
ContextTypes,
filters
)

==================================================

CONFIGURACIÓN

==================================================

TOKEN = os.getenv(“BOT_TOKEN”)
TWELVELABS_API_KEY = os.getenv(“TWELVELABS_API_KEY”)

BGUTIL_POT_URL = os.getenv(
“BGUTIL_POT_URL”,
“http://bgutil-ytdlp-pot-provider.railway.internal:4416”
)

MODELO = “pegasus1.6”

CANTIDAD_CLIPS = 3

DURACION_MINIMA = 30
DURACION_MAXIMA = 60

DURACION_MAXIMA_VIDEO = 3600

TAMANO_MAXIMO_CLIP = 45 * 1024 * 1024

MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024

TIEMPO_MAXIMO_ANALISIS = 3600

==================================================

CLIENTE TWELVELABS

==================================================

def crear_cliente():

if not TWELVELABS_API_KEY:
    raise RuntimeError(
        "Falta configurar TWELVELABS_API_KEY en Railway."
    )
return TwelveLabs(
    api_key=TWELVELABS_API_KEY
)

==================================================

EJECUTAR COMANDOS

==================================================

def ejecutar(comando, timeout=1800):

return subprocess.run(
    comando,
    capture_output=True,
    text=True,
    timeout=timeout
)

==================================================

VALIDAR ENLACES

==================================================

def validar_youtube(url):

patron = (
    r"^https?://"
    r"(www\.)?"
    r"(youtube\.com|youtu\.be)/"
)
return re.match(patron, url) is not None

==================================================

COMANDO START

==================================================

async def start(
update: Update,
context: ContextTypes.DEFAULT_TYPE
):

await update.message.reply_text(
    "🤖 BIENVENIDO A CLIPMASTER AI\n\n"
    "Envíame un enlace público de YouTube.\n\n"
    "🧠 Inteligencia artificial TwelveLabs\n"
    "🎬 Modelo Pegasus 1.6\n"
    "🔥 3 clips automáticos\n"
    "⏱️ Videos de hasta 60 minutos\n"
    "📱 Formato vertical 9:16\n"
    "🎞️ Clips de 30 a 60 segundos"
)

==================================================

OBTENER DURACIÓN

==================================================

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
return float(resultado.stdout.strip())

==================================================

DESCARGAR VIDEO CON BGUTIL PO TOKEN

==================================================

async def descargar_video(url, folder):

patron = os.path.join(
    folder,
    "video.%(ext)s"
)
print(
    "Iniciando descarga de YouTube...",
    flush=True
)
print(
    f"Proveedor PO Token: {BGUTIL_POT_URL}",
    flush=True
)
comando = [
    "yt-dlp",
    "--no-playlist",
    "--max-filesize", "2G",
    "--extractor-args",
    f"youtubepot-bgutilhttp:base_url={BGUTIL_POT_URL}",
    "-f",
    "best[height<=720][ext=mp4]/best[height<=720]/best",
    "--merge-output-format",
    "mp4",
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
    print(
        "ERROR DE DESCARGA:",
        resultado.stderr[-4000:],
        flush=True
    )
    raise RuntimeError(
        "No se pudo descargar el video:\n"
        + resultado.stderr[-1500:]
    )
video = archivos[0]
tamano = os.path.getsize(video)
if tamano > MAX_VIDEO_BYTES:
    raise RuntimeError(
        "El video supera el límite temporal "
        "de 2 GB configurado para Railway."
    )
duracion = await asyncio.to_thread(
    obtener_duracion,
    video
)
if duracion > DURACION_MAXIMA_VIDEO:
    raise RuntimeError(
        "El video supera el límite de 60 minutos."
    )
print(
    f"Descarga terminada. Duración: {duracion:.2f} segundos",
    flush=True
)
return video

==================================================

SUBIR VIDEO A TWELVELABS

==================================================

def subir_video_twelvelabs(video):

client = crear_cliente()
print(
    "Iniciando carga multipart a TwelveLabs...",
    flush=True
)
resultado = client.multipart_upload.upload_file(
    file_path=video,
    filename=os.path.basename(video),
    file_type="video",
    batch_size=5,
    max_workers=2,
    max_retries=3,
    retry_delay=2.0
)
asset_id = resultado.asset_id
if not asset_id:
    raise RuntimeError(
        "TwelveLabs no devolvió el identificador del video."
    )
print(
    f"Video subido. Asset ID: {asset_id}",
    flush=True
)
inicio = time.time()
while True:
    asset = client.assets.retrieve(asset_id)
    estado = str(asset.status).lower()
    print(
        f"Estado del archivo: {estado}",
        flush=True
    )
    if estado == "ready":
        return asset_id
    if estado == "failed":
        raise RuntimeError(
            "TwelveLabs no pudo procesar el archivo."
        )
    if time.time() - inicio > 1800:
        raise TimeoutError(
            "La preparación del video tardó demasiado."
        )
    time.sleep(5)

==================================================

ANALIZAR CON PEGASUS 1.6

==================================================

def analizar_video_twelvelabs(asset_id):

client = crear_cliente()
instrucciones = """

Analiza el video completo y encuentra los TRES mejores
momentos para crear clips virales de TikTok,
Instagram Reels y Facebook Reels.

Busca:

* Momentos sorprendentes.
* Escenas emocionantes.
* Conversaciones interesantes.
* Frases impactantes.
* Historias que despierten curiosidad.
* Momentos con potencial para captar la atención.

REGLAS:

1. Selecciona momentos de diferentes partes del video.
2. Evita seleccionar momentos repetidos.
3. Cada momento debe durar entre 30 y 60 segundos.
4. Devuelve los tiempos en segundos.
5. No inventes marcas de tiempo.
6. Los tiempos deben corresponder al video real.
7. Prioriza los momentos que puedan captar la atención
    durante los primeros segundos.

Devuelve exclusivamente JSON válido con esta estructura:

{
“clips”: [
{
“inicio”: 120,
“fin”: 165,
“titulo”: “Título atractivo”,
“motivo”: “Explicación del momento”
},
{
“inicio”: 350,
“fin”: 395,
“titulo”: “Segundo momento”,
“motivo”: “Explicación del momento”
},
{
“inicio”: 700,
“fin”: 745,
“titulo”: “Tercer momento”,
“motivo”: “Explicación del momento”
}
]
}
“””

print(
    "Enviando video a Pegasus 1.6...",
    flush=True
)
tarea = client.analyze_async.tasks.create(
    model_name=MODELO,
    video=VideoContext_AssetId(
        asset_id=asset_id
    ),
    prompt=instrucciones,
    max_tokens=4096
)
task_id = tarea.task_id
print(
    f"Análisis iniciado. Task ID: {task_id}",
    flush=True
)
inicio_espera = time.time()
while True:
    resultado = client.analyze_async.tasks.retrieve(
        task_id
    )
    estado = str(resultado.status).lower()
    print(
        f"Estado del análisis Pegasus: {estado}",
        flush=True
    )
    if estado == "ready":
        if not resultado.result or not resultado.result.data:
            raise RuntimeError(
                "Pegasus terminó sin devolver resultados."
            )
        return resultado.result.data
    if estado in ("failed", "canceled"):
        error = getattr(
            resultado,
            "error",
            None
        )
        raise RuntimeError(
            f"Análisis fallido: {error}"
        )
    if time.time() - inicio_espera > TIEMPO_MAXIMO_ANALISIS:
        raise TimeoutError(
            "Pegasus superó el tiempo máximo de análisis."
        )
    time.sleep(10)

==================================================

VALIDAR RESULTADOS

==================================================

def validar_resultados(texto):

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
        "Pegasus devolvió una lista inválida."
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
        "Pegasus no encontró momentos válidos."
    )
return clips_validos[:CANTIDAD_CLIPS]

==================================================

CREAR CLIPS VERTICALES

==================================================

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

==================================================

PROCESAR VIDEO

==================================================

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
    "📥 RECIBÍ TU VIDEO.\n\n"
    "Preparando descarga..."
)
try:
    with tempfile.TemporaryDirectory() as folder:
        # DESCARGAR VIDEO
        await mensaje.edit_text(
            "📥 DESCARGANDO VIDEO DE YOUTUBE..."
        )
        video = await descargar_video(
            url,
            folder
        )
        # SUBIR A TWELVELABS
        await mensaje.edit_text(
            "☁️ SUBIENDO VIDEO A TWELVELABS...\n\n"
            "Esto puede tardar dependiendo del tamaño."
        )
        asset_id = await asyncio.to_thread(
            subir_video_twelvelabs,
            video
        )
        # ANALIZAR VIDEO
        await mensaje.edit_text(
            "🧠 PEGASUS 1.6 ESTÁ ANALIZANDO EL VIDEO...\n\n"
            "Buscando los tres mejores momentos."
        )
        texto = await asyncio.to_thread(
            analizar_video_twelvelabs,
            asset_id
        )
        momentos = validar_resultados(
            texto
        )
        # CREAR CLIPS
        await mensaje.edit_text(
            "🔥 PEGASUS ENCONTRÓ LOS MOMENTOS.\n\n"
            "Preparando tus clips verticales..."
        )
        await crear_clips(
            update,
            video,
            momentos,
            folder
        )
        await mensaje.edit_text(
            "✅ CLIPMASTER AI TERMINÓ EL TRABAJO."
        )
except TimeoutError as error:
    await update.message.reply_text(
        "⏱️ EL PROCESAMIENTO TARDÓ DEMASIADO.\n\n"
        f"{str(error)}"
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

==================================================

INICIAR BOT

==================================================

def main():

if not TOKEN:
    raise RuntimeError(
        "Falta configurar BOT_TOKEN en Railway."
    )
if not TWELVELABS_API_KEY:
    raise RuntimeError(
        "Falta configurar TWELVELABS_API_KEY en Railway."
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
    "CLIPMASTER AI CON TWELVELABS PEGASUS 1.6 INICIADO",
    flush=True
)
print(
    f"Proveedor BgUtils configurado: {BGUTIL_POT_URL}",
    flush=True
)
app.run_polling()

==================================================

EJECUCIÓN

==================================================

if name == “main”:
main()
