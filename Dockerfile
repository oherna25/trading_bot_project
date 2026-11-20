# Full app by default; keys come from the environment (see .env.example):
#   docker build -t marketwingman .
#   docker run --rm -p 8000:8000 -v marketwingman-data:/data --env-file .env marketwingman
# One scheduled session runs from the same image (see docker-compose.yml):
#   docker run --rm -v marketwingman-data:/data marketwingman \
#     marketwingman-session --store-dir /data/trading-bot
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY bot ./bot
COPY run_app.py run_session.py ./
COPY docs/icons ./docs/icons
RUN pip install --no-cache-dir ".[claude]"

RUN useradd --create-home app && mkdir -p /data && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=3)"
CMD ["sh", "-c", "exec marketwingman --host 0.0.0.0 --port ${PORT:-8000} --store-dir /data/trading-bot"]
