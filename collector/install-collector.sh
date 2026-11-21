#!/usr/bin/env bash
# Установка КОЛЛЕКТОРА лог-сервиса с нуля: Loki + Grafana + Vector-агрегатор + архивный аплоадер.
# Запускать root'ом на облачном VPS.
#
# Переменные (можно задать заранее, иначе спросит):
#   GF_ADMIN_PASSWORD           пароль admin Grafana
#   COLLECTOR_NODE              метка коллектора в логах (nl) или '-' если коллектор не VPN-нода
#   BACKEND                     s3 | gdrive | local
#   ENCRYPT                     age | none        (для gdrive/облака — держи age!)
#   CADENCE                     hourly | daily | weekly
#   AGE_PUBKEY                  age public key архива (если ENCRYPT=age)
#   -- s3 --   S3_ENDPOINT S3_BUCKET S3_REGION S3_ACCESS_KEY S3_SECRET_KEY  [APPLY_COMPLIANCE=yes]
#   -- gdrive -- RCLONE_REMOTE (remote:path; remote заранее через `rclone config`)
#   -- local -- LOCAL_DIR LOCAL_RETENTION_DAYS
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"      # .../collector
REPO="$(cd "$SRC/.." && pwd)"
source "$REPO/scripts/installer-language.sh"
DST=/opt/logcollector

ask(){ local v="$1" p="$2" def="${3:-}"; if [ -z "${!v:-}" ]; then read -rp "$p${def:+ [$def]}: " x; printf -v "$v" '%s' "${x:-$def}"; fi; }

echo "$(loc '== prereqs ==' '== Зависимости ==')"
docker compose version >/dev/null || { echo "$(loc 'Install Docker Engine and the Compose plugin first' 'Сначала установите Docker Engine и плагин Compose')" >&2; exit 1; }
command -v openssl >/dev/null || apt-get install -y openssl
apt-get install -y psmisc util-linux iptables curl jq
apt-get install -y ca-certificates >/dev/null 2>&1 || true; update-ca-certificates >/dev/null 2>&1 || true

if [ -z "${GF_ADMIN_PASSWORD:-}" ]; then
  read -rsp "$(loc 'Grafana admin password (blank to generate)' 'Пароль Grafana (пусто для генерации)'): " GF_ADMIN_PASSWORD
  printf '\n'
  if [ -z "$GF_ADMIN_PASSWORD" ]; then
    GF_ADMIN_PASSWORD="$(openssl rand -hex 24)"
  fi
fi
if [[ ! "$GF_ADMIN_PASSWORD" =~ ^[a-zA-Z0-9_.@%+=:/-]{16,}$ ]]; then
  echo "$(loc 'Use at least 16 characters from A–Z, a–z, 0–9 or _.@%+=:/-' 'Введите не менее 16 символов из A–Z, a–z, 0–9 или _.@%+=:/-')" >&2
  exit 1
fi
ask COLLECTOR_NODE   "$(loc 'Collector log label (use - for a dedicated collector)' "Метка коллектора в логах (напр. collector; '-' если не VPN-нода)")" "collector"
ask BACKEND          "$(loc 'Archive backend: s3 | gdrive | local' "Бэкенд архива: s3 | gdrive | local")" "s3"
ask ENCRYPT          "$(loc 'Archive encryption: age | none' "Шифровать архив age? (age/none)")" "age"
ask CADENCE          "$(loc 'Archive interval: hourly | daily | weekly' "Как часто заливать: hourly | daily | weekly")" "weekly"
[[ "$COLLECTOR_NODE" == "-" || "$COLLECTOR_NODE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || { echo "$(loc 'Invalid COLLECTOR_NODE' 'Некорректное значение COLLECTOR_NODE')" >&2; exit 1; }
case "$ENCRYPT" in age|none) ;; *) echo "$(loc 'Invalid ENCRYPT' 'Некорректное значение ENCRYPT')" >&2; exit 1;; esac
case "$CADENCE" in hourly|daily|weekly) ;; *) echo "$(loc 'Invalid CADENCE' 'Некорректное значение CADENCE')" >&2; exit 1;; esac

if [ "$ENCRYPT" = "age" ]; then
  command -v age >/dev/null || apt-get install -y age
  ask AGE_PUBKEY "$(loc 'Archive age public key (age1...)' "age public key архива (age1...)")"
fi

