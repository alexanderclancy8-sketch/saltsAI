FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATA_DIR=/home/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jarvis ./jarvis
COPY knowledge ./knowledge
COPY checks ./checks
COPY *.yaml ./

RUN useradd --create-home jarvis && mkdir -p /home/data && chown -R jarvis /home/data /app
USER jarvis
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "jarvis"]
