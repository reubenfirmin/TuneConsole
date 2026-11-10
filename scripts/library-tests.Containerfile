FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
COPY requirements.txt /tmp/library-test-requirements.txt
RUN python3 -m pip install --disable-pip-version-check --no-cache-dir --require-hashes -r /tmp/library-test-requirements.txt
ARG LOCK_SHA256
LABEL org.tuneconsole.library-tests.lock-sha256=$LOCK_SHA256
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /worktree
