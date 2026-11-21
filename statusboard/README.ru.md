# Remnawave Statusboard

[English](README.md) · **Русский**

Опциональный мониторинг доступности Remnawave. Публикует JSON для страницы статуса.

| Источник | Что измеряет |
| --- | --- |
| `control_plane` | Связь включённых нод с локальной панелью Remnawave, по странам |
| `vpn_e2e` | Сквозной HTTPS-запрос через выделенный VPN-клиент с проверкой выходного IP |
| Beszel | Метрики хостов и сетевая диагностика для сопоставления с историей статуса |

Сборщик сохраняет замер в SQLite и атомарно заменяет JSON. Публичная история содержит 96 закрытых интервалов по 15 минут. Процент считается как `up / (up + down + degraded)`. Неизвестные интервалы и плановое обслуживание публикуются отдельно и исключаются из знаменателя. Без замеров возвращается `null`. Для окна обслуживания задайте границы в виде UTC epoch с шагом 15 минут.

## Установка сборщика на Linux

Нужны Python 3.10+, systemd и локальный API Remnawave для режима панели. Ручные команды выполняются из каталога `statusboard`. В примере первый снимок записывается в приватный каталог.

Для установки через мастер выполните `sudo bash install.sh statusboard` в корне репозитория. Мастер проверит доступ к локальному API и обнаружит включённые локации перед настройкой таймера.

```bash
sudo install -d -m 0755 /opt/remnawave-statusboard-v2
sudo install -m 0644 adapter.py statusboard.py probe.py check_public.py /opt/remnawave-statusboard-v2/
sudo test -e /etc/remnawave-statusboard-v2.json || sudo install -m 0600 config.example.json /etc/remnawave-statusboard-v2.json
sudo test -e /etc/remnawave-statusboard.env || sudo install -m 0600 /dev/null /etc/remnawave-statusboard.env
sudoedit /etc/remnawave-statusboard-v2.json /etc/remnawave-statusboard.env
```

Заполните `panel_url` и `locations` в JSON. Панель должна быть доступна по loopback на сервере сборщика. В закрытом env-файле задайте `REMNAWAVE_API_TOKEN`.

```bash
sudo install -m 0644 systemd/remnawave-status-collect.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start remnawave-status-collect.service
sudo systemctl enable --now remnawave-status-collect.timer
sudo systemctl status remnawave-status-collect.timer
```

Проверьте приватный снимок по пути `public_json`. Затем задайте путь к файлу в корне сайта и перезапустите сборщик. Для страницы доступны `locations`, `history`, `uptime`, `coverage`, `source`, `generatedAt` и `observedAt`. Текущие замеры устаревают через 20 минут. Проверьте публичный JSON с другой машины командой `python3 check_public.py https://status.example.net/status.json`.

При переносе для каждого публичного JSON оставьте одну службу записи.

## Сквозные VPN-проверки

На сервере пробника настройте выделенный VPN-клиент с отдельным SOCKS-портом на loopback для каждого маршрута. Установите `probe.py` и `statusboard.py` в `/opt/remnawave-statusboard-v2` и проверьте наличие curl. На основе `probe.example.json` задайте контрольный HTTPS-адрес, возвращающий строку вида `ip=192.0.2.10`, и ожидаемые выходные IP для каждого маршрута. Установите конфигурацию в `/etc/remnawave-status-probe.json` и включите `remnawave-status-probe.timer` с одноимённой службой.

Если пробник и сборщик на разных серверах, переносите кеш по закрытому каналу и заменяйте файл на сборщике атомарно. Задайте `source: vpn_e2e`, путь к кешу и ID маршрутов для каждой страны. Замеры пробника устаревают через 180 секунд. Перед публикацией проверьте успех, недоступность SOCKS, неожиданный выходной IP и недоступность контрольной цели.

Из корня репозитория запустите `python3 -B -m unittest discover -s statusboard -v`.

Источники: [расчёт Uptime Kuma](https://github.com/louislam/uptime-kuma/blob/2.5.12/server/uptime-calculator.js), [сетевые проверки Beszel](https://beszel.dev/guide/network-monitors), [Prometheus Blackbox Exporter](https://github.com/prometheus/blackbox_exporter) и [настройки curl](https://curl.se/docs/manpage.html).
