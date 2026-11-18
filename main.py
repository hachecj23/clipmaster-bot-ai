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
import math

from pathlib import Path
from urllib.parse import urlparse, urlencode

import cv2

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

# =========================================================
# CONFIGURACIÓN
# =========================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("TWELVELABS_API_KEY", "").strip()

TUNELIO_API_KEY = os.getenv("TUNELIO_API_KEY", "").strip()
TUNELIO_BASE_URL = "https://tunelio.dev"
TUNELIO_QUALITY = os.getenv("TUNELIO_QUALITY", "720p").strip()

BGUTIL_POT_URL = os.getenv(
    "BGUTIL_POT_URL",
    "http://bgutil-ytdlp-pot-provider.railway.internal:4416"
).strip()

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

FACE_SAMPLE_EVERY = 2
FACE_DETECT_SCALE = 0.5
FACE_SMOOTHING = 0.72

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("CLIPMASTER_AI")
cliente = None


# =========================================================
# TWELVELABS
# =========================================================

def obtener_cliente():
    global cliente

    if cliente is None:
        if not API_KEY:
            raise RuntimeError(
                "Falta TWELVELABS_API_KEY en Railway."
            )

        cliente = TwelveLabs(api_key=API_KEY)

    return cliente


# =========================================================
# EJECUTAR COMANDOS
# =========================================================

def ejecutar_comando(comando, timeout=900):

    proceso = subprocess.run(
        comando,
        capture_output=True,
        text=True,
        timeout=timeout
    )

    salida = (
        (proceso.stdout or "")
        + "\n"
        + (proceso.stderr or "")
    )

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


# =========================================================
# VALIDAR YOUTUBE
# =========================================================

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


# =========================================================
# VERIFICAR VIDEO
# =========================================================

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
        raise RuntimeError("No se pudo determinar la duración.")

    if duracion > DURACION_MAXIMA_VIDEO:
        raise RuntimeError("El video supera el límite de 60 minutos.")

    return video, duracion


# =========================================================
# DESCARGA CON TUNELIO
# =========================================================

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
                            int(error.headers.get("Retry-After", "10")),
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

    MAX_INTENTOS = 5
    TAMANO_BLOQUE = 1024 * 1024

    for intento in range(1, MAX_INTENTOS + 1):

        total = (
            os.path.getsize(destino)
            if os.path.exists(destino)
            else 0
        )

        inicio = time.time()

        try:
            headers = {
                "User-Agent": "ClipmasterBot/1.0"
            }

            if total > 0:
                headers["Range"] = f"bytes={total}-"

            peticion = urllib.request.Request(
                enlace,
                headers=headers
            )

            with urllib.request.urlopen(
                peticion,
                timeout=120
            ) as respuesta:

                estado = getattr(
                    respuesta,
                    "status",
                    respuesta.getcode()
                )

                # Comprobar si el servidor acepta reanudar.
                if total > 0 and estado == 206:

                    content_range = respuesta.headers.get(
                        "Content-Range",
                        ""
                    )

                    coincidencia = re.match(
                        r"bytes\s+(\d+)-",
                        content_range,
                        re.IGNORECASE
                    )

                    if coincidencia and int(
                        coincidencia.group(1)
                    ) == total:
                        modo = "ab"
                    else:
                        raise RuntimeError(
                            "Tunelio devolvió un rango de descarga incorrecto."
                        )

                else:
                    # Si no admite Range, comenzar desde cero.
                    total = 0
                    modo = "wb"

                with open(destino, modo) as archivo:

                    while True:

                        bloque = respuesta.read(
                            TAMANO_BLOQUE
                        )

                        if not bloque:
                            break

                        archivo.write(bloque)
                        total += len(bloque)

                        if total > MAX_VIDEO_BYTES:
                            raise RuntimeError(
                                "El video supera los 2 GB."
                            )

                        if time.time() - inicio > TIEMPO_DESCARGA:
                            raise RuntimeError(
                                "La descarga tardó demasiado."
                            )

            if total == 0:
                raise RuntimeError(
                    "Tunelio entregó un archivo vacío."
                )

            if (
                tamano_esperado
                and total < tamano_esperado * 0.9
            ):
                raise ConnectionError(
                    "La descarga quedó incompleta."
                )

            logger.info(
                "Descarga completada: %.2f MB",
                total / (1024 * 1024)
            )

            return

        except urllib.error.HTTPError as error:

            if error.code == 416 and total > 0:

                if (
                    not tamano_esperado
                    or total >= tamano_esperado * 0.9
                ):
                    logger.info(
                        "El archivo parece estar completo."
                    )
                    return

            if intento == MAX_INTENTOS:
                raise ErrorDescargaHttp(
                    error.code
                ) from None

            logger.warning(
                "Error HTTP %s. Intento %s/%s.",
                error.code,
                intento,
                MAX_INTENTOS
            )

        except (
            urllib.error.URLError,
            ConnectionResetError,
            TimeoutError,
            OSError,
            ConnectionError
        ) as error:

            if intento == MAX_INTENTOS:
                raise RuntimeError(
                    "Tunelio interrumpió la descarga después "
                    "de 5 intentos. Último error: "
                    + str(error)
                ) from None

            logger.warning(
                "Descarga interrumpida. Intento %s/%s: %s",
                intento,
                MAX_INTENTOS,
                error
            )

        time.sleep(3 * intento)


