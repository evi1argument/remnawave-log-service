#!/usr/bin/env bash
# Установка АГЕНТА на VPN-ноду Remnawave: tail xray access/error -> коллектор:6000 (TLS).
# Запускать root'ом на ноде.  Использование: ./install-node.sh <NODE_NAME> [COLLECTOR_IP]
#   NODE_NAME     короткая метка ноды в логах (de/fi/pl/se/...)
#   COLLECTOR_IP  IP коллектора (приёмник Vector на 6000). Можно аргументом ИЛИ через env COLLECTOR_IP —
#                 install-collector печатает готовую команду с уже подставленным IP.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/installer-language.sh"
NODE_NAME="${1:?Usage: $0 <NODE_NAME> [COLLECTOR_IP]  (или COLLECTOR_IP через env)}"
COLLECTOR_IP="${2:-${COLLECTOR_IP:-}}"
[ -n "$COLLECTOR_IP" ] || { echo "$(loc 'Set COLLECTOR_IP or pass it as the second argument.' 'Задайте COLLECTOR_IP или передайте его вторым аргументом.')"; exit 1; }
[[ "$NODE_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || { echo "$(loc 'Invalid NODE_NAME' 'Некорректная метка ноды')" >&2; exit 1; }
[[ "$COLLECTOR_IP" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo "$(loc 'Use an IPv4 address or hostname for COLLECTOR_IP' 'Для COLLECTOR_IP нужен IPv4-адрес или DNS-имя')" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/logship
: "${COLLECTOR_CERT:?Set COLLECTOR_CERT to the collector certificate copied over trusted SSH}"
[[ -s "$COLLECTOR_CERT" ]] || { echo "$(loc 'Collector certificate missing or empty' 'Сертификат коллектора отсутствует или пуст')" >&2; exit 1; }
command -v openssl >/dev/null || { echo "$(loc 'Install openssl first' 'Сначала установите openssl')" >&2; exit 1; }
openssl x509 -in "$COLLECTOR_CERT" -noout -checkend 0
if grep -q -- 'PRIVATE KEY' "$COLLECTOR_CERT"; then
  echo "$(loc 'Provide only the certificate, never a private key' 'Укажите публичный сертификат коллектора')" >&2
  exit 1
fi

echo "$(loc '== prereqs ==' '== Зависимости ==')"
docker compose version >/dev/null || { echo "$(loc 'Install Docker Engine and the Compose plugin first' 'Сначала установите Docker Engine и плагин Compose')" >&2; exit 1; }

echo "$(loc "== Xray log files ==" "== Журналы Xray ==")"
if [ ! -f /var/log/remnanode/access.log ]; then
  if [[ "${INSTALL_LANG:-en}" == ru ]]; then
    cat <<'WARN'
  !! /var/log/remnanode/access.log НЕ найден. Нужно, чтобы:
     (a) в docker-compose ноды (/opt/remnanode) был mount  /var/log/remnanode:/var/log/remnanode
     (b) в конфиге профиля этой ноды в панели блок log содержал пути
         access:/var/log/remnanode/access.log , error:/var/log/remnanode/error.log
     (см. историю: у PL этих путей не было в панели -> логи не писались на диск).
     Плюс поставь logrotate copytruncate на этот файл (держит диск ноды в узде).
WARN
  else
    echo 'Xray access.log was not found. Configure the node log paths and the log directory mount.'
  fi
fi

echo "$(loc "== Installing to $DST ==" "== Установка в $DST ==")"
mkdir -p "$DST"
install -m 644 -- "$COLLECTOR_CERT" "$DST/collector.crt"
cp "$SRC/docker-compose.yml" "$DST/docker-compose.yml"
sed -e "s/__NODE_NAME__/${NODE_NAME}/g" -e "s/__COLLECTOR_IP__/${COLLECTOR_IP}/g" \
    "$SRC/vector/vector.agent.yaml" > "$DST/vector.yaml"

echo "$(loc '== logrotate xray-логов ==' '== Ротация логов Xray ==')"
if [ -d /etc/logrotate.d ] && [ ! -f /etc/logrotate.d/remnanode ]; then
  cp "$SRC/logrotate-remnanode" /etc/logrotate.d/remnanode && echo "$(loc "Installed /etc/logrotate.d/remnanode (copytruncate)" "Установлен /etc/logrotate.d/remnanode (copytruncate)")"
else
  echo "$(loc "Log rotation setup skipped: configuration exists or logrotate is unavailable." "Ротация пропущена: конфигурация уже есть или logrotate недоступен.")"
fi

echo "$(loc '== up ==' '== Запуск ==')"
( cd "$DST" && docker compose config --quiet && \
  docker compose run --rm --no-deps --volume /vector-data vector validate --skip-healthchecks /etc/vector/vector.yaml && \
  docker compose up -d )
sleep 3
docker logs logship-vector --since 20s 2>&1 | tail -5 || true

# свой публичный IP — чтобы выдать готовую firewall-команду (её выполнять на коллекторе)
SELF_IP="${NODE_SOURCE_IP:-$(hostname -I | awk '{print $1}')}"
[[ "$SELF_IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || SELF_IP="<NODE_SOURCE_IP>"

printf '\n%s\n' "$(loc "== Node $NODE_NAME configured ==" "== Нода $NODE_NAME настроена ==")"
printf '%s\n' "$(loc "On collector $COLLECTOR_IP, allow the node source IP:" "На коллекторе $COLLECTOR_IP разрешите IPv4 источника ноды:")"
printf '  /opt/logcollector/scripts/collector-add-node-fw.sh %s\n' "$SELF_IP"
printf '%s\n' "$(loc "Verify node=$NODE_NAME in Grafana after the first logs arrive." "Проверьте метку node=$NODE_NAME в Grafana после доставки первых логов.")"
