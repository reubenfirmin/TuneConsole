FROM docker.io/library/python:3.12-slim-bookworm AS base

FROM base AS builder
RUN python -m pip install --no-cache-dir uv==0.12.19
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
COPY README.md LICENSE ./
# Git metadata never enters the build context. Release CI passes the tag's version;
# an ordinary local build remains a development build without release update notices.
ARG VERSION=0.0.0.dev0
RUN SETUPTOOLS_SCM_PRETEND_VERSION="$VERSION" uv build --wheel --out-dir /wheels \
    && uv pip install --python /opt/venv/bin/python --no-deps /wheels/*.whl

FROM base AS runtime
LABEL org.opencontainers.image.title="TuneConsole" \
      org.opencontainers.image.description="Local YouTube Music library management and recommendations" \
      org.opencontainers.image.source="https://github.com/reubenfirmin/TuneConsole" \
      org.opencontainers.image.licenses="MIT"
RUN groupadd --gid 10001 tuneconsole \
    && useradd --uid 10001 --gid tuneconsole --create-home tuneconsole \
    && install -d -o tuneconsole -g tuneconsole /data
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    YT_PLAYLIST_HOME=/data \
    YT_PLAYLIST_CONTAINER=1
USER 10001:10001
WORKDIR /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/bridge/status', timeout=3).read()"]
ENTRYPOINT ["yt-playlist"]
CMD ["--host", "0.0.0.0", "--port", "8765"]
