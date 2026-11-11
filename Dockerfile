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

# NiimBlueLib (MIT, github.com/MultiMote/niimbluelib): prints to NIIMBOT label printers (e.g. D110)
# over Web Bluetooth from the label page. Optional, like ZXing above.
ARG NIIMBLUELIB_VERSION=0.0.1-alpha.38
RUN python -c "import urllib.request; urllib.request.urlretrieve('https://unpkg.com/@mmote/niimbluelib@${NIIMBLUELIB_VERSION}/dist/umd/niimbluelib.min.js', '/app/static/vendor/niimbluelib.min.js')" \
    || (echo '/* niimbluelib unavailable */' > /app/static/vendor/niimbluelib.min.js && echo 'WARNING: could not download niimbluelib')

# Set by the GitHub Actions build: version (e.g. 1.2.0 or test-abc1234) and channel (stable/test).
ARG APP_VERSION=dev
ARG APP_CHANNEL=dev
ENV APP_VERSION=${APP_VERSION} \
    APP_CHANNEL=${APP_CHANNEL}
LABEL org.opencontainers.image.source="https://github.com/thepillock/relicbox" \
      org.opencontainers.image.version="${APP_VERSION}"

VOLUME /data
EXPOSE 3200
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','3200'))" || exit 1

CMD ["sh", "-c", "exec gunicorn --preload --workers 2 --threads 4 --bind 0.0.0.0:${PORT} --access-logfile - app:app"]
