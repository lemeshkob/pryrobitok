FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY gifs/*.gif gifs/

# SQLite DB will be stored here — mount a volume to persist it
VOLUME ["/app/data"]

CMD ["python", "bot.py"]