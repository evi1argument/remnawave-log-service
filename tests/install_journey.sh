#!/usr/bin/env bash
# Run on a disposable Ubuntu VM with Docker and systemd.
set -euo pipefail
cd "$(dirname "$0")/.."
test "$EUID" -eq 0
for path in /opt/logcollector /opt/logship /var/log/remnanode; do
  test ! -e "$path" || { echo "Refusing to use existing $path" >&2; exit 1; }
done

export COLLECTOR_ADDRESS="$(hostname -I | awk '{print $1}')"
age-keygen -o /tmp/log-service-journey.age
AGE_PUBKEY="$(age-keygen -y /tmp/log-service-journey.age)"
LOCAL_DIR=/opt/logcollector/archive/store
printf '%s\n' "$COLLECTOR_ADDRESS" weekly 10 "$AGE_PUBKEY" n | bash install.sh collector
test ! -e /opt/logcollector
printf '%s\n' "$COLLECTOR_ADDRESS" weekly 10 "$AGE_PUBKEY" y | bash install.sh collector
GF_ADMIN_PASSWORD="$(sed -n 's/^GF_ADMIN_PASSWORD=//p' /opt/logcollector/.env)"
curl -fsS --retry 15 --retry-all-errors --retry-delay 2 http://127.0.0.1:3100/ready
curl -fsS --retry 15 --retry-all-errors --retry-delay 2 -u "admin:$GF_ADMIN_PASSWORD" http://127.0.0.1:3000/api/search |
  python3 -c 'import json,sys; assert sum(d.get("type")=="dash-db" for d in json.load(sys.stdin))==2'

mkdir -m 0755 /var/log/remnanode
touch /var/log/remnanode/{access,error}.log
if COLLECTOR_CERT=/tmp/nonexistent bash node/install-node.sh rejected "$COLLECTOR_ADDRESS"; then
  echo 'Installed an agent without a trust certificate' >&2; exit 1
fi
printf '%s\n' journey "$COLLECTOR_ADDRESS" /opt/logcollector/vector/tls/cert.pem да | bash install.sh --lang ru node
AGENT_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' logship-vector)"
printf '%s\n' "$AGENT_IP" y | bash install.sh allow-node
iptables -C LOGCOLLECTOR-6000 -s "$AGENT_IP/32" -j ACCEPT
iptables -C LOGCOLLECTOR-6000 -j DROP
systemctl is-active --quiet collector-firewall.service archive-upload.timer
# Write the first complete line only after the firewall permits the agent.
# Use an explicit query window and inspect Loki's JSON rather than its raw body.
marker=synthetic-user-journey-marker
start_ns="$(date -d '1 minute ago' +%s%N)"
# Both roles use bridged containers on this VM, so the effective node IP
# can only be known after the agent is created. Start a fresh connection
# after authorizing it, rather than racing the first connect during DROP.
docker restart logship-vector >/dev/null
test "$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' logship-vector)" = "$AGENT_IP"
printf '%s\n' "$marker" >> /var/log/remnanode/access.log
found=false
for attempt in $(seq 1 60); do
  if curl -fsSG --noproxy '*' --max-time 5 -H 'Cache-Control: no-cache' \
    --data-urlencode "start=$start_ns" --data-urlencode "end=$(date +%s%N)" \
    --data-urlencode 'query={service="xray",node="journey",stream="access"}' \
    http://127.0.0.1:3100/loki/api/v1/query_range |
      jq -e --arg marker "$marker" 'any(.data.result[]?.values[]?; .[1] == $marker)' >/dev/null; then
    found=true; break
  fi
  if [ "$attempt" = 5 ] || [ "$attempt" = 15 ]; then
    echo "Checkpoint: query for logs and check distributor intake"
    curl -fsSG --noproxy '*' --max-time 5 --data-urlencode 'query={service="xray"}' \
      http://127.0.0.1:3100/loki/api/v1/query_range | jq -c '{status,streams:.data.result,ingester:.data.stats.ingester}' || true
    curl -fsS --noproxy '*' --max-time 5 http://127.0.0.1:3100/metrics |
      awk '/^loki_distributor_lines_received_total|^loki_discarded_samples_total/' || true
  fi
  sleep 2
done
if ! "$found"; then
  docker logs --tail 40 logship-vector
  docker logs --tail 40 logcollector-vector
  docker logs --tail 30 logcollector-loki
  find /opt/logcollector/spool -type f -name '*.log' -print -exec cat -- {} +
  exit 1
fi
# Exercise the shipped systemd producer against the dedicated collector.
install -m 0644 node/systemd/log-service-canary.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl start log-service-canary.service
canary_file=/var/log/remnanode/log-service-canary.log
canary_line="$(cat "$canary_file")"
[[ "$canary_line" =~ ^log-service-canary\ ([0-9]{10})$ ]]
canary_ns="${BASH_REMATCH[1]}000000000"
canary_start="$(date -d '1 minute ago' +%s%N)"
canary_selector='{service="log-service",node="journey",stream="canary"}'
query_canary() {
  curl -fsSG --noproxy '*' --max-time 5 -H 'Cache-Control: no-cache' \
    --data-urlencode "start=$canary_start" --data-urlencode "end=$(date +%s%N)" \
    --data-urlencode "query=$canary_selector" http://127.0.0.1:3100/loki/api/v1/query_range
}
canary_found=false
for attempt in $(seq 1 30); do
  if query_canary | jq -e --arg line "$canary_line" --arg ns "$canary_ns" \
        'any(.data.result[]?.values[]?; . == [$ns, $line])' >/dev/null; then
    canary_found=true; break
  fi
  sleep 2
done
if ! "$canary_found"; then
  echo 'Control record did not reach Loki with its source timestamp' >&2
  printf 'Expected %s at %s\n' "$canary_line" "$canary_ns" >&2
  query_canary | jq -c '{status,streams:.data.result}' >&2 || true
  docker logs --tail 40 logship-vector
  docker logs --tail 40 logcollector-vector
  exit 1
fi
# Close both producers before checking the complete spool and exercising the archive.
for container in logship-vector logcollector-vector; do
  docker stop --time 70 "$container" >/dev/null
  test "$(docker inspect -f '{{.State.ExitCode}}' "$container")" -eq 0
done
if grep -Rqs '^log-service-canary ' /opt/logcollector/spool; then
  echo 'Control record reached the raw archive queue' >&2
  exit 1
fi
find /opt/logcollector/spool/journey -type f -name '*.log' -exec touch -d '2 hours ago' {} +
systemctl start archive-upload.service
segment="$(find "$LOCAL_DIR" -name '*.age' -print -quit)"
test -n "$segment"
age --decrypt -i /tmp/log-service-journey.age "$segment" | grep -Fx synthetic-user-journey-marker