def descargar_video_tunelio(url, carpeta):

    info = tunelio_pedir("/info", {"url": url})

    duracion_info = info.get("duration_seconds")

    if (
        isinstance(duracion_info, (int, float))
        and duracion_info > DURACION_MAXIMA_VIDEO
    ):
        raise RuntimeError("El video supera el límite de 60 minutos.")

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
        raise RuntimeError("El video supera los 2 GB.")

    destino = os.path.join(carpeta, "video.mp4")
    separador = "&" if "?" in enlace else "?"

    logger.info(
        "Descargando desde Tunelio (%s).",
        TUNELIO_QUALITY
    )

    try:

        bajar_archivo(
            enlace + separador + "progressive=1",
            destino,
            tamano
        )

    except ErrorDescargaHttp as error:

        if error.codigo != 413:
            raise

        logger.info("Archivo grande, descarga normal.")
        bajar_archivo(enlace, destino, tamano)

    return verificar_video(destino)


# =========================================================
# DESCARGA YT-DLP DE RESPALDO
# =========================================================

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

    candidatos = [
        x for x in glob.glob(os.path.join(carpeta, "video.*"))
        if os.path.isfile(x)
        and not x.endswith((".part", ".ytdl", ".fix.mp4"))
    ]

    if not candidatos:
        raise RuntimeError("YouTube no entregó ningún archivo.")

    video = max(
        candidatos,
        key=os.path.getsize
    )

    if os.path.getsize(video) > MAX_VIDEO_BYTES:
        raise RuntimeError("El video supera los 2 GB.")

    return verificar_video(video)


def descargar_video(url, carpeta):

    if TUNELIO_API_KEY:
        return descargar_video_tunelio(url, carpeta)

    logger.warning(
        "Falta TUNELIO_API_KEY. Se usa yt-dlp."
    )

    return descargar_video_ytdlp(url, carpeta)


# =========================================================
# SUBIR VIDEO A TWELVELABS
# =========================================================

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
        raise RuntimeError("TwelveLabs no devolvió asset_id.")

    inicio = time.time()

    while time.time() - inicio < TIEMPO_ANALISIS:

        asset = api.assets.retrieve(asset_id)

        estado = str(
            getattr(asset, "status", "")
        ).lower()

        logger.info("Estado del video: %s", estado)

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


# =========================================================
# ANALIZAR VIDEO
# =========================================================

