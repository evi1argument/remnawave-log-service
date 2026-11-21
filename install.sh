#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$repo/scripts/installer-language.sh"
INSTALL_LANG="${INSTALL_LANG:-en}"
case "${1:-}" in
  --lang) INSTALL_LANG="${2:-}"; shift; (( $# == 0 )) || shift ;;
  --lang=*) INSTALL_LANG="${1#*=}"; shift ;;
esac
case "$INSTALL_LANG" in
  en|ru) ;;
  *)
    printf 'Usage / Синтаксис: --lang en|ru\n' >&2
    exit 2 ;;
esac
export INSTALL_LANG
collector=/opt/logcollector
agent=/opt/logship
status_dir=/opt/remnawave-statusboard-v2
status_config=/etc/remnawave-statusboard-v2.json
status_env=/etc/remnawave-statusboard.env
status_unit=remnawave-status-collect
notify_dir="$collector/notify"
notify_unit=log-service-notify

accent='' blue='' orange='' muted='' bold='' reset='' danger='' prompt=''
if [[ -t 1 && "${TERM:-dumb}" != dumb && -z "${NO_COLOR:-}" ]]; then
  blue=$'\033[38;5;75m'
  orange=$'\033[38;5;208m'
  if [[ "${COLORTERM:-}" == truecolor || "${COLORTERM:-}" == 24bit ]]; then
    blue=$'\033[38;2;46;134;255m'
    orange=$'\033[38;2;255;122;24m'
  fi
  accent=$'\033[38;5;111m'
  muted=$'\033[38;5;109m'
  bold=$'\033[1m'
  reset=$'\033[0m'
  danger=$'\033[38;5;203m'
  prompt='❯ '
fi

fail() { printf '%s%s:%s %s\n' "$danger" "$(loc 'Error' 'Ошибка')" "$reset" "$*" >&2; return 1; }

ask() {
  local text="$1" fallback="${2:-}" answer
  printf '%s%s%s%s%s: ' "$accent" "$prompt" "$reset" "$text" "${fallback:+ [$fallback]}" >&2
  IFS= read -r answer || return 1
  printf '%s' "${answer:-$fallback}"
}

confirm() {
  local answer
  answer="$(ask "$1 $(loc '(y/N)' '(д/Н)')")" || return 1
  case "$answer" in
    y|Y|yes|YES|Yes|д|Д|да|Да|ДА) return 0 ;;
    *) return 1 ;;
  esac
}

usage() {
  if [[ "$INSTALL_LANG" == ru ]]; then
    cat <<'EOF'
Синтаксис: Remnawave Log Service

  sudo bash install.sh --lang ru              Меню установки
  sudo bash install.sh --lang ru collector    Новый коллектор
  sudo bash install.sh --lang ru node         Новый агент ноды
  sudo bash install.sh --lang ru allow-node   Разрешить доступ ноды
  sudo bash install.sh --lang ru statusboard  Мониторинг доступности
  sudo bash install.sh --lang ru doctor       Диагностика
  sudo bash install.sh --lang ru notify       Настроить уведомления Telegram
  sudo bash install.sh --lang ru notify status|test|disable

Коллектор и агент устанавливаются на своих серверах.
Мониторинг доступности запускается на сервере с локальным API Remnawave.
EOF
    return
  fi
  cat <<'EOF'
Usage: Remnawave Log Service

  sudo bash install.sh              Open the setup menu
  sudo bash install.sh collector    Install a new log collector
  sudo bash install.sh node         Install a new node agent
  sudo bash install.sh allow-node   Allow a node on this collector
  sudo bash install.sh statusboard  Set up local availability monitoring
  sudo bash install.sh doctor       Check dependencies and services
  sudo bash install.sh notify       Configure Telegram alerts
  sudo bash install.sh notify status|test|disable
  sudo bash install.sh --lang ru    Open the menu in Russian

Run the collector and node steps on their respective hosts.
The statusboard runs on the host that has the local Remnawave API.
EOF
}

