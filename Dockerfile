FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY check_weather.py .

VOLUME /data
CMD ["python", "-u", "check_weather.py"]