def analizar_video(asset_id, duracion):

    api = obtener_cliente()

    prompt = f"""
Analiza el video completo, no solo el principio.

Encuentra hasta 8 candidatos para clips virales.
Cada candidato debe durar entre 30 y 60 segundos.

Busca momentos:
- Impactantes.
- Emocionantes.
- Sorprendentes.
- Graciosos.
- Con frases interesantes.
- Que atrapen la atención.

Evita escenas repetidas y solapamientos.

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
No inventes escenas ni uses tiempos fuera del video.
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

        tarea = api.analyze_async.tasks.retrieve(task_id)

        estado = str(
            getattr(tarea, "status", "")
        ).lower()

        logger.info("Estado del análisis: %s", estado)

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


# =========================================================
# LEER JSON
# =========================================================

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
            return json.loads(texto[inicio:final + 1])

        raise RuntimeError(
            "La IA no devolvió JSON válido."
        )


# =========================================================
# VALIDAR CLIPS
# =========================================================

def validar_clips(resultado, duracion_video):

    datos = extraer_json(resultado)
    clips = datos.get("clips", [])

    if not isinstance(clips, list):
        raise RuntimeError(
            "La respuesta de la IA no contiene una lista de clips."
        )

    validos = []

    for clip in clips:

        try:

            inicio = float(clip["inicio"])
            fin = float(clip["fin"])

            if not math.isfinite(inicio) or not math.isfinite(fin):
                continue

            if inicio < 0 or fin > duracion_video:
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
                )[:180],
                "motivo": str(
                    clip.get("motivo", "")
                )[:300]
            })

        except (KeyError, TypeError, ValueError):
            continue

    validos.sort(
        key=lambda x: (
            -(x["fin"] - x["inicio"]),
            x["inicio"]
        )
    )

    seleccionados = []

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

    seleccionados.sort(
        key=lambda x: x["inicio"]
    )

    if len(seleccionados) < CANTIDAD_CLIPS:
        raise RuntimeError(
            f"La IA solo encontró {len(seleccionados)} momentos válidos. "
            "Prueba con otro video o vuelve a intentarlo."
        )

    return seleccionados


# =========================================================
# SEGUIMIENTO FACIAL
# =========================================================

def cargar_detector_rostros():

    ruta = (
        cv2.data.haarcascades
        + "haarcascade_frontalface_default.xml"
    )

    detector = cv2.CascadeClassifier(ruta)

    if detector.empty():
        raise RuntimeError(
            "No se pudo cargar el detector facial de OpenCV."
        )

    return detector


def detectar_centro_rostro(frame, detector):

    escala = FACE_DETECT_SCALE

    reducido = cv2.resize(
        frame,
        None,
        fx=escala,
        fy=escala,
        interpolation=cv2.INTER_AREA
    )

    gris = cv2.cvtColor(
        reducido,
        cv2.COLOR_BGR2GRAY
    )

    rostros = detector.detectMultiScale(
        gris,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(30, 30),
        flags=cv2.CASCADE_SCALE_IMAGE
    )

    if len(rostros) == 0:
        return None

    x, y, w, h = max(
        rostros,
        key=lambda r: r[2] * r[3]
    )

    cx = (x + w / 2) / escala
    cy = (y + h / 2) / escala

    return (cx, cy, w / escala, h / escala)


# =========================================================
# CREAR CLIP VERTICAL 9:16
# =========================================================

def crear_clip(video, clip, carpeta, numero):

    salida = os.path.join(
        carpeta,
        f"clip_{numero}.mp4"
    )

    temporal_sin_audio = os.path.join(
        carpeta,
        f"clip_{numero}_silent.mp4"
    )

    temporal_audio = os.path.join(
        carpeta,
        f"clip_{numero}_audio.m4a"
    )

    cap = cv2.VideoCapture(video)

    if not cap.isOpened():
        raise RuntimeError(
            "OpenCV no pudo abrir el video."
        )

    fps = cap.get(cv2.CAP_PROP_FPS)

    if not fps or fps < 1 or fps > 120:
        fps = 30.0

    ancho = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    alto = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if ancho <= 0 or alto <= 0:
        cap.release()
        raise RuntimeError(
            "No se pudieron leer las dimensiones del video."
        )

    inicio = float(clip["inicio"])
    fin = float(clip["fin"])
    duracion = fin - inicio

    crop_h = alto
    crop_w = int(round(crop_h * 9 / 16))

    if crop_w > ancho:
        crop_w = ancho
        crop_h = int(round(crop_w * 16 / 9))

    crop_w = max(2, crop_w - crop_w % 2)
    crop_h = max(2, crop_h - crop_h % 2)

    detector = cargar_detector_rostros()

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    writer = cv2.VideoWriter(
        temporal_sin_audio,
        fourcc,
        fps,
        (1080, 1920)
    )

    if not writer.isOpened():
        cap.release()
        raise RuntimeError(
            "No se pudo crear el video vertical temporal."
        )

    frame_inicio = int(inicio * fps)
    frame_fin = int(fin * fps)

    cap.set(
        cv2.CAP_PROP_POS_FRAMES,
        frame_inicio
    )

    centro_x = ancho / 2
    centro_y = alto / 2

    contador = 0
    total_frames = max(
        1,
        frame_fin - frame_inicio
    )

    try:

        while contador < total_frames:

            ok, frame = cap.read()

            if not ok:
                break

            if contador % FACE_SAMPLE_EVERY == 0:

                rostro = detectar_centro_rostro(
                    frame,
                    detector
                )

                if rostro:

                    rx, ry, rw, rh = rostro

                    objetivo_x = rx
                    objetivo_y = ry - rh * 0.12

                    centro_x = (
                        FACE_SMOOTHING * centro_x
                        + (1 - FACE_SMOOTHING) * objetivo_x
                    )

                    centro_y = (
                        FACE_SMOOTHING * centro_y
                        + (1 - FACE_SMOOTHING) * objetivo_y
                    )

            x1 = int(round(centro_x - crop_w / 2))
            y1 = int(round(centro_y - crop_h / 2))

            x1 = max(0, min(x1, ancho - crop_w))
            y1 = max(0, min(y1, alto - crop_h))

            recorte = frame[
                y1:y1 + crop_h,
                x1:x1 + crop_w
            ]

            if recorte.size == 0:
                contador += 1
                continue

            vertical = cv2.resize(
                recorte,
                (1080, 1920),
                interpolation=cv2.INTER_AREA
            )

            writer.write(vertical)
            contador += 1

    finally:

        cap.release()
        writer.release()

    if contador < int(total_frames * 0.90):
        raise RuntimeError(
            "No se pudieron procesar suficientes fotogramas del clip."
        )

    ejecutar_comando([
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(inicio),
        "-i", video,
        "-t", str(duracion),
        "-vn",
        "-c:a", "aac",
        "-b:a", "128k",
        temporal_audio
    ], 600)

    ejecutar_comando([
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", temporal_sin_audio,
        "-i", temporal_audio,
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "27",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        salida
    ], 1200)

    for temporal in (
        temporal_sin_audio,
        temporal_audio
    ):

        try:
            if os.path.exists(temporal):
                os.remove(temporal)
        except OSError:
            pass

    if not os.path.exists(salida):
        raise RuntimeError(
            "FFmpeg no creó el clip final."
        )

    if os.path.getsize(salida) > MAX_CLIP_BYTES:

        comprimido = os.path.join(
            carpeta,
            f"clip_{numero}_compressed.mp4"
        )

        ejecutar_comando([
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", salida,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "32",
            "-maxrate", "1400k",
            "-bufsize", "2800k",
            "-c:a", "aac",
            "-b:a", "96k",
            "-movflags", "+faststart",
            comprimido
        ], 900)

        os.replace(
            comprimido,
            salida
        )

    if os.path.getsize(salida) > MAX_CLIP_BYTES:
        raise RuntimeError(
            "El clip supera el límite de 45 MB."
        )

    return salida


# =========================================================
# TELEGRAM
# =========================================================

async def start(update, context):

    await update.message.reply_text(
        "🎬 ¡Bienvenido a CLIPMASTER AI!\n\n"
        "Envíame un enlace público de YouTube.\n"
        "Prepararé 3 clips verticales 9:16 "
        "de entre 30 y 60 segundos.\n"
        "El recorte intentará seguir el rostro "
        "más grande que detecte.\n\n"
        "Límite: videos de hasta 60 minutos."
    )


async def ayuda(update, context):

    await update.message.reply_text(
        "Envíame un enlace público de YouTube "
        "para comenzar."
    )


# =========================================================
# ENVIAR VIDEO
# =========================================================

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
                "Envío a Telegram falló (intento %s/%s).",
                intento,
                maximo
            )

            if intento == maximo:
                raise

            await asyncio.sleep(5)


# =========================================================
# PROCESAR VIDEO
# =========================================================

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

    if context.chat_data.get("procesando"):

        await update.message.reply_text(
            "⏳ Ya estoy procesando un video en este chat. "
            "Espera a que termine."
        )

        return

    context.chat_data["procesando"] = True

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
            "Preparando 3 clips verticales "
            "con seguimiento facial..."
        )

        enviados = 0

        for numero, clip in enumerate(
            clips,
            start=1
        ):

            await update.message.reply_text(
                f"🎞️ Preparando clip {numero} de {CANTIDAD_CLIPS}..."
            )

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
                f"{int(clip['fin'])}s\n"
                f"💡 {clip['motivo']}"
            )

            await enviar_video(
                update,
                ruta,
                caption
            )

            enviados += 1

        await update.message.reply_text(
            f"✅ ¡Terminado!\n"
            f"Te envié {enviados} clips."
        )

    except subprocess.TimeoutExpired:

        logger.exception("Tiempo agotado.")

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

        context.chat_data["procesando"] = False


# =========================================================
# ERRORES
# =========================================================

async def error_handler(update, context):

    logger.exception(
        "Error no controlado.",
        exc_info=context.error
    )


# =========================================================
# INICIAR BOT
# =========================================================

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
        logger.info(
            "Descarga mediante Tunelio activada."
        )

    else:
        logger.warning(
            "Sin TUNELIO_API_KEY: se usará yt-dlp."
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
