#!/usr/bin/env bash
# Генерит self-signed TLS для приёмника Vector на коллекторе (порт 6000).
# Ограничение отправителей — DOCKER-USER; оно не заменяет проверку сертификата сервера.
set -euo pipefail
OUT="${1:-/opt/logcollector/vector/tls}"
mkdir -p "$OUT"
if [ -s "$OUT/key.pem" ] && [ -s "$OUT/cert.pem" ]; then
  echo "TLS: preserving existing certificate in $OUT"
  exit 0
fi
if [ -e "$OUT/key.pem" ] || [ -e "$OUT/cert.pem" ]; then
  echo "Incomplete TLS pair in $OUT; refusing to replace it" >&2
  exit 1
fi
umask 077
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$OUT/key.pem" -out "$OUT/cert.pem" \
  -days 825 -subj "/CN=logcollector" -addext "subjectAltName=DNS:logcollector"
chmod 600 "$OUT/key.pem"; chmod 644 "$OUT/cert.pem"
echo "TLS: $OUT/cert.pem  $OUT/key.pem"
