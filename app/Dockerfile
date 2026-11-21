# MTG Print Studio – Web-App + CUPS (treiberlos / IPP Everywhere)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    LANG=C.UTF-8

# CUPS + Filter (PDF -> PWG-Raster/URF für den Drucker) + ipptool zur Diagnose
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      cups cups-client cups-filters cups-ipp-utils ghostscript poppler-utils tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mtgprint ./mtgprint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/info', timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
