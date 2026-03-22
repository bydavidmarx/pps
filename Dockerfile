FROM python:3.11-slim
# cache-bust: 2026-03-22

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port $PORT --workers 3 --timeout-keep-alive 300
