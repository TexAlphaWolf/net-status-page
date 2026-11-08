FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ping is the only system program the checks use
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && useradd --system --no-create-home --uid 10001 upstatus \
    && install -d -o upstatus /data

# The configuration (upstatus.toml), the history and the page (public/) live in /data, a volume. The program makes
# outgoing connections only, so it runs as an unprivileged user with a read-only root filesystem and no capabilities.
# (ICMP ping needs --cap-add NET_RAW; the other checks need nothing.)
USER upstatus
WORKDIR /data
VOLUME ["/data"]
ENTRYPOINT ["upstatus"]
CMD ["--help"]
