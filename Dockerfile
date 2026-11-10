FROM python:3.12-slim

WORKDIR /app

# install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy application files
COPY . .

# create downloads directory
RUN mkdir -p /app/downloads

ENV PORT=10000
EXPOSE 10000

CMD ["python", "bot_discord.py"]
