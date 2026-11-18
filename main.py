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
import urllib.request
import urllib.error
import csv

from pathlib import Path
from urllib.parse import urlparse, urlencode

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TimedOut, NetworkError
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

# Tunelio: servicio externo para obtener el video de YouTube.
# Si TUNELIO_API_KEY no existe, el bot usa yt-dlp como antes.
TUNELIO_API_KEY = os.getenv("TUNELIO_API_KEY", "").strip()
TUNELIO_BASE_URL = "https://tunelio.dev"
# Plan Trial: hasta 720p. Plan Pro: hasta 1080p.
TUNELIO_QUALITY = os.getenv("TUNELIO_QUALITY", "720p").strip()

# Groq (Whisper): transcripcion para subtitulos. Opcional.
# Sin GROQ_API_KEY los clips salen sin subtitulos.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "whisper-large-v3-turbo").strip()
# Opcional: idioma del audio (es, en...). Vacio = detectar solo.
GROQ_LANGUAGE = os.getenv("GROQ_LANGUAGE", "").strip()
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Kick: proxy opcional (por si Kick bloquea la IP de Railway).
# Formato: http://usuario:clave@servidor:puerto
KICK_PROXY = os.getenv("KICK_PROXY", "").strip()
# Captura en vivo: cada bloque se analiza al cerrarse.
LIVE_SEGMENT_SECONDS = int(os.getenv("LIVE_SEGMENT_SECONDS", "300"))
LIVE_MAX_CLIPS_PER_SEGMENT = int(os.getenv("LIVE_MAX_CLIPS_PER_SEGMENT", "2"))

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

        raise RuntimeError(
            detalle[-4000:]
            + f"\n[codigo de salida: {proceso.returncode}]"
        )

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
# KICK: ENLACES, RANGOS Y DESCARGA
# ==========================================

class ErrorUsuario(Exception):

    """Mensaje claro para el usuario (no es un fallo del bot)."""


def es_canal_kick(url):
    """Acepta URLs públicas de canal, no rutas de VOD o clips."""
    try:
        d = urlparse(url)
        host = d.netloc.lower().split(":")[0]
        ruta = d.path.strip("/").split("/")
        return (d.scheme in ["http", "https"] and host in ["kick.com", "www.kick.com"]
                and len(ruta) == 1 and bool(ruta[0]) and ruta[0].lower() not in {"videos", "video", "clips"})
    except Exception:
        return False


def es_enlace_kick(url):

    try:

        datos = urlparse(url)

        host = datos.netloc.lower().split(":")[0]

        if datos.scheme not in ["http", "https"]:
            return False

        if host not in ["kick.com", "www.kick.com"]:
            return False

        ruta = datos.path.lower()
        consulta = datos.query.lower()

        return (
            "/video/" in ruta
            or "/videos/" in ruta
            or "/clips/" in ruta
            or "clip=" in consulta
        )

    except Exception:

        return False


def es_clip_kick(url):

    datos = urlparse(url)

    return (
        "/clips/" in datos.path.lower()
        or "clip=" in datos.query.lower()
    )


def detectar_plataforma(url):

    if validar_youtube(url):
        return "youtube"

    if es_enlace_kick(url):
        return "kick"

    return None


def a_segundos(texto):

    total = 0

    for parte in texto.split(":"):

        total = total * 60 + int(parte)

    return total


def leer_rango(mensaje, url):

    """Busca un rango como 1:20:00-2:10:00 junto al enlace."""

    resto = mensaje.replace(url, " ")

    coincidencia = re.search(
        r"(\d{1,2}(?::\d{2}){1,2})\s*(?:-|a)\s*(\d{1,2}(?::\d{2}){1,2})",
        resto
    )

    if not coincidencia:
        return None

    return (
        a_segundos(coincidencia.group(1)),
        a_segundos(coincidencia.group(2))
    )


def formato_hora(segundos):

    segundos = int(segundos)

    return (
        f"{segundos // 3600}:"
        f"{(segundos % 3600) // 60:02d}:"
        f"{segundos % 60:02d}"
    )


def comando_kick(extra, impersonar):

    comando = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--force-ipv4",
        "--socket-timeout", "30"
    ]

    if impersonar:

        comando.extend(["--impersonate", "chrome"])

    if KICK_PROXY:

        comando.extend(["--proxy", KICK_PROXY])

    comando.extend(extra)

    return comando


