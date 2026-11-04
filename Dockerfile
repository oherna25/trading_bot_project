# Zero-key demo by default. Add BASETEN_API_KEY at runtime for the 1-key demo:
#   docker build -t marketwingman .
#   docker run --rm -p 8000:8000 marketwingman
#   docker run --rm -p 8000:8000 -e BASETEN_API_KEY=... marketwingman
# Full app and one-shot session run from the same image (see docker-compose.yml):
#   docker run --rm -p 8000:8000 -v marketwingman-data:/data marketwingman \
#     marketwingman --store-dir /data/trading-bot --host 0.0.0.0
#   docker run --rm -v marketwingman-data:/data marketwingman \
#     marketwingman-session --store-dir /data/trading-bot
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY bot ./bot
COPY run_app.py run_demo.py run_session.py ./
COPY docs/icons ./docs/icons
RUN pip install --no-cache-dir .

RUN useradd --create-home app && mkdir -p /data && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=3)"
CMD ["sh", "-c", "exec marketwingman-demo --host 0.0.0.0 --port ${PORT:-8000} --store-dir /data/demo"]
