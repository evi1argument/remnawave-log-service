#!/usr/bin/env bash
# resolve-users.sh — фаза 2 отчёта: сопоставление числового id (xray 'email: N') -> аккаунт.
# Запускать на сервере панели. Читает users из БД панели Remnawave.
# Под свою инсталляцию переопредели (если контейнер/креды БД другие):
#   PANEL_DB_CONTAINER (по умолч. remnawave-db), PG_USER (postgres), PG_DB (postgres)
#
# Панель 3.0.0 переименовала users.t_id -> users.id (миграция 20260720124815_rename_column)
# и удалила users.uuid. Имя колонки определяем на лету, чтобы скрипт работал и на 2.x, и на 3.x.
set -euo pipefail
DBC="${PANEL_DB_CONTAINER:-remnawave-db}"
PGU="${PG_USER:-postgres}"
PGD="${PG_DB:-postgres}"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <id> [id ...]   |   echo '10 42 ...' | $0 -" >&2
  exit 1
fi

if [[ "$1" == "-" ]]; then IDS="$(cat | tr ' \t\n' ',,,' )"; else IDS="$(printf '%s,' "$@")"; fi
IDS="$(echo "$IDS" | sed -E 's/[^0-9,]//g; s/,+/,/g; s/^,//; s/,$//')"
[[ -z "$IDS" ]] && { echo "no numeric id given" >&2; exit 1; }

# 3.x -> 'id', 2.x -> 't_id'
IDCOL="$(docker exec -i "$DBC" psql -U "$PGU" -d "$PGD" -tAc \
  "SELECT column_name FROM information_schema.columns
    WHERE table_name='users' AND column_name IN ('t_id','id')
    ORDER BY (column_name='t_id') DESC LIMIT 1;" | tr -d '[:space:]')"
[[ -z "$IDCOL" ]] && { echo "не нашёл колонку id/t_id в таблице users" >&2; exit 1; }

SQL="SELECT $IDCOL, username, telegram_id, email, description, status,
            to_char(expire_at,'YYYY-MM-DD') AS expires
       FROM users WHERE $IDCOL IN ($IDS) ORDER BY $IDCOL;"

docker exec -i "$DBC" psql -U "$PGU" -d "$PGD" -P pager=off -c "$SQL"
