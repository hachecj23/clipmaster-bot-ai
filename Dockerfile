FROM python:3.13-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh

ENV DENO_INSTALL=/root/.deno
ENV PATH="/root/.deno/bin:$PATH"

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
