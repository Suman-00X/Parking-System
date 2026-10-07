# One image for every radreport process; the command picks which (see ops/docker/start.sh):
#   radreport-start web | jobs | worker | relay | migrate | <any command>
# The public pages read FEATURES.md, API.md, docs/ and tests/ from the source tree, so the
# image runs from a copy of the repository rather than from the installed package alone.

FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY pyproject.toml ./
COPY radreport ./radreport
# Install the dependencies, then drop the package itself: the code runs from /app.
RUN pip install ".[redis,observability]" && pip uninstall -y radreport

FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 radreport
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY --chown=radreport:radreport . .
COPY --chmod=755 ops/docker/start.sh /usr/local/bin/radreport-start
USER radreport
EXPOSE 8000
CMD ["radreport-start", "web"]