def ejecutar_kick(extra, timeout):

    """Ejecuta yt-dlp imitando un navegador. Si eso no esta
    disponible, reintenta sin imitar."""

    try:

        return ejecutar_comando(
            comando_kick(extra, True),
            timeout
        )

    except RuntimeError as error:

        if "mpersonat" in str(error):

            logger.warning(
                "Sin soporte de impersonate, se reintenta sin el."
            )

            return ejecutar_comando(
                comando_kick(extra, False),
                timeout
            )

        raise


def descargar_video_kick(url, carpeta, rango=None):

    esclip = es_clip_kick(url)

    extra = []

    if rango and not esclip:

        inicio, fin = rango

        if fin <= inicio:

            raise ErrorUsuario(
                "El rango no es valido: el final debe ser "
                "mayor que el inicio."
            )

        if fin - inicio > DURACION_MAXIMA_VIDEO:

            raise ErrorUsuario(
                "El rango supera los 60 minutos. "
                "Elige un tramo de hasta 60 minutos."
            )

        if fin - inicio < DURACION_MINIMA:

            raise ErrorUsuario(
                "El rango es demasiado corto. "
                "Debe durar al menos 30 segundos."
            )

        extra.extend([
            "--download-sections",
            f"*{inicio}-{fin}"
        ])

    elif not esclip:

        try:

            salida = ejecutar_kick(
                ["--skip-download", "--print", "duration", url],
                180
            )

            duracion_vod = float(
                salida.strip().splitlines()[-1]
            )

        except RuntimeError as error:

            manejar_error_kick(error)

        except (ValueError, IndexError):

            duracion_vod = None

        if (
            duracion_vod
            and duracion_vod > DURACION_MAXIMA_VIDEO
        ):

            raise ErrorUsuario(
                "Este VOD dura "
                + formato_hora(duracion_vod)
                + ". Para sacar clips, envia el enlace con un "
                "rango de hasta 60 minutos, por ejemplo:\n"
                + url + " 1:20:00-2:10:00"
            )

    extra.extend([
        "-f", "best[height<=720]/best",
        "--remux-video", "mp4",
        "-o", os.path.join(carpeta, "video.%(ext)s"),
        url
    ])

    logger.info("Descargando desde Kick con yt-dlp.")

    try:

        ejecutar_kick(extra, TIEMPO_DESCARGA)

    except RuntimeError as error:

        manejar_error_kick(error)

    candidatos = [
        x for x in glob.glob(os.path.join(carpeta, "video.*"))
        if os.path.isfile(x)
        and not x.endswith((".part", ".ytdl"))
    ]

    if not candidatos:

        raise RuntimeError(
            "Kick no entrego ningun archivo."
        )

    video = max(candidatos, key=os.path.getsize)

    if os.path.getsize(video) > MAX_VIDEO_BYTES:

        raise RuntimeError(
            "El video supera los 2 GB."
        )

    return verificar_video(video)


def manejar_error_kick(error):

    texto = str(error)

    if "403" in texto or "Forbidden" in texto:

        raise RuntimeError(
            "Kick bloqueo la descarga desde el servidor (403). "
            "Kick usa Cloudflare y puede bloquear IPs de "
            "servidores como Railway."
        )

    raise error


# ==========================================
# VERIFICAR VIDEO DESCARGADO
# ==========================================

def leer_duracion(video):

    salida = ejecutar_comando([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video
    ], 60)

    return float(salida.strip())


def verificar_video(video):

    try:

        duracion = leer_duracion(video)

    except (ValueError, RuntimeError):

        # Algunos MP4 en streaming no traen la duración.
        # Se reempaqueta sin recodificar y se vuelve a leer.
        logger.info("Reempaquetando video para leer la duración.")

        arreglado = video + ".fix.mp4"

        ejecutar_comando([
            "ffmpeg",
            "-y",
            "-i", video,
            "-c", "copy",
            "-movflags", "+faststart",
            arreglado
        ], 1800)

        os.replace(arreglado, video)

        duracion = leer_duracion(video)

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
# DESCARGA CON TUNELIO
# ==========================================

class ErrorDescargaHttp(RuntimeError):

    def __init__(self, codigo):

        super().__init__(
            f"Falló la descarga del archivo (HTTP {codigo})."
        )

        self.codigo = codigo


