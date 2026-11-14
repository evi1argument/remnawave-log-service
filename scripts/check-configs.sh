#!/usr/bin/env bash
# Validate templates using pinned images, isolated data, and no published ports.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
mkdir -p "$WORK/tls" "$WORK/data" "$WORK/spool" "$WORK/logs"
bash "$REPO/scripts/gen-tls.sh" "$WORK/tls" >/dev/null 2>&1
chmod 755 "$WORK" "$WORK/tls" "$WORK/data" "$WORK/spool" "$WORK/logs"
# Temporary test certificate only, never the production private key.
chmod 644 "$WORK/tls/key.pem"
cp "$WORK/tls/cert.pem" "$WORK/tls/collector.crt"
export GF_ADMIN_PASSWORD=configuration-check-only
docker compose -f "$REPO/collector/docker-compose.yml" config --quiet
docker compose -f "$REPO/node/docker-compose.yml" config --quiet
VECTOR_IMAGE="$(docker compose -f "$REPO/node/docker-compose.yml" config --images)"
LOKI_IMAGE="$(docker compose -f "$REPO/collector/docker-compose.yml" config --format json | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["loki"]["image"])')"
for config in "$REPO/node/vector/vector.agent.yaml" "$REPO/collector/vector/vector.aggregator.yaml"; do
  docker run --rm --network none --user 0 \
    -v "$config:/etc/vector/vector.yaml:ro" -v "$WORK/tls:/tls:ro" \
    -v "$WORK/data:/vector-data" -v "$WORK/spool:/spool" \
    -v "$WORK/logs:/self-logs:ro" -v "$WORK/logs:/var/log/remnanode:ro" \
    "$VECTOR_IMAGE" validate --skip-healthchecks /etc/vector/vector.yaml
done
# Exercise the installer's dedicated-collector rendering, too.
sed '/^  # локальные/,/^transforms:/{ /^transforms:/!d; }; /^  local_meta:/,/^  checked_nodes:/{ /^  checked_nodes:/!d; }' \
  "$REPO/collector/vector/vector.aggregator.yaml" | \
  sed 's/inputs: \[checked_nodes, local_meta\]/inputs: [checked_nodes]/g' > "$WORK/dedicated.yaml"
docker run --rm --network none --user 0 \
  -v "$WORK/dedicated.yaml:/etc/vector/vector.yaml:ro" -v "$WORK/tls:/tls:ro" \
  -v "$WORK/data:/vector-data" -v "$WORK/spool:/spool" \
  "$VECTOR_IMAGE" validate --skip-healthchecks /etc/vector/vector.yaml
docker run --rm --network none \
  -v "$REPO/collector/loki/loki-config.yaml:/etc/loki/config.yaml:ro" \
  "$LOKI_IMAGE" -config.file=/etc/loki/config.yaml -verify-config=true
python3 - "$REPO" <<'PY'
import json, pathlib, sys
for path in pathlib.Path(sys.argv[1], 'collector/grafana').rglob('*.json'):
    json.loads(path.read_text())
print('Grafana dashboard JSON: OK')
PY
