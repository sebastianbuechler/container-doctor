FROM python:3.14.8-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so image rebuilds after a code change stay cheap.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Stamped by CI with the commit that built the image, so the dashboard can say
# which build is running. Last in the file: it changes every build, and anything
# below it would be rebuilt rather than cached.
ARG DOCTOR_BUILD=dev
ENV DOCTOR_BUILD=$DOCTOR_BUILD

# Talks to Docker over TCP, so it needs neither root nor a socket mount. The
# Synology's first user and its users group by default; docker-compose.yml
# sets `user:` from DOCTOR_UID and DOCTOR_GID for any other host.
RUN useradd --uid 1026 --gid 100 --no-create-home --shell /usr/sbin/nologin doctor \
    && mkdir -p /data && chown 1026:100 /data
USER 1026:100

VOLUME ["/data"]
EXPOSE 8080

# Traefik drops a container whose health reads `starting`, so until the first
# probe passes, the reverse proxy has no route. --start-interval probes every 2s
# during the start period instead of waiting a whole --interval for the first
# one; Docker Engine older than 25 ignores it and keeps the 60s wait.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