case "$BACKEND" in
  s3)
    ask S3_ENDPOINT   "$(loc 'S3 endpoint' "S3 endpoint (свой провайдер)")" "https://s3.us-east-1.amazonaws.com"
    ask S3_BUCKET     "S3 bucket" "remnawave-logs"
    ask S3_REGION     "S3 region" "us-east-1"
    ask S3_ACCESS_KEY "S3 access key"
    if [ -z "${S3_SECRET_KEY:-}" ]; then
      read -rsp "$(loc 'S3 secret key' 'Секретный ключ S3'): " S3_SECRET_KEY
      printf '\n'
    fi
    [[ "$S3_ENDPOINT" == https://* && "$S3_ACCESS_KEY" =~ ^[a-zA-Z0-9_.@+=:/-]+$ &&
       "$S3_SECRET_KEY" =~ ^[a-zA-Z0-9_.@+=:/-]+$ && "$S3_REGION" =~ ^[a-zA-Z0-9-]+$ ]] ||
      { echo "$(loc 'Use an HTTPS S3 endpoint and valid S3 credentials/region' 'Укажите HTTPS-адрес S3 и допустимые реквизиты и регион')" >&2; exit 1; } ;;
  gdrive)
    command -v rclone >/dev/null || apt-get install -y rclone
    echo "$(loc 'Configure a Drive remote first with rclone config.' 'Сначала настройте remote типа drive через rclone config.')"
    ask RCLONE_REMOTE "$(loc 'rclone remote:path' "rclone remote:path")" "gdrive:remnawave-logs" ;;
  local)
    ask LOCAL_DIR             "$(loc 'Local archive directory' "Каталог архива на коллекторе")" "$DST/archive/store"
    ask LOCAL_RETENTION_DAYS  "$(loc 'Local retention in days (0 keeps all archives)' "Retention локально, дней (0=не чистить)")" "180" ;;
  *) echo "$(loc "Unsupported backend: $BACKEND" "Неизвестный тип архива: $BACKEND")"; exit 1 ;;
esac

