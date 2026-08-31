FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    CONFIG_DIR=/config \
    PORT=8577

# Single stage on purpose. Every dependency here is a pure-Python or manylinux
# wheel, so there is nothing to compile and a builder stage would save almost
# nothing -- while adding a hardcoded copy path (/lib/python3.12/site-packages)
# that silently breaks if the base image's Python minor version ever moves.
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

# setpriv (util-linux) lets the entrypoint drop from root to PUID/PGID after
# fixing ownership on the mounted config volume. Checked at build time so a base
# image without it fails here with a clear message rather than at first boot.
# The account itself is cosmetic -- setpriv takes numeric ids and PUID/PGID
# decide the real ones at runtime -- so a pre-existing 1000 is not an error.
RUN set -eux; \
    if ! command -v setpriv >/dev/null; then \
        echo "ERROR: setpriv is missing from the base image; the entrypoint needs it to drop privileges." >&2; \
        exit 1; \
    fi; \
    groupadd -g 1000 unwatcharr || true; \
    useradd -u 1000 -g 1000 -M -d /config -s /usr/sbin/nologin unwatcharr || true; \
    mkdir -p /config; \
    chown 1000:1000 /config

WORKDIR /app
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Fail the build rather than ship an image that cannot import itself.
# Importing app.main opens the database and generates the session secret, so
# point it at a throwaway directory -- otherwise every container from this image
# would ship with the same pre-baked secret and a stray database.
RUN CONFIG_DIR=/tmp/importcheck python -c "import app.main; print('import check OK')" \
    && rm -rf /tmp/importcheck

VOLUME ["/config"]
EXPOSE 8577

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8577')+'/healthz',timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app"]
