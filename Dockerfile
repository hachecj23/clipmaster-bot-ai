FROM python:3.13-slim

RUN apt-get update && apt-get install -y ffmpeg curl unzip ca-certificates && rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/usr/local

RUN curl -fsSL https://deno.land/install.sh | sh

ENV PATH=”/usr/local/bin:$PATH”

WORKDIR /app

COPY requirements.txt .

RUN pip install –no-cache-dir -r requirements.txt

RUN yt-dlp –version && deno –version

COPY . .

CMD [“python”, “main.py”]