def tunelio_pedir(ruta, parametros, reintentos=3):

    direccion = (
        TUNELIO_BASE_URL
        + ruta
        + "?"
        + urlencode(parametros)
    )

    cabeceras = {
        "Authorization": "Bearer " + TUNELIO_API_KEY,
        "Accept": "application/json",
        "User-Agent": "ClipmasterBot/1.0"
    }

    ultimo_error = "Error desconocido"

    for intento in range(1, reintentos + 1):

        espera = 5 * intento

        peticion = urllib.request.Request(
            direccion,
            headers=cabeceras
        )

        try:

            with urllib.request.urlopen(
                peticion,
                timeout=60
            ) as respuesta:

                cuerpo = respuesta.read().decode(
                    "utf-8",
                    errors="replace"
                )

            return json.loads(cuerpo)

        except urllib.error.HTTPError as error:

            codigo = error.code

            try:

                texto = error.read().decode(
                    "utf-8",
                    errors="replace"
                )

                info = json.loads(texto)

                detalle = str(
                    info.get("message")
                    or info.get("error")
                    or ""
                )

            except Exception:

                detalle = ""

            if codigo == 401:

                raise RuntimeError(
                    "Tunelio rechazó la clave (401). "
                    "Revisa TUNELIO_API_KEY en Railway."
                )

            if codigo == 402:

                raise RuntimeError(
                    "Tunelio se quedó sin créditos (402)."
                )

            if codigo == 404:

                raise RuntimeError(
                    "El video no existe, es privado "
                    "o fue eliminado."
                )

            if codigo in (429, 500, 503):

                ultimo_error = (
                    f"Tunelio respondió {codigo}. {detalle}"
                )

                if codigo == 429:

                    try:

                        espera = min(
                            int(error.headers.get(
                                "Retry-After",
                                "10"
                            )),
                            60
                        )

                    except ValueError:

                        espera = 10

            else:

                raise RuntimeError(
                    f"Tunelio error {codigo}: {detalle}"
                )

        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            OSError
        ) as error:

            ultimo_error = (
                "No se pudo conectar con Tunelio: "
                + str(error)
            )

        logger.info(
            "Tunelio %s intento %s/%s falló: %s",
            ruta,
            intento,
            reintentos,
            ultimo_error
        )

        if intento < reintentos:

            time.sleep(espera)

    raise RuntimeError(ultimo_error)


def bajar_archivo(enlace, destino, tamano_esperado=None):

    peticion = urllib.request.Request(
        enlace,
        headers={"User-Agent": "ClipmasterBot/1.0"}
    )

    inicio = time.time()
    total = 0

    try:

        with urllib.request.urlopen(
            peticion,
            timeout=60
        ) as respuesta, open(destino, "wb") as archivo:

            while True:

                bloque = respuesta.read(1024 * 1024)

                if not bloque:
                    break

                total += len(bloque)

                if total > MAX_VIDEO_BYTES:

                    raise RuntimeError(
                        "El video supera los 2 GB."
                    )

                if time.time() - inicio > TIEMPO_DESCARGA:

                    raise RuntimeError(
                        "La descarga tardó demasiado."
                    )

                archivo.write(bloque)

    except urllib.error.HTTPError as error:

        raise ErrorDescargaHttp(error.code) from None

    if total == 0:

        raise RuntimeError(
            "Tunelio entregó un archivo vacío."
        )

    if tamano_esperado and total < tamano_esperado * 0.9:

        raise RuntimeError(
            "La descarga quedó incompleta."
        )


def descargar_video_tunelio(url, carpeta):

    info = tunelio_pedir("/info", {"url": url})

    duracion_info = info.get("duration_seconds")

    if (
        isinstance(duracion_info, (int, float))
        and duracion_info > DURACION_MAXIMA_VIDEO
    ):

        raise RuntimeError(
            "El video supera el límite de 60 minutos."
        )

    creado = tunelio_pedir(
        "/create",
        {"url": url, "quality": TUNELIO_QUALITY}
    )

    enlace = creado.get("url")

    if creado.get("status") != "ok" or not enlace:

        raise RuntimeError(
            "Tunelio no entregó enlace de descarga: "
            + str(creado.get("status"))
        )

    tamano = creado.get("file_size")

    if not isinstance(tamano, int):
        tamano = None

    if tamano and tamano > MAX_VIDEO_BYTES:

        raise RuntimeError(
            "El video supera los 2 GB."
        )

    destino = os.path.join(carpeta, "video.mp4")

    separador = "&" if "?" in enlace else "?"

    logger.info(
        "Descargando desde Tunelio (%s).",
        TUNELIO_QUALITY
    )

    try:

        # progressive=1 entrega un MP4 normal, fácil de recortar.
        bajar_archivo(
            enlace + separador + "progressive=1",
            destino,
            tamano
        )

    except ErrorDescargaHttp as error:

        if error.codigo != 413:
            raise

        # Archivo muy grande para ese formato: descarga normal.
        logger.info("Archivo grande, descarga normal.")

        bajar_archivo(enlace, destino, tamano)

    return verificar_video(destino)


