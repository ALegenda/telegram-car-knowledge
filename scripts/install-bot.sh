#!/usr/bin/env bash
# Invoked over SSH by deploy-bot.sh; all paths are for this separate service only.
set -euo pipefail
release_id=${1:?release SHA required}
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ $(id -u) == 0 ]] || { echo 'Installer requires root.' >&2; exit 2; }
base=/opt/tg-knowledge-bot
release="$base/releases/$release_id"
config=/etc/tg-knowledge-bot/bot.env
previous=$(readlink -f "$base/current" 2>/dev/null || true)
archive="$base/uploads/$release_id.tar.gz"
incoming="$base/uploads/$release_id.env"

python3 - <<'PY'
import sys
if not (3, 11) <= sys.version_info[:2] < (3, 14):
    raise SystemExit('Python 3.11–3.13 is required; system Python was not modified.')
PY
if ! id tg-knowledge-bot >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/tg-knowledge-bot --shell /usr/sbin/nologin tg-knowledge-bot
fi
install -d -m 0755 "$base" "$base/releases"
install -d -m 0700 -o tg-knowledge-bot -g tg-knowledge-bot /var/lib/tg-knowledge-bot
install -d -m 0750 -g tg-knowledge-bot /etc/tg-knowledge-bot
if [[ ! -d "$release" ]]; then
    install -d -m 0755 "$release"
    tar -xzf "$archive" -C "$release"
    chmod -R a+rX "$release"
fi
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip --disable-pip-version-check install --require-hashes -r "$release/deploy/requirements-bot.txt" >/dev/null
"$release/.venv/bin/python" -m pip --disable-pip-version-check install --no-deps "$release" >/dev/null

# Validate credentials in the uploaded env without printing their contents.
"$release/.venv/bin/python" - "$incoming" <<'PY'
import sys
from dotenv import dotenv_values
values = dotenv_values(sys.argv[1])
for name in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN'):
    if not values.get(name):
        raise SystemExit(f'Missing {name}; existing service has not been changed.')
PY
if [[ -f "$config" ]]; then
    cp -p "$config" "$base/uploads/$release_id.previous.env"
fi
install -m 0640 -o root -g tg-knowledge-bot "$incoming" "$config"
rm -f "$incoming"
install -m 0644 "$release/deploy/tg-knowledge-bot.service" /etc/systemd/system/tg-knowledge-bot.service
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl daemon-reload
systemctl enable tg-knowledge-bot.service >/dev/null
systemctl restart tg-knowledge-bot.service

# Verify this launch, not a stale success from a previous process.
started=$(date +%s)
for attempt in $(seq 1 25); do
    if runuser -u tg-knowledge-bot -- "$release/.venv/bin/python" - "$started" <<'PY'
import sqlite3, sys
from datetime import datetime
try:
    with sqlite3.connect('file:/var/lib/tg-knowledge-bot/bot.sqlite3?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM state WHERE key='last_poll'").fetchone()
    assert row and datetime.fromisoformat(row[0]).timestamp() >= int(sys.argv[1])
except Exception:
    raise SystemExit(1)
PY
    then
        echo "Bot deployment healthy: $release_id"
        rm -f "$archive" "$base/uploads/$release_id.previous.env"
        exit 0
    fi
    sleep 2
done
echo 'Bot did not reach healthy polling; restoring previous release.' >&2
if [[ -n "$previous" && -d "$previous" ]]; then
    ln -sfn "$previous" "$base/current.next"
    mv -Tf "$base/current.next" "$base/current"
    if [[ -f "$base/uploads/$release_id.previous.env" ]]; then
        mv "$base/uploads/$release_id.previous.env" "$config"
    fi
    systemctl restart tg-knowledge-bot.service
else
    systemctl stop tg-knowledge-bot.service
fi
exit 1
