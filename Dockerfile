# Zero-key demo by default. Add BASETEN_API_KEY at runtime for the 1-key demo:
#   docker build -t marketwingman .
#   docker run --rm -p 8000:8000 marketwingman
#   docker run --rm -p 8000:8000 -e BASETEN_API_KEY=... marketwingman
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY bot ./bot
COPY run_app.py run_demo.py run_session.py ./
RUN pip install --no-cache-dir .
VOLUME ["/data"]
EXPOSE 8000
CMD ["sh", "-c", "marketwingman-demo --host 0.0.0.0 --port ${PORT:-8000} --store-dir /data/demo"]
