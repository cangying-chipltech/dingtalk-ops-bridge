ARG BASE_IMAGE=debian:bookworm-slim

FROM ${BASE_IMAGE} AS dws-downloader

ARG DWS_INSTALL_URL=https://gitee.com/DingTalk-Real-AI/dingtalk-workspace-cli/raw/main/scripts/install.sh

RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates bash unzip \
  && rm -rf /var/lib/apt/lists/* \
  && curl -fsSL --retry 5 --retry-delay 2 --connect-timeout 10 --max-time 300 \
    "$DWS_INSTALL_URL" -o /tmp/dws-install.sh \
  && bash /tmp/dws-install.sh \
  && test -x /root/.local/bin/dws \
  && install -m 0755 /root/.local/bin/dws /usr/local/bin/dws \
  && rm -rf /root/.local /tmp/dws-install.sh

FROM ${BASE_IMAGE}

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    iputils-ping \
    openssh-client \
    procps \
    python3 \
    sshpass \
    tini \
  && rm -rf /var/lib/apt/lists/*

COPY --from=dws-downloader /usr/local/bin/dws /usr/local/bin/dws

WORKDIR /app
COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY config/ /app/config/

RUN chmod +x /usr/local/bin/dws /app/scripts/*.sh \
  && install -d -m 700 /var/lib/dws /root/.local/share/dws-cli /app/data \
  && install -d -m 755 /app/plugins

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/app/config \
    DATA_DIR=/app/data \
    ACTION_PLUGIN_DIR=/app/plugins \
    MONITOR_STATE_FILE=/app/data/monitor-state.json \
    AGENT_CMD=/app/scripts/agent-router.sh \
    DWS_CONFIG_DIR=/var/lib/dws \
    DWS_DISABLE_KEYCHAIN=1

VOLUME ["/var/lib/dws"]
VOLUME ["/root/.local/share/dws-cli"]
VOLUME ["/app/data"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/entrypoint.sh"]
