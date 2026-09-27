#!/bin/sh
# One-time root setup for the second machine's conflict-file uploader.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this installer as root on the CC machine.' >&2
    exit 1
fi

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE="$SOURCE_DIR/sync-private-continuity.py"
TARGET=/home/node/grey-ws/-/scripts/sync-private-continuity.py
TOKEN_FILE=/home/node/grey-ws/.ob-daily-note-token
URL=https://zhizhi.zeabur.app/internal/private-continuity/conflict
LOG=/home/node/grey-ws/logs/private-continuity-sync.log
CRON=/etc/cron.d/ombre-private-continuity-sync

if [ ! -s "$TOKEN_FILE" ]; then
    echo 'The root-owned OB private continuity token is missing.' >&2
    exit 1
fi

# Verify the dedicated credential before installing the repeating job.
PRIVATE_SYNC_TOKEN_FILE="$TOKEN_FILE" PRIVATE_SYNC_URL="$URL" /usr/bin/python3 - <<'PY'
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

token = Path(os.environ["PRIVATE_SYNC_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
request = Request(os.environ["PRIVATE_SYNC_URL"], headers={"Authorization": "Bearer " + token})
try:
    with urlopen(request, timeout=8) as response:
        state = json.load(response)
        if not isinstance(state, dict) or state.get("enabled") is not True:
            raise SystemExit("OB private continuity is unavailable")
except HTTPError as exc:
    raise SystemExit(f"OB private continuity authentication failed: HTTP {exc.code}") from None
except URLError:
    raise SystemExit("OB private continuity is unreachable") from None
print("OB private continuity authentication OK")
PY

install -o root -g root -m 700 "$SOURCE" "$TARGET"
touch "$LOG"
chmod 600 "$LOG"

# Upload an existing marker first. A mismatch with an OB Dashboard edit stops
# installation so the timer cannot repeatedly overwrite it.
/usr/bin/python3 "$TARGET" --url "$URL" --if-changed >> "$LOG" 2>&1 || {
    echo "Initial sync failed; see $LOG. Timer was not installed." >&2
    exit 1
}

TEMP=$(mktemp /etc/cron.d/.ombre-private-continuity-sync.XXXXXX)
trap 'rm -f "$TEMP"' EXIT HUP INT TERM
cat > "$TEMP" <<EOF
SHELL=/bin/sh
PATH=/usr/bin:/bin
* * * * * root umask 077; /usr/bin/python3 $TARGET --url $URL --if-changed >> $LOG 2>&1
EOF
chmod 644 "$TEMP"
chown root:root "$TEMP"
mv "$TEMP" "$CRON"
trap - EXIT HUP INT TERM
echo 'Private continuity sync installed: checks local file once per minute.'
