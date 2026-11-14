#!/usr/bin/env bash
# Run on a disposable Ubuntu VM with Docker and systemd.
set -euo pipefail
cd "$(dirname "$0")/.."
test "$EUID" -eq 0
for path in /opt/logcollector /opt/logship /var/log/remnanode; do
  test ! -e "$path" || { echo "Refusing to use existing $path" >&2; exit 1; }
done

export GF_ADMIN_PASSWORD=user-journey-test-only
export COLLECTOR_ADDRESS="$(hostname -I | awk '{print $1}')"
export COLLECTOR_NODE=- BACKEND=local ENCRYPT=age CADENCE=weekly
age-keygen -o /tmp/log-service-journey.age
export AGE_PUBKEY="$(age-keygen -y /tmp/log-service-journey.age)"
export LOCAL_DIR='/opt/logcollector/test archive' LOCAL_RETENTION_DAYS=10
bash collector/install-collector.sh
curl -fsS --retry 15 --retry-all-errors --retry-delay 2 http://127.0.0.1:3100/ready
curl -fsS --retry 15 --retry-all-errors --retry-delay 2 -u "admin:$GF_ADMIN_PASSWORD" http://127.0.0.1:3000/api/search |
  python3 -c 'import json,sys; assert sum(d.get("type")=="dash-db" for d in json.load(sys.stdin))==2'

mkdir -m 0755 /var/log/remnanode
printf 'synthetic-user-journey-marker\n' > /var/log/remnanode/access.log
touch /var/log/remnanode/error.log
if COLLECTOR_CERT=/tmp/nonexistent bash node/install-node.sh rejected "$COLLECTOR_ADDRESS"; then
  echo 'Installed an agent without a trust certificate' >&2; exit 1
fi
COLLECTOR_CERT=/opt/logcollector/vector/tls/cert.pem bash node/install-node.sh journey "$COLLECTOR_ADDRESS"
AGENT_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' logship-vector)"
bash /opt/logcollector/scripts/collector-add-node-fw.sh "$AGENT_IP"
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
# Stop the synthetic writer before aging its file and exercising the real timer service.
docker stop logcollector-vector >/dev/null
find /opt/logcollector/spool/journey -type f -name '*.log' -exec touch -d '2 hours ago' {} +
systemctl start archive-upload.service
segment="$(find "$LOCAL_DIR" -name '*.age' -print -quit)"
test -n "$segment"
age --decrypt -i /tmp/log-service-journey.age "$segment" | grep -Fx synthetic-user-journey-marker
