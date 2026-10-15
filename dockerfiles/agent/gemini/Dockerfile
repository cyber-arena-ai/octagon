# cyberarena/agent-gemini — Gemini CLI on top of cyberarena/agent-base.
# Build: docker build -t cyberarena/agent-gemini dockerfiles/agent/gemini
#
# Built FROM SOURCE (not `npm install -g @google/gemini-cli`) so we can register
# gemini flash models newer than the CLI's GA flash generation. The published CLI
# does not recognize such tags and silently downgrades them to the GA flash model
# on the wire (see models_patch.py); our api-router upstream serves them correctly,
# so we teach the CLI to emit them verbatim. PINNED to v0.58.0 — keep in sync with
# GEMINI_VERSION in agents/gemini.py, and CYBERARENA_EXTRA_FLASH_MODELS here in sync
# with GEMINI_REGISTERED_EXTRA_MODELS there.
ARG BASE_IMAGE=cyberarena/agent-base

# --- builder: clone the pinned tag, register our models, bundle ---------------
FROM node:20 AS gemini-build
WORKDIR /build
RUN git clone --depth 1 --branch v0.58.0 \
    https://github.com/google-gemini/gemini-cli.git .
COPY models_patch.py /build/models_patch.py
# ignore-scripts: skip the husky/prepare auto-bundle so we bundle AFTER patching.
RUN HUSKY=0 npm ci --ignore-scripts \
    && python3 models_patch.py packages/core/src/config/models.ts \
    && HUSKY=0 npm run bundle \
    && test -f bundle/gemini.js \
    && mkdir -p /build/out && npm pack --pack-destination /build/out

# --- final: install the built package into the agent image --------------------
FROM ${BASE_IMAGE}
COPY --from=gemini-build /build/out/ /tmp/gemini-pkg/
RUN npm install -g /tmp/gemini-pkg/*.tgz \
    && rm -rf /tmp/gemini-pkg \
    && npm cache clean --force && rm -rf /root/.npm

RUN gemini --version