echo "$(loc "== Installing to $DST ==" "== Установка в $DST ==")"
mkdir -p "$DST"
cp -r "$SRC/loki" "$SRC/grafana" "$SRC/vector" "$SRC/archive" "$DST/"
mkdir -p "$DST/vector/tls" "$DST/spool" "$DST/secrets" "$DST/report"
cp "$SRC/docker-compose.yml" "$DST/docker-compose.yml"
cp -r "$REPO/scripts" "$DST/scripts"           # нужны и после удаления репо (systemd-файрвол)
chmod +x "$DST/archive/archive-upload.sh" "$DST"/scripts/*.sh

umask 077
printf 'GF_ADMIN_PASSWORD=%s\n' "$GF_ADMIN_PASSWORD" > "$DST/.env"
[ "$ENCRYPT" = "age" ] && printf '%s\n' "$AGE_PUBKEY" > "$DST/secrets/archive.pub"

# archive.conf
{
  echo "BACKEND=$BACKEND"
  echo "ENCRYPT=$ENCRYPT"
  printf '%s=%q\n' "AGE_PUBKEY_FILE" "$DST/secrets/archive.pub"
  echo "SETTLED_MIN=65"
  case "$BACKEND" in
    s3)
      printf '%s=%q\n' "S3_ENDPOINT" "$S3_ENDPOINT"; printf '%s=%q\n' "S3_BUCKET" "$S3_BUCKET"; printf '%s=%q\n' "S3_REGION" "$S3_REGION"
      printf '%s=%q\n' "S3_ENV" "$DST/secrets/s3.env"
      printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_DEFAULT_REGION=%s\n' \
        "$S3_ACCESS_KEY" "$S3_SECRET_KEY" "$S3_REGION" > "$DST/secrets/s3.env"; chmod 600 "$DST/secrets/s3.env" ;;
    gdrive) printf '%s=%q\n' "RCLONE_REMOTE" "$RCLONE_REMOTE" ;;
    local)  printf '%s=%q\n' "LOCAL_DIR" "$LOCAL_DIR"; printf '%s=%q\n' "LOCAL_RETENTION_DAYS" "$LOCAL_RETENTION_DAYS" ;;
  esac
} > "$DST/archive/archive.conf"
chmod 600 "$DST/archive/archive.conf" "$DST/.env"
[ -f "$DST/secrets/archive.pub" ] && chmod 600 "$DST/secrets/archive.pub"

# метка коллектора в агрегаторе
sed -i "s/__COLLECTOR_NODE__/${COLLECTOR_NODE}/g" "$DST/vector/vector.aggregator.yaml"
if [ "$COLLECTOR_NODE" = "-" ]; then
  sed -i '/^  # локальные/,/^transforms:/{ /^transforms:/!d; }; /^  local_meta:/,/^  checked_nodes:/{ /^  checked_nodes:/!d; }' "$DST/vector/vector.aggregator.yaml"
  sed -i 's/inputs: \[checked_nodes, local_meta\]/inputs: [checked_nodes]/g' "$DST/vector/vector.aggregator.yaml"
  sed -i '\|/self-logs|d' "$DST/docker-compose.yml"
fi

chmod +x "$REPO"/scripts/*.sh 2>/dev/null || true   # tar/zip-инсталл может не сохранить exec-бит
"$REPO/scripts/gen-tls.sh" "$DST/vector/tls"

# файрвол приёмника: default-deny 6000 в DOCKER-USER (Docker обходит ufw!) + reboot-persist через systemd
touch "$DST/allowed-nodes.txt"
chmod 0700 "$DST/spool" "$DST/secrets"
cp "$SRC/systemd/collector-firewall.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now collector-firewall.service
# Apply even when the oneshot unit is already active. Fail before publishing 6000.
"$DST/scripts/collector-firewall-apply.sh"

echo "$(loc '== validate and up ==' '== Проверка и запуск ==')"
( cd "$DST" && docker compose config --quiet && \
  docker compose run --rm --no-deps --volume /vector-data vector validate --skip-healthchecks /etc/vector/vector.yaml && \
  docker compose up -d loki && \
  curl -fsS --retry 30 --retry-all-errors --retry-delay 2 --max-time 5 --noproxy '*' http://127.0.0.1:3100/ready && \
  docker compose up -d )

echo "$(loc "== Archive timer ($CADENCE) ==" "== Таймер архива ($CADENCE) ==")"
case "$CADENCE" in
  hourly) OC="hourly" ;;
  daily)  OC="*-*-* 04:00:00" ;;
  weekly) OC="Sun *-*-* 04:00:00" ;;
  *) OC="Sun *-*-* 04:00:00" ;;
esac
cp "$SRC/systemd/archive-upload.service" /etc/systemd/system/
sed "s|__ONCALENDAR__|$OC|" "$SRC/systemd/archive-upload.timer" > /etc/systemd/system/archive-upload.timer
systemctl daemon-reload
systemctl enable --now archive-upload.timer

# отчёт фазы 1
install -m 755 "$REPO/report/investigate.sh" "$DST/report/investigate.sh"
cp "$REPO/report/README.md" "$DST/report/README.md" 2>/dev/null || true

# COMPLIANCE (только s3)
if [ "$BACKEND" = "s3" ] && [ "${APPLY_COMPLIANCE:-}" = "yes" ]; then
  echo "== COMPLIANCE 180д на бакет $S3_BUCKET =="
  docker run --rm --env-file "$DST/secrets/s3.env" \
    -v /etc/ssl/certs/ca-certificates.crt:/ca.crt:ro -e AWS_CA_BUNDLE=/ca.crt \
    amazon/aws-cli@sha256:33a16c53ea442e918b58df67301ad76a8fb57082c39f7e78753b898c3f3e87c2 --endpoint-url "$S3_ENDPOINT" \
    s3api put-object-lock-configuration --bucket "$S3_BUCKET" \
    --object-lock-configuration '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"COMPLIANCE","Days":180}}}' \
    && echo "COMPLIANCE применён"
fi

# свой публичный IP — чтобы выдать ГОТОВУЮ команду установки ноды (с уже подставленным IP коллектора)
SELF_IP="${COLLECTOR_ADDRESS:-$(hostname -I | awk '{print $1}')}"
[[ "$SELF_IP" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || SELF_IP="<COLLECTOR_ADDRESS>"
printf 'COLLECTOR_CERT=./collector.crt COLLECTOR_IP=%s ./install-node.sh <NODE_NAME>\n' "$SELF_IP" > "$DST/node-install.txt"

printf '\n%s\n' "$(loc "== Collector ready ($BACKEND, $ENCRYPT, $CADENCE) ==" "== Коллектор готов ($BACKEND, $ENCRYPT, $CADENCE) ==")"
printf '%s\n' "$(loc "Open Grafana with an SSH tunnel:" "Откройте Grafana через SSH-туннель:")"
printf '  ssh -N -L 3000:127.0.0.1:3000 root@%s\n' "$SELF_IP"
printf '%s\n' "$(loc "Grafana is at http://127.0.0.1:3000. Read the admin password from $DST/.env." "Grafana доступна на http://127.0.0.1:3000. Пароль admin хранится в $DST/.env.")"
printf '%s\n' "$(loc "Copy the collector certificate over trusted SSH. Node command: $DST/node-install.txt." "Скопируйте сертификат через доверенное SSH. Команда установки ноды: $DST/node-install.txt.")"
printf '%s\n' "$(loc 'To test archiving, start archive-upload.service and check its journal.' 'Для проверки архивации запустите archive-upload.service и посмотрите его журнал.')"
case "$BACKEND" in
  s3) printf '%s\n' "$(loc 'For immutable retention, configure Object Lock and versioning in the bucket.' 'Для неизменяемого хранения настройте Object Lock и версионирование бакета.')" ;;
  local) printf '%s\n' "$(loc 'To retrieve local archives over SSH, use scripts/pull-to-pc.sh.' 'Для выгрузки локальных архивов по SSH используйте scripts/pull-to-pc.sh.')" ;;
esac
