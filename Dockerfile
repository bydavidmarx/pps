FROM python:3.11-slim
# build: 2026-03-21c

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Verify correct version is deployed
RUN python3 -c "
import ast, sys
content = open('main.py').read()
ast.parse(content)
# Check version
for line in content.split('\n')[:25]:
    if 'version=' in line and 'FastAPI' in line:
        print('VERSION CHECK:', line.strip())
        if '2.3.2' not in line:
            print('ERROR: Wrong version!')
            sys.exit(1)
        break
print('Build OK')
"

EXPOSE 8000
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 3 --timeout-keep-alive 300
