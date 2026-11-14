#!/usr/bin/env bash
# Claim settled segments before uploading; never unlink a live spool pathname.
set -euo pipefail
umask 077
BASE="${LOGCOLLECTOR_BASE:-/opt/logcollector}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="${ARCHIVE_CONF:-$BASE/archive/archive.conf}"
[ -f "$CONF" ] || { echo "archive.conf not found: $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONF"
: "${BACKEND:?BACKEND is required}"
case "$BACKEND" in s3|gdrive|local) ;; *) echo "Unknown BACKEND" >&2; exit 1;; esac
ENCRYPT="${ENCRYPT:-age}"
case "$ENCRYPT" in age|none) ;; *) echo "ENCRYPT must be age or none" >&2; exit 1;; esac
SETTLED_MIN="${SETTLED_MIN:-65}"
[[ "$SETTLED_MIN" =~ ^[0-9]+$ ]] || { echo "Invalid SETTLED_MIN" >&2; exit 1; }
SPOOL="$BASE/spool"
[ -d "$SPOOL" ] || { echo "Missing spool: $SPOOL" >&2; exit 1; }
# Run on the Linux host as root: fuser must see writers in container processes.
[ "$EUID" -eq 0 ] || { echo "Run on the collector host as root" >&2; exit 1; }
cap_eff="$(awk '/^CapEff:/ {print $2}' /proc/self/status)"
(( (16#$cap_eff & 0x80000) != 0 )) || {
  echo "CAP_SYS_PTRACE required to inspect all writer handles; run on the host" >&2; exit 1;
}
profile="$(cat /proc/self/attr/current 2>/dev/null || true)"
case "$profile" in
  ''|unconfined*) ;;
  *) echo "Confined process cannot reliably inspect writers; run on the host" >&2; exit 1;;
esac
command -v fuser >/dev/null || { echo "Install psmisc (fuser)" >&2; exit 1; }
exec 9>"$BASE/archive-upload.lock"
flock -n 9 || { echo "archive-upload already running" >&2; exit 1; }
PENDING="$SPOOL/.archive-pending"
mkdir -p "$PENDING"
WORK="$(mktemp -d "$BASE/.archive-work.XXXXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# shellcheck disable=SC1090
source "$HERE/backends/$BACKEND.sh"
backend_prepare
if [ "$ENCRYPT" = age ]; then
  command -v age >/dev/null || { echo "Install age" >&2; exit 1; }
  PUB="$(cat "${AGE_PUBKEY_FILE:-$BASE/secrets/archive.pub}")"
  [ -n "$PUB" ] || { echo "Empty age public key" >&2; exit 1; }
fi

# Materialize find output: errors must fail the job, not look like an empty spool.
find "$SPOOL" -path "$PENDING" -prune -o -type f -name '*.log' \
  -mmin "+$SETTLED_MIN" -print0 > "$WORK/candidates"
batch=""
while IFS= read -r -d '' f; do
  [ -n "$batch" ] || batch="$(mktemp -d "$PENDING/segment.XXXXXXXX")"
  rel="${f#"$SPOOL"/}"
  mkdir -p "$(dirname "$batch/$rel")"
  # Atomic rename: future opens of the original path create a separate segment.
  mv -- "$f" "$batch/$rel"
done < "$WORK/candidates"

find "$PENDING" -type f -name '*.log' -print0 > "$WORK/pending"
n=0; failed=0; deferred=0
while IFS= read -r -d '' f; do
  if fuser -s "$f" 2>"$WORK/fuser-error"; then
    deferred=$((deferred+1)); continue
  else
    rc=$?
    if [ -s "$WORK/fuser-error" ]; then
      cat "$WORK/fuser-error" >&2; exit 1
    fi
    [ "$rc" -eq 1 ] || { echo "fuser failed: $f" >&2; exit 1; }
  fi
  # No open handle and a private pathname: Vector can no longer change this inode.
  rel="${f#"$PENDING"/}"; segment="${rel%%/*}"; rel="${rel#*/}"
  digest="$(sha256sum < "$f")"; digest="${digest%% *}"
  relout="raw/${rel%.log}.$segment.$digest.log"
  art="$f"
  if [ "$ENCRYPT" = age ]; then
    art="$WORK/segment.age"
    rm -f -- "$art"
    if ! age -r "$PUB" -o "$art" "$f"; then
      echo "Encryption FAILED: $rel" >&2
      failed=$((failed+1)); continue
    fi
    relout="$relout.age"
  fi
  if backend_put "$art" "$relout"; then
    rm -- "$f"
    echo "archived $relout"; n=$((n+1))
  else
    echo "Upload FAILED: $rel" >&2; failed=$((failed+1))
  fi
done < "$WORK/pending"
find "$PENDING" -depth -mindepth 1 -type d -empty -delete
backend_retention || { echo "Retention FAILED" >&2; failed=$((failed+1)); }
echo "archive-upload [$BACKEND]: $n archived, $failed failed, $deferred open segments deferred"
[ "$failed" -eq 0 ]