require_linux() {
  [[ "$(uname -s)" == Linux ]] || fail "$(loc 'Run this on the target Linux server.' 'Запустите установщик на целевом Linux-сервере.')"
  [[ "$EUID" == 0 ]] || fail "$(loc 'Run this action with sudo.' 'Запустите это действие через sudo.')"
  command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] ||
    fail "$(loc 'A running systemd instance is required.' 'Требуется запущенный systemd.')"
  command -v flock >/dev/null || fail "$(loc 'Install util-linux to continue.' 'Установите пакет util-linux.')"
}

require_docker() {
  command -v docker >/dev/null && docker version --format '{{.Server.Version}}' >/dev/null 2>&1 &&
    docker compose version >/dev/null 2>&1 ||
    fail "$(loc 'Install Docker Engine and the Compose plugin first. Instructions: https://docs.docker.com/engine/install/' 'Сначала установите Docker Engine и плагин Compose. Инструкция: https://docs.docker.com/engine/install/')"
}

unused() {
  local path
  for path in "$@"; do
    if [[ -e "$path" || -L "$path" ]]; then
      fail "$(loc "Existing installation or partial setup at $path. See docs/UPGRADING.md or run doctor." "Найдена существующая или незавершённая установка: $path. См. docs/UPGRADING.md или запустите doctor.")"
      return 1
    fi
  done
}

lock_install() {
  exec 9>/run/lock/remnawave-log-service-setup.lock
  flock -n 9 || fail "$(loc 'Another setup action is running on this server.' 'На этом сервере уже выполняется установка.')"
}

