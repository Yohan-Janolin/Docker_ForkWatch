FROM alpine:3

RUN apk add --no-cache python3 git tzdata ca-certificates \
 && git config --system --add safe.directory '*'

COPY app/ /app/

ENV TZ=Europe/Paris \
    PYTHONUNBUFFERED=1 \
    FORKWATCH_DATA_DIR=/data \
    FORKWATCH_REPOS_FILE=/config/repos.conf \
    FORKWATCH_PORT=8080

VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=5m --timeout=5s CMD wget -q -O /dev/null http://127.0.0.1:8080/api/health || exit 1

CMD ["python3", "/app/forkwatch.py"]