# ==========================================
# DESCARGA DE YOUTUBE CON YT-DLP (RESPALDO)
# ==========================================

def descargar_video_ytdlp(url, carpeta):

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

    logger.info("Iniciando descarga de YouTube con yt-dlp.")

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

    return verificar_video(video)


# ==========================================
# DESCARGA (ELIGE TUNELIO O YT-DLP)
# ==========================================

def descargar_video(url, carpeta, rango=None):

    if detectar_plataforma(url) == "kick":

        return descargar_video_kick(url, carpeta, rango)

    if TUNELIO_API_KEY:

        return descargar_video_tunelio(url, carpeta)

    logger.warning(
        "Falta TUNELIO_API_KEY. Se usa yt-dlp."
    )

    return descargar_video_ytdlp(url, carpeta)


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
# SUBTITULOS (GROQ WHISPER + ASS)
# ==========================================

ENCABEZADO_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,68,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,70,70,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def transcribir_groq(ruta_audio, reintentos=3):

    with open(ruta_audio, "rb") as archivo:

        contenido = archivo.read()

    if len(contenido) > 24 * 1024 * 1024:

        raise RuntimeError(
            "El audio supera el límite de Groq (25 MB)."
        )

    limite = "ClipmasterBoundary" + str(int(time.time() * 1000))

    campos = [
        ("model", GROQ_MODEL),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
        ("temperature", "0")
    ]

    if GROQ_LANGUAGE:

        campos.append(("language", GROQ_LANGUAGE))

    partes = []

    for nombre, valor in campos:

        partes.append((
            "--" + limite + "\r\n"
            + 'Content-Disposition: form-data; name="'
            + nombre + '"\r\n\r\n'
            + valor + "\r\n"
        ).encode("utf-8"))

    partes.append((
        "--" + limite + "\r\n"
        + 'Content-Disposition: form-data; name="file"; '
        + 'filename="audio.flac"\r\n'
        + "Content-Type: audio/flac\r\n\r\n"
    ).encode("utf-8"))

    partes.append(contenido)

    partes.append(
        ("\r\n--" + limite + "--\r\n").encode("utf-8")
    )

    cuerpo = b"".join(partes)

    cabeceras = {
        "Authorization": "Bearer " + GROQ_API_KEY,
        "Content-Type": "multipart/form-data; boundary=" + limite,
        "Accept": "application/json",
        "User-Agent": "ClipmasterBot/1.0"
    }

    ultimo_error = "Error desconocido"

    for intento in range(1, reintentos + 1):

        peticion = urllib.request.Request(
            GROQ_URL,
            data=cuerpo,
            headers=cabeceras,
            method="POST"
        )

        espera = 5 * intento

        try:

            with urllib.request.urlopen(
                peticion,
                timeout=120
            ) as respuesta:

                datos = json.loads(
                    respuesta.read().decode("utf-8", errors="replace")
                )

            palabras = []

            for item in datos.get("words") or []:

                texto = str(item.get("word", "")).strip()

                if not texto:
                    continue

                inicio = float(item.get("start", 0))
                final = float(item.get("end", inicio))

                palabras.append({
                    "word": texto,
                    "start": inicio,
                    "end": final
                })

            return palabras

        except urllib.error.HTTPError as error:

            codigo = error.code

            if codigo == 401:

                raise RuntimeError(
                    "Groq rechazó la clave (401). "
                    "Revisa GROQ_API_KEY en Railway."
                )

            if codigo in (429, 500, 502, 503):

                ultimo_error = f"Groq respondió {codigo}."

                if codigo == 429:

                    try:

                        espera = min(
                            int(error.headers.get(
                                "Retry-After",
                                "10"
                            )),
                            60
                        )

                    except ValueError:

                        espera = 10

            else:

                raise RuntimeError(
                    f"Groq error {codigo}."
                )

        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            OSError
        ) as error:

            ultimo_error = (
                "No se pudo conectar con Groq: " + str(error)
            )

        if intento < reintentos:

            time.sleep(espera)

    raise RuntimeError(ultimo_error)


