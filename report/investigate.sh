#!/usr/bin/env bash
# investigate.sh — выборка логов «IP / юзер / домен за период -> цепочка соединений»
# Фаза 1 (на коллекторе): тянет из Loki, парсит xray-access, отдаёт TSV-цепочку.
# Сопоставление t_id -> аккаунт делается ОТДЕЛЬНО на панели: /root/resolve-users.sh (см. вывод в конце).
set -euo pipefail
umask 077

LOKI="${LOKI:-http://127.0.0.1:3100}"
NODE=".+"; TID=".+"; IP=".+"; DOMAIN=".+"
FROM=""; TO="now"; LIMIT=5000; OUT=""

usage() {
  cat <<EOF
Usage: $0 --from <t> [--to <t>] [--user <t_id>] [--ip <addr>] [--domain <re>] [--node <re>] [--out file.tsv]
  --from/--to   RFC3339 (2026-07-01T00:00:00Z) или '-3d','-12h','now'. По умолчанию --to now.
  --user        t_id пользователя (число, xray 'email: N'); поддерживает regex.
  --ip          IP клиента (или regex).
  --domain      назначение: подстрока/regex (напр. 'example\.com').
  --node        метка ноды (regex). По умолчанию все.
  --out         записать цепочку в TSV-файл (по умолч. только stdout).
Примеры:
  $0 --from -24h --ip 203.0.113.7
  $0 --from 2026-01-10T00:00:00Z --to 2026-01-11T00:00:00Z --user 42
  $0 --from -7d --domain 'example\.com' --node node1
EOF
  exit 1
}

# относительное время (-3d,-12h,-30m,now) -> RFC3339 UTC
resolve_ts() {
  local v="$1"
  if [[ "$v" == "now" ]]; then date -u +%Y-%m-%dT%H:%M:%SZ; return; fi
  if [[ "$v" =~ ^-([0-9]+)([smhd])$ ]]; then
    local n="${BASH_REMATCH[1]}" u="${BASH_REMATCH[2]}"
    case "$u" in s) date -u -d "-$n seconds" +%Y-%m-%dT%H:%M:%SZ;; m) date -u -d "-$n minutes" +%Y-%m-%dT%H:%M:%SZ;;
      h) date -u -d "-$n hours" +%Y-%m-%dT%H:%M:%SZ;; d) date -u -d "-$n days" +%Y-%m-%dT%H:%M:%SZ;; esac
    return
  fi
  echo "$v"  # предполагаем уже RFC3339
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="$2"; shift 2;; --to) TO="$2"; shift 2;;
    --user) TID="$2"; shift 2;; --ip) IP="$2"; shift 2;;
    --domain) DOMAIN="$2"; shift 2;; --node) NODE="$2"; shift 2;;
    --out) OUT="$2"; shift 2;; -h|--help) usage;;
    *) echo "unknown arg: $1" >&2; usage;;
  esac
done
[[ -z "$FROM" ]] && usage

START="$(resolve_ts "$FROM")"; END="$(resolve_ts "$TO")"

# RE2-паттерн парсинга access-строки Reality-ноды
PAT='from (?P<client_ip>[0-9.]+):[0-9]+ accepted (?P<proto>[a-z0-9]+):(?P<dest>.+):(?P<dport>[0-9]+) \[(?P<inbound>[^\]]+) -> (?P<outbound>[^\]]+)\] email: (?P<tid>[0-9]+)'

# серверный префильтр |= для скорости, если задан литеральный IP/домен
PREFILTER=""
[[ "$IP" != ".+" && "$IP" =~ ^[0-9.]+$ ]] && PREFILTER="$PREFILTER |= \"$IP\""
if [[ "$DOMAIN" != ".+" && ! "$DOMAIN" =~ [\|\(\)\[\]\*\+\?\^\$\\] ]]; then PREFILTER="$PREFILTER |= \"$DOMAIN\""; fi

# в LogQL-строке в двойных кавычках каждый '\' должен быть удвоен ('\['-> '\\[')
PAT_ESC="${PAT//\\/\\\\}"
LOGQL="{service=\"xray\", stream=\"access\", node=~\"$NODE\"}$PREFILTER | regexp \"$PAT_ESC\" | tid=~\"$TID\" | client_ip=~\"$IP\" | dest=~\"$DOMAIN\""

echo "# range : $START .. $END" >&2
echo "# logql : $LOGQL" >&2

HDR="timestamp_utc\tnode\tclient_ip\tt_id\tproto\tdest\tdport\toutbound"
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT

# постраничная выборка: продвигаем start по ns последней строки, пока идёт полная страница
cur="$START"
while :; do
  resp="$(curl -sfG "$LOKI/loki/api/v1/query_range" \
      --data-urlencode "query=$LOGQL" \
      --data-urlencode "start=$cur" \
      --data-urlencode "end=$END" \
      --data-urlencode "limit=$LIMIT" \
      --data-urlencode "direction=forward")" || { echo "loki query failed" >&2; exit 2; }
  n="$(printf '%s' "$resp" | jq '[.data.result[].values[]] | length')"
  [[ "$n" -eq 0 ]] && break
  # Loki уже распарсил строку через | regexp -> берём поля прямо из .stream:
  # ts_ns \t node \t client_ip \t tid \t proto \t dest \t dport \t outbound
  printf '%s' "$resp" | jq -r '.data.result[] as $s | $s.values[] |
    [ .[0], $s.stream.node, $s.stream.client_ip, $s.stream.tid, $s.stream.proto,
      $s.stream.dest, $s.stream.dport, $s.stream.outbound ] | @tsv' >> "$tmp"
  last_ns="$(printf '%s' "$resp" | jq -r '[.data.result[].values[][0] | tonumber] | max')"
  [[ "$n" -lt "$LIMIT" ]] && break
  cur="$((last_ns + 1))"   # ns-эпоха, Loki принимает
done

# сортировка по ts (ns) + перевод ns->UTC средствами bash printf (без gawk/strftime)
export TZ=UTC
CHAIN="$(sort -t$'\t' -k1,1n "$tmp" | while IFS=$'\t' read -r ns node ip tid proto dest dport out; do
  [[ -z "$ns" ]] && continue
  sec="${ns:0:${#ns}-9}"
  printf '%(%Y-%m-%dT%H:%M:%SZ)T\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$sec" "$node" "$ip" "$tid" "$proto" "$dest" "$dport" "$out"
done)"

if [[ -n "$OUT" ]]; then { echo -e "$HDR"; echo "$CHAIN"; } > "$OUT"; echo "# written: $OUT ($(grep -c . <<<"$CHAIN") rows)" >&2; fi
echo -e "$HDR"
echo "$CHAIN"

# сводка + подсказка сопоставления t_id -> аккаунт на панели
UNIQ="$(echo "$CHAIN" | awk -F'\t' 'NF>=4 && $4!=""{print $4}' | sort -un | tr '\n' ' ')"
rows="$(echo "$CHAIN" | grep -c . || true)"
{
  echo ""
  echo "# rows: $rows | unique t_id: $UNIQ"
  echo "# сопоставить t_id -> аккаунт — на СЕРВЕРЕ ПАНЕЛИ (там БД users):"
  echo "#   bash /root/resolve-users.sh $UNIQ"
} >&2
