# Бэкенд: локальный каталог на коллекторе. Забирай на свой ПК скриптом scripts/pull-to-pc.sh.
# В archive.conf:  LOCAL_DIR=/opt/logcollector/archive/store   LOCAL_RETENTION_DAYS=180 (0=не чистить)
# Иммутабельности нет (обычные файлы); при желании смонтируй сюда WORM-ФС.
backend_prepare(){
  : "${LOCAL_DIR:?LOCAL_DIR не задан}"
  mkdir -p "$LOCAL_DIR"
}
backend_put(){ # <local_file> <relpath>
  local file="$1" key="$2" dst
  dst="$LOCAL_DIR/$key"
  mkdir -p "$(dirname "$dst")"
  local tmp
  tmp="$(mktemp "${dst}.XXXXXXXX.part")" || return 1
  if cp -- "$file" "$tmp" && mv -f -- "$tmp" "$dst"; then
    return 0
  fi
  rm -f -- "$tmp"
  return 1
}
backend_retention(){
  local d="${LOCAL_RETENTION_DAYS:-0}"
  [ "$d" -gt 0 ] 2>/dev/null || return 0
  [ -d "$LOCAL_DIR/raw" ] || return 0
  find "$LOCAL_DIR/raw" -type f -mtime "+$d" -delete || return 1
  find "$LOCAL_DIR/raw" -depth -mindepth 1 -type d -empty -delete
}
