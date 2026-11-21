# Remnawave Statusboard

**English** · [Русский](README.ru.md)

An optional availability monitor for Remnawave. It publishes JSON for a status page.

| Source | What it measures |
| --- | --- |
| `control_plane` | Whether enabled nodes report connected to the local Remnawave panel, grouped by country |
| `vpn_e2e` | Whether an HTTPS request through a dedicated VPN client exits at the expected IP address |
| Beszel | Host metrics and network diagnostics that can be correlated with the status history |

The collector writes an observation to SQLite and atomically replaces the JSON file. The public history contains 96 closed 15-minute intervals. The percentage is `up / (up + down + degraded)`. Unknown intervals and planned maintenance are excluded from the denominator and reported separately. Without measurements, uptime is `null`. Enter maintenance windows as UTC epoch timestamps aligned to 15-minute boundaries.

## Set up the collector on Linux

Requirements include Python 3.10+, systemd, and a local Remnawave API for panel mode. Run these commands from the `statusboard` directory. The example writes its first snapshot to a private directory.

For guided setup, run `sudo bash install.sh statusboard` from the repository root. The installer validates access to the local API and detects enabled locations before setting up the timer.

```bash
sudo install -d -m 0755 /opt/remnawave-statusboard-v2
sudo install -m 0644 adapter.py statusboard.py probe.py check_public.py /opt/remnawave-statusboard-v2/
sudo test -e /etc/remnawave-statusboard-v2.json || sudo install -m 0600 config.example.json /etc/remnawave-statusboard-v2.json
sudo test -e /etc/remnawave-statusboard.env || sudo install -m 0600 /dev/null /etc/remnawave-statusboard.env
sudoedit /etc/remnawave-statusboard-v2.json /etc/remnawave-statusboard.env
```

Fill in `panel_url` and `locations` in the JSON file. The panel URL must be reachable over loopback on the collector host. Set `REMNAWAVE_API_TOKEN` in the private environment file.

```bash
sudo install -m 0644 systemd/remnawave-status-collect.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start remnawave-status-collect.service
sudo systemctl enable --now remnawave-status-collect.timer
sudo systemctl status remnawave-status-collect.timer
```

Check the private snapshot at `public_json`. Then set that path to the status file in your web root and restart the collector. Use `locations`, `history`, `uptime`, `coverage`, `source`, `generatedAt`, and `observedAt` to build a status page. Current observations expire after 20 minutes. Check the published JSON from another host with `python3 check_public.py https://status.example.net/status.json`.

During a migration, keep a single writer for each public JSON file.

## End-to-end VPN checks

On the probe host, configure a dedicated VPN client with one loopback SOCKS listener per monitored route. Install `probe.py` and `statusboard.py` in `/opt/remnawave-statusboard-v2` and make sure curl is available. Based on `probe.example.json`, configure an HTTPS verifier that returns a line such as `ip=192.0.2.10`, and list each route's expected egress IPs. Install the config as `/etc/remnawave-status-probe.json` and enable `remnawave-status-probe.timer` with its matching service.

If the probe and collector run on different hosts, transfer the cache through a private channel and atomically replace it on the collector. Set the collector's source to `vpn_e2e`, configure the cache path, and map each country to its expected route IDs. Probe observations expire after 180 seconds. Verify success, an unavailable SOCKS listener, an unexpected egress IP, and an unreachable verifier before publishing the source.

Run `python3 -B -m unittest discover -s statusboard -v` from the repository root.

References include the [Uptime Kuma calculator](https://github.com/louislam/uptime-kuma/blob/2.5.12/server/uptime-calculator.js), [Beszel network monitoring](https://beszel.dev/guide/network-monitors), [Prometheus Blackbox Exporter](https://github.com/prometheus/blackbox_exporter), and [curl options](https://curl.se/docs/manpage.html).
