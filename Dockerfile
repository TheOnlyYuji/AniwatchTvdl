FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    wget \
    libicu-dev \
    && rm -rf /var/lib/apt/lists/*

RUN wget https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.2.1-beta/N_m3u8DL-RE_Beta_linux-x64_20240828.tar.gz && \
    tar -xzf N_m3u8DL-RE_Beta_linux-x64_20240828.tar.gz && \
    mv N_m3u8DL-RE_Beta_linux-x64/N_m3u8DL-RE /usr/local/bin/ && \
    chmod +x /usr/local/bin/N_m3u8DL-RE && \
    rm -rf N_m3u8DL-RE_Beta_linux-x64*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

CMD ["bash", "run.sh"]
