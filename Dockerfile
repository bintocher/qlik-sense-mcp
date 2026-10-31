# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS build
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY qlik_sense_mcp_server ./qlik_sense_mcp_server
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir .

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QLIK_MCP_MODE=incoming-jwt \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    HOME=/app
RUN groupadd --gid 10001 mcp \
    && useradd --uid 10001 --gid mcp --home-dir /app --create-home --shell /usr/sbin/nologin mcp
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "qlik_sense_mcp_server.container_health"]
ENTRYPOINT ["qlik-sense-mcp-server"]
