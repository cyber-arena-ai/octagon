# Containerized health-poller (v3) — LEAN BASE image. Schedules probes + runs
# each functionality_test against prod over SSH. This base carries the framework
# + challenge CODE but only framework deps (fastapi/uvicorn/httpx) — NOT challenge
# deps. Per-game, `facility.build.build_facility_images` layers the deployed
# challenges' health_facility_setup.sh on top to produce `cyberarena/health-poller`
# (the tag the node runs). Most challenges need no script.
#
# Build from the REPO ROOT (source spans several top-level packages):
#   docker build -t cyberarena/health-poller-base -f dockerfiles/health-poller/Dockerfile .
#
# Run (wiring via env; the facility token is a secret, never baked in):
#   docker run -d --name health_poller -p 127.0.0.1:9201:8080 \
#     -e CYBERARENA_GM_BASE_URL=http://host.docker.internal:8000 \
#     -e CYBERARENA_FACILITY_TOKEN=... \
#     cyberarena/health-poller
FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# openssh-client: the poller probes prod over SSH (SshExecPort).
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first for layer caching.
COPY dockerfiles/health-poller/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# v3: the poller runs challenge functionality_tests (build_challenge) to probe
# over SSH, so it needs the challenges namespace, the warehouse packages it
# points at, and the repo modules they import. NOT docker.sock and no docker SDK
# — exec is SSH only.
COPY health/ /app/health/
COPY facility/ /app/facility/
COPY challenges/ /app/challenges/
COPY models/ /app/models/
COPY gamemgr/__init__.py /app/gamemgr/__init__.py
COPY gamemgr/events.py /app/gamemgr/events.py

EXPOSE 8080

CMD ["uvicorn", "health.service_main:app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
