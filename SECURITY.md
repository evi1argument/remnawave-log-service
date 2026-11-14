# Security

Please report security issues privately through [GitHub private vulnerability reporting](https://github.com/evi1argument/remnawave-log-service/security/advisories/new). Do not include credentials, VPN logs,
user identifiers, or real infrastructure addresses in public issues.

Run the collector on a dedicated or otherwise trusted Linux host. Keep Grafana
and Loki on loopback, restrict collector TCP/6000 at the network perimeter, and
copy the collector certificate to agents over verified SSH. The IPv4 allow-list
does not authenticate individual agents that share an egress address.

VPN access logs can contain identifying information. Use synthetic data in
support requests, set a retention window appropriate for your deployment,
restrict access to the host, and encrypt cloud archives with age. Keep the age
private key off the collector.