valid_host() { [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; }

require_unused_containers() {
  local name
  for name in "$@"; do
    if docker container inspect "$name" >/dev/null 2>&1; then
      fail "$(loc "Container $name already exists. Run doctor or follow docs/UPGRADING.md." "Контейнер $name уже существует. Запустите doctor или см. docs/UPGRADING.md.")"
      return 1
    fi
  done
}

install_collector() {
  banner "$(loc 'Install a collector' 'Установить коллектор')"
  require_linux
  require_docker
  lock_install
  unused "$collector" /etc/systemd/system/collector-firewall.service /etc/systemd/system/archive-upload.timer
  require_unused_containers logcollector-loki logcollector-grafana logcollector-vector
  if command -v ss >/dev/null; then
    local busy
    busy="$(ss -ltnH | awk '$4 ~ /:(3000|3100|6000)$/ {print $4}')"
    [[ -z "$busy" ]] || fail "$(loc "Collector ports already in use: $busy" "Порты коллектора уже заняты: $busy")"
  fi
  command -v apt-get >/dev/null || fail "$(loc 'Collector setup is supported on Debian and Ubuntu.' 'Установка коллектора поддерживается на Debian и Ubuntu.')"
  local address cadence retention encryption=none key=''
  address="$(ask "$(loc 'Collector IPv4 address or DNS name used by the nodes' 'IPv4-адрес или DNS-имя коллектора, доступное с нод')")" || return 1
  valid_host "$address" || fail "$(loc 'Enter a DNS name or IPv4 address.' 'Введите DNS-имя или IPv4-адрес.')"
  cadence="$(ask "$(loc 'Archive frequency (hourly, daily, weekly)' 'Периодичность архивации (hourly, daily, weekly)')" daily)" || return 1
  case "$cadence" in hourly|daily|weekly) ;; *) fail "$(loc 'Choose hourly, daily, or weekly.' 'Выберите hourly, daily или weekly.')"; return 1;; esac
  retention="$(ask "$(loc 'Days to keep local archives' 'Срок хранения локальных архивов в днях')" 30)" || return 1
  [[ "$retention" =~ ^[1-9][0-9]{0,3}$ ]] || fail "$(loc 'Enter a positive retention in days.' 'Введите положительное число дней.')"
  key="$(ask "$(loc 'Optional age public key (leave blank for a plaintext local archive)' 'Публичный ключ age (пусто для локального архива без шифрования)')")" || return 1
  if [[ -n "$key" ]]; then
    [[ "$key" =~ ^age1[0-9a-z]{58}$ ]] || fail "$(loc 'Enter an age public key beginning with age1.' 'Введите публичный ключ age, начинающийся с age1.')"
    encryption=age
  fi
  section "$(loc 'REVIEW' 'ПРОВЕРКА НАСТРОЕК')"
  detail "$(loc 'Collector' 'Коллектор')" "$address"
  detail "$(loc 'Archive' 'Архив')" "$collector/archive/store"
  detail "$(loc 'Schedule' 'Периодичность')" "$(loc "$cadence · $retention days · encryption: $encryption" "$cadence · $retention дн. · шифрование: $encryption")"
  printf '\n  %s\n\n' "$(loc 'Grafana stays on loopback. Node access is granted individually on TCP/6000.' 'Grafana доступна через loopback. Доступ нод к TCP/6000 открывается по одному адресу.')"
  confirm "$(loc 'Install the collector' 'Установить коллектор')" || { echo "$(loc 'Cancelled.' 'Отменено.')"; return 0; }
  command -v openssl >/dev/null || apt-get install -y openssl
  GF_ADMIN_PASSWORD="$(openssl rand -hex 24)" COLLECTOR_ADDRESS="$address" COLLECTOR_NODE=- \
    BACKEND=local ENCRYPT="$encryption" AGE_PUBKEY="$key" CADENCE="$cadence" \
    LOCAL_DIR="$collector/archive/store" LOCAL_RETENTION_DAYS="$retention" \
    bash "$repo/collector/install-collector.sh" </dev/null
  curl -fsS --noproxy '*' --retry 10 --retry-all-errors --retry-delay 1 --max-time 3 \
    http://127.0.0.1:3000/api/health >/dev/null ||
    fail "$(loc 'Grafana has not reported healthy. Run doctor to inspect the installed services.' 'Grafana не подтвердила готовность. Проверьте службы через doctor.')"
  printf '\n%s\n' "$(loc "Next: copy $collector/vector/tls/cert.pem to each node over trusted SSH, then run node setup." "Далее перенесите $collector/vector/tls/cert.pem на каждую ноду через доверенное SSH-соединение и запустите настройку агента.")"
}

