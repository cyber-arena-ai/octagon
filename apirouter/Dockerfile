# Self-contained image for the apirouter component. Build from the repo root:
#   docker build -t cyberarena/api-router apirouter/
#
# Everything lives in ONE mounted dir, /data: put your config.yaml there; the
# router reads /data/config.yaml and writes /data/usage.jsonl. Only the real
# upstream key comes via -e. Per-person spend: GET /usage (no log parsing).
#   mkdir -p apirouter-data && cp apirouter/config.example.yaml apirouter-data/config.yaml
#   docker run -d --name api-router -p 8080:8080 -p 3128:3128 \
#     -e UPSTREAM_API_KEY=sk-... \
#     -v "$PWD/apirouter-data:/data" \
#     cyberarena/api-router
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APIROUTER_CONFIG=/data/config.yaml

WORKDIR /app
RUN mkdir -p /data

# Deps first for layer caching.
COPY requirements.txt /app/apirouter/requirements.txt
RUN pip install --no-cache-dir -r /app/apirouter/requirements.txt

# Package body. (Build context is apirouter/, so sources sit at the root.)
COPY . /app/apirouter/

EXPOSE 8080 3128
VOLUME ["/data"]

CMD ["python", "-m", "apirouter"]
