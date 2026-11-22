#!/bin/sh
# 桥密钥必须是独立随机串：缺失、占位串、短于 24 位都拒绝启动，而不是退回某个公开的默认值。
set -eu
key="${BOTGAME_CITY_BRIDGE_KEY:-}"
case "$key" in
  ""|please-change*)
    echo "[city-engine] CITY_BRIDGE_KEY is not set to a real secret; refusing to start" >&2
    exit 2
    ;;
esac
if [ "${#key}" -lt 24 ]; then
  echo "[city-engine] CITY_BRIDGE_KEY must be at least 24 characters; refusing to start" >&2
  exit 2
fi
cfg="/etc/city/config.json"
if [ -n "${BOTGAME_HOUSE_ROSTER_JSON:-}" ]; then
  node -e '
    const fs = require("fs");
    const base = JSON.parse(fs.readFileSync("/etc/city/config.json", "utf8"));
    const roster = JSON.parse(process.env.BOTGAME_HOUSE_ROSTER_JSON);
    if (!Array.isArray(roster) || roster.length === 0) throw new Error("empty roster");
    base.city.house = roster;
    fs.writeFileSync("/tmp/city-runtime-config.json", JSON.stringify(base, null, 2));
  '
  cfg="/tmp/city-runtime-config.json"
fi
# 监听地址显式写成 0.0.0.0：容器只挂内部网络，宿主不发布端口，所以只有同网络的小屋能连上。
exec node packages/engine/dist/cli.js serve --config "$cfg" --host 0.0.0.0 --port 7700 --no-state
