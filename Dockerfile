# MR Pilot image. Build: docker compose build   (setup.sh / setup.bat melakukannya otomatis)
FROM python:3.12-slim

# true = ikut pasang CLI Claude Code (untuk provider AI "claude_code")
ARG INSTALL_CLAUDE_CODE=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MRP_IN_DOCKER=1 \
    MRP_CONFIG=/data/config.yaml \
    DASHBOARD_HOST=0.0.0.0 \
    DISABLE_AUTOUPDATER=1 \
    PATH="/opt/claude/.local/bin:${PATH}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client tini tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Claude Code native installer -> /opt/claude (readable by any uid; $HOME saat runtime = /data/home)
RUN if [ "$INSTALL_CLAUDE_CODE" = "true" ]; then \
      HOME=/opt/claude bash -c "curl -fsSL https://claude.ai/install.sh | bash" \
      && chmod -R a+rX /opt/claude && claude --version; \
    fi

COPY mr_pilot ./mr_pilot
COPY standards ./standards
COPY deploy ./deploy
COPY config.example.yaml .env.example docker-compose.yml ./

VOLUME ["/data"]
EXPOSE 8787
# healthy = main loop wrote its heartbeat recently (works with or without the dashboard)
HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
  CMD ["python", "-m", "mr_pilot", "health"]

ENTRYPOINT ["tini", "--", "python", "-m", "mr_pilot"]
CMD ["run"]
