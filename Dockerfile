FROM python:3.12-slim

# The agent has hands: it runs a shell, writes its own tools, and installs
# software into this container. Give it node/npm, git, curl, and build basics
# so `npm install`, `pip install`, and `apt-get install` all work out of the
# box. (apt metadata is kept so the agent can install more at runtime.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git curl ca-certificates nodejs npm build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY cairnival ./cairnival
RUN pip install --no-cache-dir .

# The agent's whole world lives here; mount a volume to persist it.
ENV CAIRNIVAL_HOME=/data
VOLUME /data

EXPOSE 8600 8700

# Override with `hub` (or `once` for cron-style single wakes).
ENTRYPOINT ["cairnival"]
CMD ["agent"]
