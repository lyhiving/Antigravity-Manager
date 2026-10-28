FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    KYA_PROXY_HOST=0.0.0.0 \
    KYA_PROXY_PORT=8317 \
    ABV_DATA_DIR=/root/.antigravity_tools

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# cursor-agent ships its own runtime; resolve_cursor_agent() looks in
# /root/.local/bin and /usr/local/bin, so land it in both.
RUN curl -fsS https://cursor.com/install | bash \
    && ln -sf /root/.local/bin/cursor-agent /usr/local/bin/cursor-agent \
    && test -x /usr/local/bin/cursor-agent

COPY app/ /app/

EXPOSE 8317
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8317/health || exit 1

CMD ["python", "main.py"]