install_node() {
  banner "$(loc 'Install a node agent' 'Установить агент ноды')"
  require_linux
  require_docker
  lock_install
  unused "$agent"
  require_unused_containers logship-vector
  local label address certificate
  label="$(ask "$(loc 'Node label, such as de-1' 'Метка ноды, например de-1')")" || return 1
  [[ "$label" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || fail "$(loc 'Use a valid node label.' 'Введите допустимую метку ноды.')"
  address="$(ask "$(loc 'Collector IPv4 address or DNS name' 'IPv4-адрес или DNS-имя коллектора')")" || return 1
  valid_host "$address" || fail "$(loc 'Enter a DNS name or IPv4 address.' 'Введите DNS-имя или IPv4-адрес.')"
  certificate="$(ask "$(loc 'Path to the trusted collector certificate' 'Путь к доверенному сертификату коллектора')" ./collector.crt)" || return 1
  [[ -f "$certificate" && -s "$certificate" ]] || fail "$(loc 'Copy the collector certificate over trusted SSH first.' 'Сначала перенесите сертификат коллектора через доверенное SSH-соединение.')"
  [[ -f /var/log/remnanode/access.log ]] ||
    fail "$(loc 'This node has no Xray access log at /var/log/remnanode/access.log. See docs/INSTALLATION.md.' 'На ноде отсутствует журнал Xray /var/log/remnanode/access.log. См. docs/INSTALLATION.md.')"
  section "$(loc 'REVIEW' 'ПРОВЕРКА НАСТРОЕК')"
  detail "$(loc 'Node' 'Нода')" "$label"
  detail "$(loc 'Collector' 'Коллектор')" "$address"
  detail "$(loc 'Certificate' 'Сертификат')" "$certificate"
  printf '\n'
  confirm "$(loc "Install node $label and send its Xray logs to $address" "Установить агент $label и отправлять логи Xray на $address")" || { echo "$(loc 'Cancelled.' 'Отменено.')"; return 0; }
  COLLECTOR_CERT="$certificate" bash "$repo/node/install-node.sh" "$label" "$address" </dev/null
  printf '\n%s\n' "$(loc 'Next: run allow-node on the collector with this node’s source IPv4 address.' 'Далее выполните allow-node на коллекторе и укажите IPv4-адрес источника этой ноды.')"
}

allow_node() {
  banner "$(loc 'Allow a node' 'Разрешить доступ ноды')"
  require_linux
  require_docker
  lock_install
  [[ -x "$collector/scripts/collector-add-node-fw.sh" ]] || fail "$(loc 'Install the collector on this host first.' 'Сначала установите коллектор на этом сервере.')"
  local address
  address="$(ask "$(loc 'Node IPv4 address as seen by the collector' 'IPv4-адрес ноды со стороны коллектора')")" || return 1
  confirm "$(loc "Allow this address on collector TCP/6000: $address" "Разрешить доступ к TCP/6000 коллектора для $address")" || { echo "$(loc 'Cancelled.' 'Отменено.')"; return 0; }
  ALLOWED_NODES="$collector/allowed-nodes.txt" "$collector/scripts/collector-add-node-fw.sh" "$address"
}

install_statusboard() (
  banner "$(loc 'Set up availability monitoring' 'Настроить мониторинг')"
  require_linux
  lock_install
  unused "$status_dir" "$status_config" "$status_env" /var/lib/remnawave-statusboard-v2 \
    "/etc/systemd/system/$status_unit.service" "/etc/systemd/system/$status_unit.timer"
  command -v python3 >/dev/null &&
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    fail "$(loc 'Install Python 3.10 or newer first.' 'Сначала установите Python 3.10 или новее.')"
  local url token staged
  url="$(ask "$(loc 'Local Remnawave nodes API URL' 'URL локального API нод Remnawave')" http://127.0.0.1:3000/api/nodes)" || return 1
  printf '%s: ' "$(loc 'Remnawave API token (input hidden)' 'API-токен Remnawave (ввод скрыт)')" >&2
  IFS= read -rs token || return 1
  printf '\n' >&2
  [[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || fail "$(loc 'Enter a nonempty token without spaces.' 'Введите непустой токен без пробелов.')"
  staged="$(mktemp)"
  trap 'rm -f -- "$staged"' EXIT
  REMNAWAVE_API_TOKEN="$token" python3 "$repo/statusboard/setup_config.py" "$url" >"$staged"
  printf '%s\n' "$(loc 'The installer detected locations and will collect into a private snapshot on this host.' 'Локации найдены. Снимки будут сохраняться в закрытый каталог на этом сервере.')"
  confirm "$(loc 'Start local availability monitoring' 'Запустить мониторинг доступности')" || { echo "$(loc 'Cancelled.' 'Отменено.')"; return 0; }
  install -d -m 0755 "$status_dir"
  install -m 0644 "$repo"/statusboard/{adapter,statusboard,check_public,probe}.py "$status_dir/"
  install -m 0600 "$staged" "$status_config"
  (umask 077; printf 'REMNAWAVE_API_TOKEN=%s\n' "$token" >"$status_env")
  install -m 0644 "$repo"/statusboard/systemd/"$status_unit".{service,timer} /etc/systemd/system/
  systemctl daemon-reload
  systemctl start "$status_unit.service"
  python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); sys.exit(not any(state != "unknown" for _, state in data["locations"]))' \
    /var/lib/remnawave-statusboard-v2/status-candidate.json ||
    fail "$(loc 'No locations were measured during the initial poll. Check the statusboard service journal.' 'Первая проверка не получила замеров. Посмотрите журнал службы Statusboard.')"
  systemctl enable --now "$status_unit.timer"
  printf '%s: /var/lib/remnawave-statusboard-v2/status-candidate.json\n' "$(loc 'Snapshot' 'Снимок')"
  printf '%s\n' "$(loc 'See statusboard/README.md for public JSON and end-to-end VPN mode.' 'Публикация JSON и сквозные проверки описаны в statusboard/README.ru.md.')"
)

banner() {
  if [[ -n "$accent" ]]; then
    printf '\n  %s▌%s  %sLOG%s %sSERVICE%s  %s/ Remnawave%s\n' \
      "$orange" "$reset" "$bold$accent" "$reset" "$bold" "$reset" "$muted" "$reset"
    printf '     %s%s%s\n' "$muted" "$1" "$reset"
  else
    printf '\n  REMNAWAVE / LOG SERVICE\n  %s\n' "$1"
  fi
}

section() {
  printf '\n  %s%s%s\n' "$accent$bold" "$1" "$reset"
}

detail() {
  local LC_ALL=C.UTF-8
  local width=$(( 16 - ${#1} ))
  (( width > 0 )) || width=1
  printf '  %s%s%s%*s%s\n' "$muted" "$1" "$reset" "$width" '' "$2"
}

host_summary() {
  local os arch cores ram
  os="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 || true)"
  os="${os#\"}"; os="${os%\"}"
  arch="$(uname -m)"
  cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo unknown)"
  ram="$(awk '/^MemTotal:/ {printf "%.1f", $2 / 1048576}' /proc/meminfo 2>/dev/null || true)"
  if [[ -n "$ram" ]]; then ram="$ram $(loc 'GiB RAM' 'ГиБ ОЗУ')"; fi
  detail "$(loc 'System' 'Система')" "${os:-$(uname -s)} · $arch"
  detail "$(loc 'Resources' 'Ресурсы')" "$cores CPU${ram:+ · $ram}"
}

install_state() {
  if [[ -f "$1" ]]; then
    loc configured настроен
  else
    loc 'not configured' 'не настроен'
  fi
}

menu_item() {
  local LC_ALL=C.UTF-8
  local width=$(( 31 - ${#2} )) marker=' ' title='' number="$muted"
  (( width > 0 )) || width=1
  if [[ "$1" == "${menu_focus:-}" ]]; then
    marker='›' title="$bold$accent" number="$orange"
  fi
  printf '  %s%s%s %s%s%s  %s%s%s' "$orange" "$marker" "$reset" "$number" "$1" "$reset" "$title" "$2" "$reset"
  if (( ${menu_columns:-80} >= 76 )); then
    printf '%*s%s%s%s' "$width" '' "$muted" "$3" "$reset"
  fi
  printf '\n'
}

setup_notify() {
  require_linux
  local action="${1:-configure}"
  case "$action" in
    status)
      if [[ -f "$notify_dir/config.json" ]]; then
        systemctl is-active "$notify_unit.timer" || true
        python3 "$notify_dir/monitor.py" status --lang "$INSTALL_LANG"
      else
        printf '%s\n' "$(loc 'Telegram alerts are not configured.' 'Уведомления Telegram не настроены.')"
      fi ;;
    test)
      [[ -f "$notify_dir/config.json" ]] ||
        { fail "$(loc 'Configure Telegram alerts first.' 'Сначала настройте уведомления Telegram.')"; return 1; }
      python3 "$notify_dir/monitor.py" test --lang "$INSTALL_LANG" ;;
    disable)
      lock_install
      if [[ -f "/etc/systemd/system/$notify_unit.timer" ]]; then
        systemctl disable --now "$notify_unit.timer"
        systemctl stop "$notify_unit.service"
        rm -f -- /etc/systemd/system/archive-upload.service.d/50-log-service-notify.conf
        systemctl daemon-reload
      fi ;;
    configure)
      [[ -f "$collector/docker-compose.yml" ]] ||
        { fail "$(loc 'Install the collector first.' 'Сначала установите коллектор.')"; return 1; }
      lock_install
      command -v python3 >/dev/null || apt-get install -y python3
      install -d -m 0700 "$notify_dir"
      python3 "$repo/collector/notify/monitor.py" configure --lang "$INSTALL_LANG"
      install -m 0700 "$repo/collector/notify/monitor.py" "$notify_dir/monitor.py"
      install -m 0644 "$repo/collector/notify/$notify_unit.service" \
        "$repo/collector/notify/$notify_unit.timer" /etc/systemd/system/
      install -d -m 0755 /etc/systemd/system/archive-upload.service.d
      install -m 0644 "$repo/collector/notify/archive-success.conf" \
        /etc/systemd/system/archive-upload.service.d/50-log-service-notify.conf
      systemctl daemon-reload
      systemctl enable --now "$notify_unit.timer"
      printf '%s\n' "$(loc 'Telegram alerts configured. Use notify test to send a test message.' 'Уведомления настроены. Команда notify test отправляет тестовое сообщение.')" ;;
    *) usage >&2; return 2 ;;
  esac
}

setup_menu() {
  banner "$(loc 'Setup' 'Установка')"
  section "$(loc 'SERVER' 'СЕРВЕР')"
  host_summary
  detail "$(loc 'Collector' 'Коллектор')" "$(install_state "$collector/docker-compose.yml")"
  detail "$(loc 'Node agent' 'Агент ноды')" "$(install_state "$agent/docker-compose.yml")"
  detail 'Statusboard' "$(install_state "$status_config")"
  section "$(loc 'SETUP' 'НАСТРОЙКА')"
  menu_item 1 "$(loc 'Install a collector' 'Установить коллектор')" "$(loc 'Loki, Grafana, archive' 'Loki, Grafana и архив')"
  menu_item 2 "$(loc 'Install a node agent' 'Установить агент ноды')" "$(loc 'Send logs over TLS' 'Передавать логи по TLS')"
  menu_item 3 "$(loc 'Allow a node' 'Разрешить доступ ноды')" "$(loc 'Add an IPv4 address' 'Добавить IPv4 в список доступа')"
  menu_item 4 "$(loc 'Set up availability monitoring' 'Настроить мониторинг')" "$(loc 'Publish a status feed' 'Собирать статусы доступности')"
  menu_item 5 "$(loc 'Check this server' 'Проверить сервер')" "$(loc 'Dependencies and services' 'Зависимости и службы')"
  menu_item 6 "$(loc 'Configure Telegram alerts' 'Настроить Telegram')" "$(loc 'Select checks and destination' 'Выбрать проверки и чат')"
  if [[ -z "${menu_focus:-}" ]]; then
    menu_item L "$(loc 'Language · Русский' 'Язык · English')" ''
    menu_item 0 "$(loc 'Exit' 'Выход')" ''
  fi
}

switch_language() {
  if [[ "$INSTALL_LANG" == en ]]; then INSTALL_LANG=ru; else INSTALL_LANG=en; fi
}

terminal_menu() {
  local key tail rows menu_focus=1 menu_columns=80
  read -r rows menu_columns < <(stty size 2>/dev/null) || true
  [[ "${menu_columns:-}" =~ ^[1-9][0-9]*$ ]] || menu_columns=80
  printf '\033[?1049h'
  trap 'printf "\033[?1049l"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  while true; do
    printf '\033[H\033[J'
    setup_menu
    printf '\n  %s%s%s\n' "$muted" "$(loc '↑/↓ Select · Enter Open · L Русский · 0 Exit' '↑/↓ Выбор · Enter Открыть · L English · 0 Выход')" "$reset"
    if ! IFS= read -rsn1 key; then choice=0; break; fi
    case "$key" in
      '') choice="$menu_focus"; break ;;
      0|q|Q) choice=0; break ;;
      [1-6]) menu_focus="$key" ;;
      l|L|л|Л|д|Д) switch_language ;;
      $'\033')
        tail=''
        IFS= read -rsn2 -t .15 tail || true
        case "$tail" in
          '[A'|'OA') menu_focus=$(( (menu_focus + 4) % 6 + 1 )) ;;
          '[B'|'OB') menu_focus=$(( menu_focus % 6 + 1 )) ;;
          '') choice=0; break ;;
        esac ;;
    esac
  done
  printf '\033[?1049l'
  trap - EXIT INT TERM
}

