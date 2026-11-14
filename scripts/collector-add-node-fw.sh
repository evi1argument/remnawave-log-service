#!/usr/bin/env bash
# Запускать НА КОЛЛЕКТОРЕ. Разрешает приём 6000/tcp с IP ноды (в DOCKER-USER — Docker обходит ufw!).
# Добавляет IP в список и переприменяет правила (reboot-safe через systemd collector-firewall.service).
# Использование: ./collector-add-node-fw.sh <NODE_IP> [PORT=6000]
set -euo pipefail
IP="${1:?Usage: $0 <NODE_IP> [PORT]}"
[[ "$IP" =~ ^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$ ]] &&
  (( BASH_REMATCH[1] <= 255 && BASH_REMATCH[2] <= 255 && BASH_REMATCH[3] <= 255 && BASH_REMATCH[4] <= 255 )) ||
  { echo "Expected the node's IPv4 address" >&2; exit 1; }
PORT="${2:-6000}"
LIST="${ALLOWED_NODES:-/opt/logcollector/allowed-nodes.txt}"
mkdir -p "$(dirname "$LIST")"; touch "$LIST"
grep -qxF -- "$IP" "$LIST" || echo "$IP" >> "$LIST"
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="$PORT" ALLOWED_NODES="$LIST" "$HERE/collector-firewall-apply.sh"
echo "node $IP allowed on $PORT"
