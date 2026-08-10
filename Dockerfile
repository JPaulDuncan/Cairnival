FROM python:3.12-slim

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