doctor() {
  banner "$(loc 'Diagnostics' 'Диагностика')"
  section "$(loc 'SERVER' 'СЕРВЕР')"
  host_summary
  detail "$(loc 'Disk /' 'Диск /')" "$(df -hP / 2>/dev/null | awk -v free="$(loc 'free of' 'свободно из')" -v used="$(loc used занято)" 'NR == 2 {print $4 " " free " " $2 " (" $5 " " used ")"}' || true)"
  section "$(loc 'DEPENDENCIES' 'ЗАВИСИМОСТИ')"
  if command -v docker >/dev/null; then
    detail 'Docker Engine' "$(docker version --format '{{.Server.Version}}' 2>/dev/null || loc inaccessible 'нет доступа')"
    detail 'Docker Compose' "$(docker compose version --short 2>/dev/null || loc unavailable недоступен)"
    if [[ -f "$collector/docker-compose.yml" ]]; then
      (cd "$collector" && docker compose ps) || true
      if command -v curl >/dev/null; then
        curl -fsS --noproxy '*' --max-time 4 http://127.0.0.1:3100/ready || echo "$(loc 'Loki: not ready' 'Loki не готов')"
      fi
    fi
    if [[ -f "$agent/docker-compose.yml" ]]; then
      (cd "$agent" && docker compose ps) || true
    fi
  else
    detail 'Docker' "$(loc 'missing' 'не установлен')"
  fi
  if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    section "$(loc 'SERVICES' 'СЛУЖБЫ')"
    for unit in collector-firewall.service archive-upload.timer "$status_unit.timer" "$notify_unit.timer"; do
      local state
      state="$(systemctl is-active "$unit" 2>/dev/null)" || true
      case "$state" in
        active) state="$(loc active активна)" ;;
        inactive) state="$(loc inactive неактивна)" ;;
        failed) state="$(loc failed сбой)" ;;
        '') state="$(loc unknown неизвестно)" ;;
      esac
      printf '  %s%-31s%s %s\n' "$muted" "$unit" "$reset" "$state"
    done
  fi
  printf '\n'
}

case "${1:-menu}" in
  -h|--help|help) usage ;;
  -V|--version) printf 'Remnawave Log Service %s\n' "$(<"$repo/VERSION")" ;;
  doctor) doctor ;;
  collector) install_collector ;;
  node) install_node ;;
  allow-node) allow_node ;;
  statusboard) install_statusboard ;;
  notify) setup_notify "${2:-configure}" ;;
  menu)
    if [[ -t 0 && -n "$accent" ]]; then
      terminal_menu
    else
      while true; do
        setup_menu
        choice="$(ask "$(loc 'Choose an action' 'Выберите действие')")" || exit 0
        case "$choice" in
          [0-6]|'') break ;;
          l|L|л|Л|д|Д) switch_language ;;
          *) fail "$(loc 'Choose a number from 0 to 6, or L.' 'Выберите число от 0 до 6 или L.')" || true ;;
        esac
      done
    fi
    case "$choice" in
      1) install_collector ;;
      2) install_node ;;
      3) allow_node ;;
      4) install_statusboard ;;
      5) doctor ;;
      6) setup_notify ;;
    esac ;;
  *) usage >&2; exit 2 ;;
esac
