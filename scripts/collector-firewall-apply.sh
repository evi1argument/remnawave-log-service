#!/usr/bin/env bash
# Apply the receiver allow-list as one atomic IPv4 filter table update.
set -euo pipefail
PORT="${PORT:-6000}"
LIST="${ALLOWED_NODES:-/opt/logcollector/allowed-nodes.txt}"
[[ "$PORT" =~ ^[1-9][0-9]{0,4}$ ]] && (( PORT <= 65535 )) || { echo "Invalid port" >&2; exit 1; }
CHAIN="LOGCOLLECTOR-$PORT"
exec 9>"/run/lock/logcollector-firewall-$PORT.lock"
flock 9
for _ in $(seq 1 30); do iptables -w -S DOCKER-USER >/dev/null 2>&1 && break; sleep 1; done

rules=()
if [ -f "$LIST" ]; then
  while IFS= read -r ip || [ -n "$ip" ]; do
    ip="${ip%%#*}"
    ip="${ip//[[:space:]]/}"
    [ -n "$ip" ] || continue
    if ! [[ "$ip" =~ ^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$ ]] ||
       (( BASH_REMATCH[1] > 255 || BASH_REMATCH[2] > 255 || BASH_REMATCH[3] > 255 || BASH_REMATCH[4] > 255 )); then
      echo "Expected an IPv4 address in $LIST" >&2
      exit 1
    fi
    rules+=("-A $CHAIN -s $ip/32 -j ACCEPT")
  done < "$LIST"
fi

jump="-p tcp --dport $PORT -j $CHAIN"
{
  printf '*filter\n:%s - [0:0]\n-F %s\n' "$CHAIN" "$CHAIN"
  if ! iptables -w -C DOCKER-USER -p tcp --dport "$PORT" -j "$CHAIN" 2>/dev/null; then
    printf '%s\n' "-I DOCKER-USER 1 $jump"
  fi
  if [ "${#rules[@]}" -gt 0 ]; then printf '%s\n' "${rules[@]}"; fi
  printf '%s\n' "-A $CHAIN -j DROP" COMMIT
} | iptables-restore --wait --noflush

if command -v netfilter-persistent >/dev/null; then
  netfilter-persistent save >/dev/null
fi
echo "firewall: port $PORT — allow ${#rules[@]} node(s), default DROP"