def tiempo_ass(segundos):

    centesimas = int(round(max(segundos, 0) * 100))

    horas = centesimas // 360000
    centesimas -= horas * 360000

    minutos = centesimas // 6000
    centesimas -= minutos * 6000

    seg = centesimas // 100
    centesimas -= seg * 100

    return f"{horas}:{minutos:02d}:{seg:02d}.{centesimas:02d}"


def limpiar_palabra(texto):

    texto = (
        str(texto)
        .replace("\\", "")
        .replace("{", "")
        .replace("}", "")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )

    return texto.upper()


def agrupar_palabras(palabras):

    grupos = []
    actual = []
    letras = 0

    for palabra in palabras:

        texto = limpiar_palabra(palabra["word"])

        if not texto:
            continue

        item = {
            "t": texto,
            "s": palabra["start"],
            "e": max(palabra["end"], palabra["start"])
        }

        if actual:

            hueco = item["s"] - actual[-1]["e"]

            if (
                len(actual) >= 4
                or letras + len(texto) + 1 > 17
                or hueco > 0.8
            ):

                grupos.append(actual)
                actual = []
                letras = 0

        actual.append(item)
        letras += len(texto) + 1

        if texto[-1] in ".?!":

            grupos.append(actual)
            actual = []
            letras = 0

    if actual:

        grupos.append(actual)

    return grupos


def escribir_ass(palabras, ruta):

    grupos = agrupar_palabras(palabras)

    lineas = [ENCABEZADO_ASS.rstrip("\n")]

    for indice, grupo in enumerate(grupos):

        if indice + 1 < len(grupos):
            tope = grupos[indice + 1][0]["s"]
        else:
            tope = None

        for i, palabra in enumerate(grupo):

            inicio = palabra["s"]

            if i + 1 < len(grupo):
                final = grupo[i + 1]["s"]
            else:
                final = palabra["e"]

            if final < inicio + 0.08:
                final = inicio + 0.08

            if tope is not None and final > tope:
                final = max(tope, inicio + 0.02)

            piezas = []

            for j, otra in enumerate(grupo):

                if j == i:

                    piezas.append(
                        "{\\c&H00FFFF&\\fscx85\\fscy85"
                        "\\t(0,90,\\fscx118\\fscy118)}"
                        + otra["t"]
                        + "{\\r}"
                    )

                else:

                    piezas.append(otra["t"])

            lineas.append(
                "Dialogue: 0,"
                + tiempo_ass(inicio) + ","
                + tiempo_ass(final)
                + ",Default,,0,0,0,,"
                + " ".join(piezas)
            )

    with open(ruta, "w", encoding="utf-8") as archivo:

        archivo.write("\n".join(lineas) + "\n")


def preparar_subtitulos(video, clip, carpeta, numero):

    if not GROQ_API_KEY:

        return None

    try:

        duracion = clip["fin"] - clip["inicio"]

        audio = os.path.join(
            carpeta,
            f"audio_{numero}.flac"
        )

        ejecutar_comando([
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-nostats",
            "-y",
            "-ss", str(clip["inicio"]),
            "-t", str(duracion),
            "-i", video,
            "-map", "0:a:0",
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "flac",
            audio
        ], 300)

        palabras = transcribir_groq(audio)

        if not palabras:

            logger.info(
                "Clip %s: sin voz detectada, sin subtitulos.",
                numero
            )

            return None

        ruta_ass = os.path.join(
            carpeta,
            f"subs_{numero}.ass"
        )

        escribir_ass(palabras, ruta_ass)

        logger.info(
            "Clip %s: subtitulos listos (%s palabras).",
            numero,
            len(palabras)
        )

        return ruta_ass

    except Exception:

        logger.exception(
            "Clip %s: no se pudieron crear subtitulos.",
            numero
        )

        return None


# ==========================================
# CREAR CLIP VERTICAL
# ==========================================

