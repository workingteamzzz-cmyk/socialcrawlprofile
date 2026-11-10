# Post Link Collector as an API server (FastAPI).
#   docker build -t post-link-collector .
# Based on Microsoft's Playwright image: Chromium and every library it needs are already inside.
# When changing the image version, change the playwright pin in requirements-server.txt to the same version.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PIP_BREAK_SYSTEM_PACKAGES=1 \
    COLLECTOR_SERVER=1 CRAWLER_HOME=/data HOST=0.0.0.0 PORT=8770 TZ=Asia/Jakarta

WORKDIR /app
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt \
 && mkdir -p /data && chown pwuser:pwuser /data

COPY --chown=pwuser:pwuser . .
USER pwuser
VOLUME ["/data"]
EXPOSE 8770
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8770'), timeout=4)"
CMD ["python", "serve.py"]
