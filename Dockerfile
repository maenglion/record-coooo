FROM python:3.12-slim

WORKDIR /app

# install deno (JS runtime required by yt-dlp for YouTube challenge solving)
RUN apt-get update -qq && apt-get install -y -qq curl ca-certificates unzip && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy application files
COPY . .

# create runtime directories
RUN mkdir -p /app/downloads /app/logs

ENV PORT=10000
EXPOSE 10000

CMD ["python", "bot_discord.py"]