def detectar_contenido(video, clip):

    """Busca barras negras fijas dentro del video original.
    Devuelve (ancho, alto, x, y) del contenido real o None."""

    try:

        dimensiones = ejecutar_comando([
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            video
        ], 60).strip()

        ancho, alto = [
            int(x) for x in dimensiones.split("x")[:2]
        ]

        duracion = clip["fin"] - clip["inicio"]

        cajas = []

        for fraccion in (0.2, 0.5, 0.8):

            instante = clip["inicio"] + duracion * fraccion

            proceso = subprocess.run([
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-ss", str(instante),
                "-t", "2",
                "-i", video,
                "-vf", "cropdetect=limit=24:round=2:reset=0",
                "-an",
                "-f", "null",
                "-"
            ], capture_output=True, text=True, timeout=180)

            encontrados = re.findall(
                r"crop=(\d+):(\d+):(\d+):(\d+)",
                proceso.stderr or ""
            )

            if encontrados:

                cajas.append(
                    tuple(int(x) for x in encontrados[-1])
                )

        if not cajas:

            logger.info(
                "Video %sx%s: sin datos de barras.",
                ancho,
                alto
            )

            return None

        x1 = min(c[2] for c in cajas)
        y1 = min(c[3] for c in cajas)
        x2 = max(c[2] + c[0] for c in cajas)
        y2 = max(c[3] + c[1] for c in cajas)

        w = (x2 - x1) // 2 * 2
        h = (y2 - y1) // 2 * 2
        x1 = x1 // 2 * 2
        y1 = y1 // 2 * 2

        logger.info(
            "Video %sx%s, contenido detectado %sx%s en (%s,%s).",
            ancho,
            alto,
            w,
            h,
            x1,
            y1
        )

        sin_barras = (
            w >= ancho * 0.97
            and h >= alto * 0.97
        )

        sospechoso = (
            w < ancho * 0.3
            or h < alto * 0.3
        )

        if sin_barras or sospechoso:

            return None

        return w, h, x1, y1

    except Exception:

        logger.exception("No se pudo detectar barras negras.")

        return None


def codificar_clip(video, clip, duracion, filtro, salida):

    comando = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-y",

        "-ss",
        str(clip["inicio"]),

        "-i",
        video,

        "-t",
        str(duracion),

        "-vf",
        filtro,

        "-threads", "2",
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


def crear_clip(video, clip, carpeta, numero):

    salida = os.path.join(
        carpeta,
        f"clip_{numero}.mp4"
    )

    duracion = (
        clip["fin"] - clip["inicio"]
    )

    # Recorte centrado 9:16 valido para videos anchos o altos.
    centrado = (
        "crop='min(iw,trunc(ih*9/16/2)*2)':"
        "'min(ih,trunc(iw*16/9/2)*2)',"
        "scale=1080:1920"
    )

    caja = detectar_contenido(video, clip)

    if caja:

        base = (
            f"crop={caja[0]}:{caja[1]}:{caja[2]}:{caja[3]},"
            + centrado
        )

    else:

        base = centrado

    filtros = []

    ruta_ass = preparar_subtitulos(
        video,
        clip,
        carpeta,
        numero
    )

    if ruta_ass:

        filtros.append(base + ",ass=" + ruta_ass)

    filtros.append(base)

    if caja:

        filtros.append(centrado)

    for posicion, filtro in enumerate(filtros):

        try:

            return codificar_clip(
                video,
                clip,
                duracion,
                filtro,
                salida
            )

        except RuntimeError:

            if posicion == len(filtros) - 1:
                raise

            logger.exception(
                "Clip %s: falló con subtitulos, se crea sin ellos.",
                numero
            )

    raise RuntimeError("No se pudo crear el clip.")


# ==========================================
# KICK EN VIVO: CAPTURA POR BLOQUES
# ==========================================

# Una tarea activa por chat. Los procesos se guardan para poder detenerlos.
DIRECTOS_ACTIVOS = {}


def procesar_bloque_directo(video, carpeta, numero):
    """Analiza un bloque cerrado del directo y devuelve clips exportados."""
    _, duracion = verificar_video(video)
    if duracion < DURACION_MINIMA:
        return []
    asset_id = subir_video(video)
    resultado = analizar_video(asset_id, duracion)
    clips = validar_clips(resultado, duracion)
    clips = clips[:LIVE_MAX_CLIPS_PER_SEGMENT]
    salidas = []
    for i, clip in enumerate(clips, 1):
        ruta = crear_clip(video, clip, carpeta, numero * 10 + i)
        salidas.append((ruta, clip))
    return salidas


