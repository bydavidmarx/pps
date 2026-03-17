FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y \
    libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "-c", "import os,uvicorn; uvicorn.run('main:app', host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))"]
