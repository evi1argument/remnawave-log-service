#!/bin/sh
# Forced command for a dedicated SSH key. The receiver uses its own clock.
set -eu
umask 077
inbox=/var/lib/log-service-heartbeat/inbox
tmp=$(mktemp "$inbox/.received.XXXXXXXX")
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
date +%s >"$tmp"
mv -fT -- "$tmp" "$inbox/received"
