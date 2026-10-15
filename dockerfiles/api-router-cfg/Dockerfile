# cyberarena/api-router-cfg — base apirouter image + a baked config.
#   docker build -t cyberarena/api-router-cfg dockerfiles/api-router-cfg/
# Config goes to /app (NOT /data: the base declares VOLUME /data, so files
# copied there are masked by an anonymous volume at runtime). APIROUTER_CONFIG
# is repointed accordingly.
FROM cyberarena/api-router

COPY config.yaml /app/config.yaml
ENV APIROUTER_CONFIG=/app/config.yaml
