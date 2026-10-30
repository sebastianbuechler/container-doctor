# Pinned to a digest, so a rebuild of the same commit gets the same base;
# Dependabot moves the tag and the digest together.
FROM python:3.15.0-slim-trixie@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so image rebuilds after a code change stay cheap.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY LICENSE ./

# Talks to Docker over TCP, so it needs neither root nor a socket mount.
# compose.example.yaml sets `user:` from DOCTOR_UID and DOCTOR_GID, so this is
# only the default for a plain `docker run`.
RUN groupadd --gid 1000 doctor \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin doctor \
    && mkdir -p /data && chown 1000:1000 /data
USER 1000:1000

VOLUME ["/data"]
EXPOSE 8080

# Traefik drops a container whose health reads `starting`, so until the first
# probe passes, the reverse proxy has no route. --start-interval probes every 2s
# during the start period instead of waiting a whole --interval for the first
# one; Docker Engine older than 25 ignores it and keeps the 60s wait.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.main"]

# Stamped by CI with the commit that built the image and, for a release, its
# version, so the dashboard can say which build is running. Last in the file:
# they change every build, and anything below them would be rebuilt rather
# than cached.
ARG DOCTOR_BUILD=dev
ARG DOCTOR_VERSION=
ENV DOCTOR_BUILD=$DOCTOR_BUILD \
    DOCTOR_VERSION=$DOCTOR_VERSION
