#!/usr/bin/env bash
# Kiosk mode for the SD Uploader station: boot straight into the full-screen console.
#
#   sudo bash scripts/ubuntu/kiosk.sh enable    # Next boot starts in kiosk mode
#   sudo bash scripts/ubuntu/kiosk.sh disable   # Back to the normal desktop
#   bash scripts/ubuntu/kiosk.sh status
#
# Enable creates the `sdu-kiosk` user (no password, no shell), installs cage and the kiosk unit,
# and makes the computer boot to the text target so the desktop does not start. Disable puts
# back the boot target that was there before. The station service itself is not touched.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT_SOURCE="$HERE/packaging/linux/kiosk/sdu-kiosk.service"
UNIT=/etc/systemd/system/sdu-kiosk.service
STATE_DIR=/var/lib/sd-mass-uploader/kiosk
KIOSK_USER=sdu-kiosk
STATION_GROUP=sdu

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo." >&2
    exit 1
  fi
}

status() {
  echo "Kiosk unit:      $(systemctl is-enabled sdu-kiosk.service 2>/dev/null || echo not installed)"
  echo "Boot target:     $(systemctl get-default)"
  echo "Station service: $(systemctl is-active sdu-daemon.service 2>/dev/null || echo unknown)"
  command -v cage >/dev/null 2>&1 && echo "cage:            installed" || echo "cage:            missing"
}

enable() {
  need_root
  echo "This makes the computer start straight into the full-screen SD Uploader console."
  echo "The normal desktop will not start. Ctrl+Alt+F2 still gives a text login."
  read -r -p "Turn kiosk mode on? Type 'yes': " answer
  [ "$answer" = "yes" ] || { echo "Nothing changed."; exit 1; }

  apt-get install -y cage python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1
  if ! getent group "$STATION_GROUP" >/dev/null; then
    echo "The station group '$STATION_GROUP' does not exist. Install the station first." >&2
    exit 1
  fi
  mkdir -p "$STATE_DIR"
  if ! id "$KIOSK_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR/home" --create-home --shell /usr/sbin/nologin \
      "$KIOSK_USER"
  fi
  for group in "$STATION_GROUP" video input render; do
    getent group "$group" >/dev/null && usermod -aG "$group" "$KIOSK_USER"
  done
  [ -f "$STATE_DIR/previous-target" ] || systemctl get-default > "$STATE_DIR/previous-target"
  install -m 644 "$UNIT_SOURCE" "$UNIT"
  systemctl daemon-reload
  systemctl enable sdu-kiosk.service
  systemctl set-default multi-user.target
  echo "Kiosk mode is on. Restart the computer to start it."
  echo "To leave it: Ctrl+Alt+F2, log in, then: sudo bash $0 disable"
}

disable() {
  need_root
  systemctl disable --now sdu-kiosk.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl daemon-reload
  previous=graphical.target
  [ -f "$STATE_DIR/previous-target" ] && previous="$(cat "$STATE_DIR/previous-target")"
  systemctl set-default "$previous"
  rm -f "$STATE_DIR/previous-target"
  echo "Kiosk mode is off. Boot target is back to $previous. Restart to see the desktop."
}

case "${1:-status}" in
  enable) enable ;;
  disable) disable ;;
  status) status ;;
  *) echo "Usage: $0 enable|disable|status" >&2; exit 2 ;;
esac
