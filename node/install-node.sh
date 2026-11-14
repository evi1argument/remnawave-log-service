#!/usr/bin/env bash
# Установка АГЕНТА на VPN-ноду Remnawave: tail xray access/error -> коллектор:6000 (TLS).
# Запускать root'ом на ноде.  Использование: ./install-node.sh <NODE_NAME> [COLLECTOR_IP]
#   NODE_NAME     короткая метка ноды в логах (de/fi/pl/se/...)
#   COLLECTOR_IP  IP коллектора (приёмник Vector на 6000). Можно аргументом ИЛИ через env COLLECTOR_IP —
#                 install-collector печатает готовую команду с уже подставленным IP.
set -euo pipefail
NODE_NAME="${1:?Usage: $0 <NODE_NAME> [COLLECTOR_IP]  (или COLLECTOR_IP через env)}"
COLLECTOR_IP="${2:-${COLLECTOR_IP:-}}"
[ -n "$COLLECTOR_IP" ] || { echo "COLLECTOR_IP не задан (аргумент 2 или env COLLECTOR_IP) — возьми готовую команду из вывода install-collector"; exit 1; }
[[ "$NODE_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || { echo "Invalid NODE_NAME" >&2; exit 1; }
[[ "$COLLECTOR_IP" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo "Use an IPv4 address or hostname for COLLECTOR_IP" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/logship
: "${COLLECTOR_CERT:?Set COLLECTOR_CERT to the collector certificate copied over trusted SSH}"
[[ -s "$COLLECTOR_CERT" ]] || { echo "Collector certificate missing or empty" >&2; exit 1; }
command -v openssl >/dev/null || { echo "Install openssl first" >&2; exit 1; }
openssl x509 -in "$COLLECTOR_CERT" -noout -checkend 0
if grep -q -- 'PRIVATE KEY' "$COLLECTOR_CERT"; then
  echo "Provide only the certificate, never a private key" >&2
  exit 1
fi

echo "== prereqs =="
docker compose version >/dev/null || { echo "Install Docker Engine and the Compose plugin first" >&2; exit 1; }

echo "== ПРОВЕРКА: нода должна писать xray-логи в файл =="
if [ ! -f /var/log/remnanode/access.log ]; then
  cat <<'WARN'
  !! /var/log/remnanode/access.log НЕ найден. Нужно, чтобы:
     (a) в docker-compose ноды (/opt/remnanode) был mount  /var/log/remnanode:/var/log/remnanode
     (b) в конфиге профиля этой ноды в панели блок log содержал пути
         access:/var/log/remnanode/access.log , error:/var/log/remnanode/error.log
     (см. историю: у PL этих путей не было в панели -> логи не писались на диск).
     Плюс поставь logrotate copytruncate на этот файл (держит диск ноды в узде).
WARN
fi

echo "== выкладка $DST =="
mkdir -p "$DST"
install -m 644 -- "$COLLECTOR_CERT" "$DST/collector.crt"
cp "$SRC/docker-compose.yml" "$DST/docker-compose.yml"
sed -e "s/__NODE_NAME__/${NODE_NAME}/g" -e "s/__COLLECTOR_IP__/${COLLECTOR_IP}/g" \
    "$SRC/vector/vector.agent.yaml" > "$DST/vector.yaml"

echo "== logrotate xray-логов =="
if [ -d /etc/logrotate.d ] && [ ! -f /etc/logrotate.d/remnanode ]; then
  cp "$SRC/logrotate-remnanode" /etc/logrotate.d/remnanode && echo "  установлен /etc/logrotate.d/remnanode (copytruncate)"
else
  echo "  пропущено (уже есть /etc/logrotate.d/remnanode или нет logrotate)"
fi

echo "== up =="
( cd "$DST" && docker compose config --quiet && \
  docker compose run --rm --no-deps --volume /vector-data vector validate --skip-healthchecks /etc/vector/vector.yaml && \
  docker compose up -d )
sleep 3
docker logs logship-vector --since 20s 2>&1 | tail -5 || true

# свой публичный IP — чтобы выдать готовую firewall-команду (её выполнять на коллекторе)
SELF_IP="${NODE_SOURCE_IP:-$(hostname -I | awk '{print $1}')}"
[[ "$SELF_IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || SELF_IP="<NODE_SOURCE_IP>"

cat <<EOF

== ГОТОВО (нода '$NODE_NAME' -> $COLLECTOR_IP:6000) ==
  Открой приём с этой ноды — выполни ОДНУ команду НА КОЛЛЕКТОРЕ ($COLLECTOR_IP):
     /opt/logcollector/scripts/collector-add-node-fw.sh $SELF_IP
  (без неё 6000 закрыт default-deny). Проверка: в Grafana/Loki появится метка node="$NODE_NAME".
EOF
