#!/usr/bin/env bash
# Запускать НА КОЛЛЕКТОРЕ (вызывается install-collector.sh). Ставит default-deny на приёмник:
# создаёт (пустой) список нод и применяет правила DOCKER-USER. Открывай ноды add-node-fw.sh.
set -euo pipefail
PORT="${1:-6000}"
LIST="${ALLOWED_NODES:-/opt/logcollector/allowed-nodes.txt}"
mkdir -p "$(dirname "$LIST")"; touch "$LIST"
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="$PORT" ALLOWED_NODES="$LIST" "$HERE/collector-firewall-apply.sh"
