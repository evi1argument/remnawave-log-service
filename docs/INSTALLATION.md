# Установка и настройка

Команды ниже устанавливают **v1.2.0**. Для действующего стека используйте [порядок обновления](UPGRADING.md).

Предпосылки: Debian/Ubuntu с systemd, root, IPv4 и [Docker Engine 28+ с плагином Compose](https://docs.docker.com/engine/install/).
На коллекторе нужен Docker с iptables backend. До установки ограничьте входящий TCP/6000
известными адресами нод на сетевом firewall провайдера или используйте закрытую сеть.
Это защищает порт и во время старта Docker, до применения правил systemd.

### 1. Коллектор
```bash
git clone --branch v1.2.0 https://github.com/evi1argument/remnawave-log-service.git
cd remnawave-log-service/collector
./install-collector.sh          # root; спросит: бэкенд (s3/gdrive/local), шифрование, каденцию, параметры
# для S3 с защитой архива от удаления:  APPLY_COMPLIANCE=yes ./install-collector.sh
```
> Скрипты в git с exec-битом, `./` работает; для ZIP-скачки — `bash install-collector.sh`.
Инсталлятор ставит Docker/Loki/Grafana/Vector, TLS, пишет `archive.conf`, поднимает systemd-таймер
(`hourly|daily|weekly`) и отчёт фазы 1.

Подготовка бэкенда:
- **s3** — создай бакет заранее; для защиты от удаления включи при создании **Object Lock + Versioning**
  и заведи **отдельный сервисный ключ** (не мастер-ключ аккаунта). Режим блокировки ставит `APPLY_COMPLIANCE=yes`.
- **gdrive** — `rclone config` (тип drive) → укажи `remote:path`.
- **local** — просто каталог; забирай на ПК `scripts/pull-to-pc.sh root@<коллектор>`.

### 2. Каждая нода
Укажите адрес коллектора, доступный с ноды. Инсталлятор предлагает первый адрес
хоста; если используется NAT или DNS, задайте `COLLECTOR_ADDRESS` при установке.
На ноде скопируйте только `cert.pem` по SSH с проверенным ключом хоста:
```bash
git clone --branch v1.2.0 https://github.com/evi1argument/remnawave-log-service.git
cd remnawave-log-service/node
scp root@COLLECTOR:/opt/logcollector/vector/tls/cert.pem ./collector.crt
COLLECTOR_CERT=./collector.crt COLLECTOR_IP=COLLECTOR ./install-node.sh NODE_NAME
```
На коллекторе разрешите IPv4, с которого ожидаете соединение. За NAT это
внешний адрес ноды, а первый локальный IP из подсказки установщика может отличаться:
```bash
/opt/logcollector/scripts/collector-add-node-fw.sh <IP_ноды>   # готовая — из вывода install-node
```
Предпосылка на ноде: xray пишет в файл — mount `/var/log/remnanode` в compose ноды, в профиле ноды в панели
блок `log` с путями `access`/`error`, и logrotate `copytruncate` на access.log (пример — `node/logrotate-remnanode`).

### 3. Панель (сопоставление t_id → аккаунт)
```bash
scp report/resolve-users.sh root@<PANEL>:/root/ && ssh root@<PANEL> chmod +x /root/resolve-users.sh
```

## Дашборды и отчёты
- Grafana: `ssh -L 3000:127.0.0.1:3000 root@<COLLECTOR>` → http://127.0.0.1:3000. Папка **Remnawave Logs**.
- CLI-отчёт: [описание отчётов](../report/README.md).

## Настройка после установки

- [Контроль доставки логов](LOG-DELIVERY.md) проверяет прохождение записи с каждой включённой ноды через Vector до Loki. Включите таймер и проверьте появление метки ноды перед подключением внешнего мониторинга.
- [Telegram в Grafana](TELEGRAM.md) подключается отдельно для всплесков строк в `error.log`. Токен и чат задаются в Grafana; встроенная настройка подходит для любого служебного бота и чата.

Настройки в `/opt/logcollector/archive/archive.conf` (бэкенд, шифрование, retention) и в юните таймера
(`/etc/systemd/system/archive-upload.timer`, каденция). Прогнать вручную:
`systemctl start archive-upload.service`.

| Что | По умолчанию | Где |
|---|---|---|
| Loki hot-retention | 30 дней | `collector/loki/loki-config.yaml` |
| Каденция архива | weekly (Вс 04:00 UTC) | `archive-upload.timer` |
| Шифрование | age | `archive.conf` (`ENCRYPT`) |
| Бэкенд | s3 | `archive.conf` (`BACKEND`) |
| «Settled» порог | >65 мин | `archive.conf` (`SETTLED_MIN`) |
| max_query_series | 20000 | `collector/loki/loki-config.yaml` |

> Реже заливка = меньше обращений к бэкенду, но больше окно потери при гибели диска коллектора до заливки
> (данные до этого живут в Loki+spool). Плотнее окно — `CADENCE=hourly/daily`.

## Приватность и доступ

Access-логи VPN чувствительны (видно, какой пользователь на какой домен ходил). Рекомендации:
**`ENCRYPT=age`** (на сервере только public key, приватный офлайн у тебя) — обязательно для gdrive/облаков;
в Loki и в архиве хранится только числовой `t_id`, а сопоставление `t_id → аккаунт` делается отдельно на
панели и на коллекторе не остаётся.

⚠️ **Приёмник 6000 фильтруется в цепочке `DOCKER-USER`, а НЕ через ufw.** Docker публикует порты в обход ufw
(DNAT минует `ufw-user-input`), поэтому `ufw allow` на 6000 бесполезен. Инсталлятор ставит default-deny
(`collector-firewall-init.sh` + systemd-юнит), доступ по нодам открывается `collector-add-node-fw.sh <IP>`.
Без этого 6000 открыт всему интернету — любой сможет слать поддельные строки в Loki и архив. Для сохранения
правил после ребута: `apt-get install -y iptables-persistent`.

Агент проверяет сертификат и TLS-имя `logcollector`. Передавайте сертификат по
доверенному каналу и обновляйте копии на нодах до окончания его срока действия.
Доступ отправителей ограничивает IPv4 allow-list; отдельной аутентификации каждой
ноды нет. Подробнее — [обновление и ограничения](UPGRADING.md).
