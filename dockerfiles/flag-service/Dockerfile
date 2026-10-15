# Containerized flag-service (v3) — LEAN BASE image. The FlagServer + FlagStore
# submit authority; plants flags into prod over SSH. This base carries the
# framework + challenge CODE but only framework deps (fastapi/uvicorn/httpx) —
# NOT challenge deps. Per-game, `facility.build.build_facility_images` layers the
# deployed challenges' flag_facility_setup.sh on top to produce
# `cyberarena/flag-service` (the tag nodes run). Most challenges need no script
# (their flag_handler is stdlib / execs one-liners in prod over SSH).
#
# Build from the REPO ROOT (the source spans several top-level packages):
#   docker build -t cyberarena/flag-service-base -f dockerfiles/flag-service/Dockerfile .
#
# Pinned linux/amd64: matches the prod challenge images + the EC2 deploy target
# (and avoids QEMU-emulated SSH-handshake slowness when prod is amd64). On an
# arm64 dev box this builds under emulation; the image still runs native on EC2.
#
# Run (wiring via env; the facility token is a secret, never baked in):
#   docker run -d --name team1_flag_service -p 127.0.0.1:9001:8080 \
#     -e CYBERARENA_GM_BASE_URL=http://host.docker.internal:8000 \
#     -e CYBERARENA_FACILITY_TOKEN=... \
#     cyberarena/flag-service
FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# openssh-client: the flag-service plants into prod over SSH (SshExecPort).
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first for layer caching.
COPY dockerfiles/flag-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# v3: the service runs challenge flag_handlers (build_challenge) to plant over
# SSH, so it needs the challenges namespace, the warehouse packages it points at,
# and the repo modules they import. NOT docker.sock and no docker SDK — exec is
# SSH only.
COPY flag/ /app/flag/
COPY facility/ /app/facility/
COPY challenges/ /app/challenges/
COPY models/ /app/models/
COPY gamemgr/__init__.py /app/gamemgr/__init__.py
COPY gamemgr/events.py /app/gamemgr/events.py

EXPOSE 8080

CMD ["uvicorn", "flag.service_main:app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
