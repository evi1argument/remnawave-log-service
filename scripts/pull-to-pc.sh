#!/usr/bin/env bash
# Запускать НА СВОЁМ ПК (Linux/macOS/WSL/Git-Bash). Тянет архив с коллектора (BACKEND=local) к себе.
# Использование: ./pull-to-pc.sh root@<COLLECTOR_HOST> [dest_dir] [remote_store]
set -euo pipefail
COLLECTOR="${1:?Usage: $0 root@collector-host [dest_dir] [remote_store]}"
DEST="${2:-./remnawave-archive}"
STORE="${3:-/opt/logcollector/archive/store/}"
mkdir -p "$DEST"
if command -v rsync >/dev/null; then
  rsync -avz --ignore-existing "$COLLECTOR:$STORE" "$DEST/"
else
  echo "rsync нет — использую scp (Windows: ставь через 'winget install rsync' или запусти в WSL)"
  scp -r "$COLLECTOR:$STORE"* "$DEST/"
fi
echo "готово -> $DEST"
echo "расшифровать:  age -d -i <твой_приватный_ключ> archive.age > archive.log   (если ENCRYPT=age)"
