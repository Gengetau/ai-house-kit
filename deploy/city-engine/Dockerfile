# 晴空都城市引擎（bot-game-mcp），固定到 main@971484c13004e14ee6e91e9a9a75c0bd5ec65020。
# 克隆与构建都在「构建」阶段完成（需要外网）；运行时只挂内部网络，不再联网。
FROM node:22-slim

ARG BOT_GAME_MCP_REF=971484c13004e14ee6e91e9a9a75c0bd5ec65020
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# 完整克隆后再检出固定的完整提交号。
RUN git clone https://github.com/Gengetau/bot-game-mcp.git /opt/bot-game-mcp \
 && git -C /opt/bot-game-mcp checkout "${BOT_GAME_MCP_REF}" \
 && cd /opt/bot-game-mcp \
 && npm ci \
 && npm run build \
 && rm -rf .git

COPY config.json /etc/city/config.json
COPY entrypoint.sh /usr/local/bin/city-entrypoint.sh
RUN chmod 0755 /usr/local/bin/city-entrypoint.sh \
 && mkdir -p /data \
 && chown -R node:node /data

WORKDIR /opt/bot-game-mcp
USER node
ENTRYPOINT ["/usr/local/bin/city-entrypoint.sh"]