async def capturar_directo(update, context, url):
    chat_id = update.effective_chat.id
    if chat_id in DIRECTOS_ACTIVOS:
        await update.message.reply_text("⚠️ Ya hay una captura activa en este chat. Usa /detener para finalizarla.")
        return

    carpeta = tempfile.mkdtemp(prefix="clipmaster_live_")
    lista = os.path.join(carpeta, "bloques.csv")
    patron = os.path.join(carpeta, "bloque_%05d.mp4")
    await update.message.reply_text(
        "🔴 Iniciando captura del directo de Kick.\n"
        f"Analizaré bloques de {LIVE_SEGMENT_SECONDS // 60} minutos y enviaré los mejores clips mientras la transmisión continúa.\n"
        "Usa /detener para terminar."
    )

    stream_cmd = ["streamlink", "--stdout", url, "best"]
    if KICK_PROXY:
        stream_cmd[1:1] = ["--http-proxy", KICK_PROXY]

    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", "pipe:0", "-map", "0", "-c", "copy",
        "-f", "segment", "-segment_time", str(LIVE_SEGMENT_SECONDS),
        "-reset_timestamps", "1", "-segment_format", "mp4",
        "-segment_list", lista, "-segment_list_type", "csv",
        patron
    ]

    stream = ffmpeg = None
    stop_event = asyncio.Event()
    DIRECTOS_ACTIVOS[chat_id] = {"stop": stop_event, "stream": None, "ffmpeg": None, "task": asyncio.current_task()}
    procesados = set()
    numero = 0
    try:
        stream = await asyncio.create_subprocess_exec(*stream_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        ffmpeg = await asyncio.create_subprocess_exec(*ffmpeg_cmd, stdin=stream.stdout, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        DIRECTOS_ACTIVOS[chat_id]["stream"] = stream
        DIRECTOS_ACTIVOS[chat_id]["ffmpeg"] = ffmpeg
        if stream.stdout:
            stream.stdout.close()

        while not stop_event.is_set():
            await asyncio.sleep(8)
            if stream.returncode is not None and stream.returncode != 0:
                raise RuntimeError("Streamlink terminó inesperadamente. Comprueba que el canal esté en vivo y sea público.")
            if ffmpeg.returncode is not None and ffmpeg.returncode != 0:
                raise RuntimeError("FFmpeg detuvo la captura del directo.")
            if not os.path.exists(lista):
                continue
            filas = []
            try:
                with open(lista, newline="", encoding="utf-8") as f:
                    filas = list(csv.reader(f))
            except Exception:
                continue
            for fila in filas:
                if not fila:
                    continue
                nombre = fila[0]
                ruta = nombre if os.path.isabs(nombre) else os.path.join(carpeta, nombre)
                if ruta in procesados or not os.path.isfile(ruta):
                    continue
                procesados.add(ruta)
                numero += 1
                await update.message.reply_text(f"🧠 Analizando el bloque {numero} del directo...")
                try:
                    resultados = await asyncio.to_thread(procesar_bloque_directo, ruta, carpeta, numero)
                    for j, (clip_path, clip) in enumerate(resultados, 1):
                        await enviar_video(update, clip_path, f"🔴 KICK EN VIVO · CLIP {j}\n{clip['titulo']}\n{clip.get('motivo','')}")
                    if not resultados:
                        await update.message.reply_text("No encontré un momento de 30–60 segundos en este bloque. Seguiré analizando el directo.")
                except Exception as e:
                    logger.exception("Error procesando bloque del directo")
                    await update.message.reply_text(f"⚠️ No pude procesar el bloque {numero}: {str(e)[-700:]}\nLa captura continuará.")
                # El bloque fuente ya no es necesario después de exportar.
                try:
                    os.remove(ruta)
                except OSError:
                    pass

        await update.message.reply_text("⏹️ Solicitud de detención recibida. Cerrando la captura...")
    except Exception as e:
        logger.exception("Fallo en captura Kick en vivo")
        await update.message.reply_text("❌ No pude mantener la captura del directo.\n" + str(e)[-1200:])
    finally:
        # Cerrar primero Streamlink para que FFmpeg finalice el último segmento.
        for proc in (stream, ffmpeg):
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        for proc in (stream, ffmpeg):
            if proc:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=12)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        DIRECTOS_ACTIVOS.pop(chat_id, None)
        shutil.rmtree(carpeta, ignore_errors=True)
        await update.message.reply_text("✅ Captura finalizada. Puedes enviarme otro canal cuando quieras.")


async def comando_directo(update, context):
    texto = " ".join(context.args).strip()
    if not texto:
        await update.message.reply_text("Uso: /directo https://kick.com/nombre_del_canal")
        return
    url = texto.split()[0]
    if not es_canal_kick(url):
        await update.message.reply_text("Envíame el enlace público de un canal, por ejemplo: /directo https://kick.com/nombre_del_canal")
        return
    await capturar_directo(update, context, url)


async def comando_detener(update, context):
    estado = DIRECTOS_ACTIVOS.get(update.effective_chat.id)
    if not estado:
        await update.message.reply_text("No hay ninguna captura activa en este chat.")
        return
    estado["stop"].set()
    await update.message.reply_text("⏹️ Estoy deteniendo la captura...")


# ==========================================
# TELEGRAM: INICIO
# ==========================================

async def start(update, context):

    await update.message.reply_text(
        "🎬 ¡Bienvenido a CLIPMASTER AI!\n\n"
        "Envíame un enlace público de YouTube, "
        "un VOD o clip de Kick.\n"
        "Para capturar un directo: /directo https://kick.com/canal\n"
        "Para detenerlo: /detener.\n\n"
        "Prepararé hasta 3 clips verticales 9:16 "
        "de entre 30 y 60 segundos.\n\n"
        "Límite: videos de hasta 60 minutos.\n"
        "Para un VOD largo de Kick, agrega un tramo: "
        "enlace 1:20:00-2:10:00"
    )


async def ayuda(update, context):

    await update.message.reply_text(
        "Envíame un enlace de YouTube, un VOD/clip de Kick "
        "o usa /directo https://kick.com/canal. "
        "Usa /detener para parar una captura en vivo."
    )


# ==========================================
# ENVIAR VIDEO CON REINTENTOS
# ==========================================

async def enviar_video(update, ruta, caption):

    maximo = 3

    for intento in range(1, maximo + 1):

        try:

            with open(ruta, "rb") as archivo:

                await update.message.reply_video(
                    video=archivo,
                    caption=caption[:1024],
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                    pool_timeout=60
                )

            return

        except (TimedOut, NetworkError):

            logger.warning(
                "Envio a Telegram fallo (intento %s/%s).",
                intento,
                maximo
            )

            if intento == maximo:
                raise

            await asyncio.sleep(5)


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

    if es_canal_kick(url):
        await capturar_directo(update, context, url)
        return

    if detectar_plataforma(url) is None:

        await update.message.reply_text(
            "⚠️ Envíame un enlace válido de YouTube, "
            "o de un VOD o clip de Kick."
        )

        return

    rango = leer_rango(mensaje, url)

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
            carpeta,
            rango
        )

        if duracion <= 90:

            await update.message.reply_text(
                "✅ Video descargado.\n"
                "Es corto: lo convierto a vertical completo."
            )

            clips = [{
                "inicio": 0.0,
                "fin": min(duracion, DURACION_MAXIMA),
                "titulo": "Clip completo",
                "motivo": ""
            }]

        else:

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

            await enviar_video(update, ruta, caption)

            enviados += 1

        await update.message.reply_text(
            f"✅ ¡Terminado!\n"
            f"Te envié {enviados} clips."
        )

    except ErrorUsuario as error:

        await update.message.reply_text(
            "⚠️ " + str(error)
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

    if TUNELIO_API_KEY:

        logger.info("Descarga mediante Tunelio activada.")

    else:

        logger.warning(
            "Sin TUNELIO_API_KEY: se usará yt-dlp."
        )

    if GROQ_API_KEY:

        logger.info("Subtitulos con Groq activados.")

    else:

        logger.warning(
            "Sin GROQ_API_KEY: los clips saldran sin subtitulos."
        )

    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("ayuda", ayuda)
    )

    app.add_handler(CommandHandler("directo", comando_directo))
    app.add_handler(CommandHandler("detener", comando_detener))

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
