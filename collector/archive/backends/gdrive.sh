# Бэкенд: Google Drive (или любой облак rclone) через rclone.
# Настрой remote заранее:  rclone config   (тип drive) -> получишь имя, напр. 'gdrive'.
# В archive.conf:  RCLONE_REMOTE=gdrive:remnawave-logs   (remote:путь)
# ВНИМАНИЕ: SaaS-облако видит объём/имена; для чувствительных VPN-логов держи ENCRYPT=age.
# Защиты от удаления (WORM) у Drive нет — это удобный бэкап, но не неизменяемый архив.
backend_prepare(){
  : "${RCLONE_REMOTE:?RCLONE_REMOTE не задан}"
  command -v rclone >/dev/null || { echo "rclone не установлен (apt-get install -y rclone)"; return 1; }
  local remote="${RCLONE_REMOTE%%:*}"
  rclone listremotes 2>/dev/null | grep -qx "${remote}:" || { echo "rclone remote '$remote:' не настроен (rclone config)"; return 1; }
}
backend_put(){ # <local_file> <relpath>
  local file="$1" key="$2"
  rclone rcat "${RCLONE_REMOTE}/${key}" < "$file" 2>/dev/null
}
backend_retention(){ : ; }   # при желании: rclone delete --min-age <N>d "$RCLONE_REMOTE"
