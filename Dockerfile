FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=3200

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# ZXing barcode reader, used for camera scanning on browsers without the native
# BarcodeDetector API (iPhone Safari, Firefox). Fetched at build time so the running
# container needs no internet. If the download fails the build still succeeds and
# Android/Chrome scanning + manual entry keep working.
ARG ZXING_VERSION=0.21.3
RUN mkdir -p /app/static/vendor && \
    python -c "import urllib.request,sys; urllib.request.urlretrieve('https://unpkg.com/@zxing/library@${ZXING_VERSION}/umd/index.min.js', '/app/static/vendor/zxing.min.js')" \
    || (echo '/* zxing unavailable */' > /app/static/vendor/zxing.min.js && echo 'WARNING: could not download ZXing')

VOLUME /data
EXPOSE 3200
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','3200'))" || exit 1

CMD ["sh", "-c", "exec gunicorn --preload --workers 2 --threads 4 --bind 0.0.0.0:${PORT} --access-logfile - app:app"]
