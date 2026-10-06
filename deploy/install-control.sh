#!/usr/bin/env bash
# One-time setup of the UI control helper (reset, restart, git pull, branch switch) for systemd servers.
#   sudo bash deploy/install-control.sh [dashboard.service] [freqtrade.service]
# The dashboard stays sandboxed; it only writes data/supervisor/request.json. A root oneshot unit,
# started by a path unit when that file appears, runs git/pip as the repository owner and systemctl.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "sudo ilə işlədin: sudo bash $0" >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "$PY tapılmadı" >&2; exit 1; }

detect() { for unit in "$@"; do systemctl cat "$unit" >/dev/null 2>&1 && { echo "$unit"; return; }; done; }
DASHBOARD="${1:-$(detect crypto-jev-dashboard.service crypto-jev.service)}"
FREQTRADE="${2:-$(detect crypto-jev-freqtrade.service crypto-jev-paper.service)}"
if [ -z "$DASHBOARD" ] || [ -z "$FREQTRADE" ]; then
  echo "Servis adları tapılmadı. Belə işlədin: sudo bash $0 <dashboard.service> <freqtrade.service>" >&2
  exit 1
fi

OWNER="$(stat -c %U "$REPO")"
DUSER="$(systemctl show -p User --value "$DASHBOARD")"
DUSER="${DUSER:-root}"
DATA="$(cd "$REPO" && runuser -u "$OWNER" -- "$PY" -c 'from app.config import Settings; print(Settings.load().data_dir.resolve())')"
CONTROL="$DATA/supervisor"
install -d -m 0770 -o "$DUSER" -g "$(id -gn "$DUSER")" "$CONTROL"

cat > /etc/systemd/system/crypto-jev-control.service <<EOF
[Unit]
Description=Crypto Radar UI control requests (reset, restart, git)

[Service]
Type=oneshot
WorkingDirectory=$REPO
Environment=CONTROL_DASHBOARD_SERVICE=$DASHBOARD
Environment=CONTROL_FREQTRADE_SERVICE=$FREQTRADE
ExecStart=$PY -m app.control
TimeoutStartSec=1200
EOF

cat > /etc/systemd/system/crypto-jev-control.path <<EOF
[Unit]
Description=Watch Crypto Radar UI control requests

[Path]
PathExists=$CONTROL/request.json

[Install]
WantedBy=multi-user.target
EOF

printf '{"dashboard": "%s", "freqtrade": "%s"}\n' "$DASHBOARD" "$FREQTRADE" > "$CONTROL/systemd.json"
chown "$DUSER:$(id -gn "$DUSER")" "$CONTROL/systemd.json"
systemctl daemon-reload
systemctl enable --now crypto-jev-control.path
echo "Hazır: $DASHBOARD + $FREQTRADE UI-dan idarə olunur (sorğu qovluğu: $CONTROL)."
echo "Log: journalctl -u crypto-jev-control.service -n 50 --no-pager"
