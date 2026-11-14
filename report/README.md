# Отчёты «IP / юзер / домен за период → цепочка»

Две фазы, чтобы сопоставление `t_id → аккаунт` не тащить на коллектор: в Loki и в архиве хранится только
числовой `t_id` (из xray access `email: N`), а связка `t_id → аккаунт` живёт в БД панели.

## Фаза 1 — собрать цепочку (на КОЛЛЕКТОРЕ)
`investigate.sh` → `/opt/logcollector/report/investigate.sh`
```bash
investigate.sh --from <t> [--to <t>] [--user <t_id>] [--ip <addr>] [--domain <re>] [--node <re>] [--out chain.tsv]
```
`--from/--to`: RFC3339 (`2026-01-10T00:00:00Z`) или относительно (`-24h`, `-7d`, `now`).
Вывод — TSV: `timestamp_utc  node  client_ip  t_id  proto  dest  dport  outbound`, снизу список уникальных `t_id`
и готовая команда сопоставления.

Примеры:
```bash
investigate.sh --from -24h --ip 203.0.113.7
investigate.sh --from 2026-01-10T00:00:00Z --to 2026-01-11T00:00:00Z --user 42 --out u42.tsv
investigate.sh --from -7d --domain 'example\.com' --node node1
```
Заметь: hot-поиск через Loki покрывает `retention_period` (по умолчанию 30 дней). Более старые периоды —
из архива (расшифровать `.age` приватным ключом, затем те же фильтры).

## Фаза 2 — сопоставить t_id → аккаунт (на ПАНЕЛИ)
`resolve-users.sh` → `/root/resolve-users.sh` (ставится на сервер панели)
```bash
bash /root/resolve-users.sh <t_id> [t_id ...]
```
Отдаёт `id → username, telegram_id, email, description, status, expires` из БД панели.
Под свою инсталляцию переопредели контейнер/креды БД: `PANEL_DB_CONTAINER`, `PG_USER`, `PG_DB`.

> Соответствие: xray в access-логе пишет `email: N`, где `N` — числовой PK пользователя панели.
> **Имя колонки зависит от версии панели:** до 3.0.0 это `users.t_id`, начиная с 3.0.0 —
> `users.id` (миграция `20260720124815_rename_column`; заодно там же удалён `users.uuid`).
> Значения при переименовании сохранились, так что старые `t_id` из архивных логов остаются
> валидными. Скрипт определяет имя колонки сам через `information_schema` и работает на обеих
> версиях — правки при апгрейде не требуются.
