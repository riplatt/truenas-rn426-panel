#!/bin/bash
# Installer for the RN426 front-panel driver on TrueNAS SCALE.
#
# Copies the driver to a directory on one of your pools (the root filesystem is
# reset on TrueNAS updates, so it must live on a pool), loads the i2c modules,
# and registers a POSTINIT init script in the TrueNAS config DB so it starts on
# every boot and survives updates.
#
# Usage:   sudo ./install.sh /mnt/<your-pool>/rn426-panel
# Remove:  sudo ./install.sh --uninstall /mnt/<your-pool>/rn426-panel
set -euo pipefail

COMMENT="RN426 front panel driver (LCD + buttons)"
UNIT="rn426-panel"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() { echo "Usage: sudo $0 [--uninstall] /mnt/<pool>/rn426-panel"; exit 1; }

UNINSTALL=0
if [ "${1:-}" = "--uninstall" ]; then UNINSTALL=1; shift; fi
DEST="${1:-}"; [ -n "$DEST" ] || usage
[ "$(id -u)" = "0" ] || { echo "Please run as root (sudo)."; exit 1; }
command -v midclt >/dev/null || { echo "midclt not found -- this installer is for TrueNAS SCALE."; exit 1; }

find_id() {
  midclt call initshutdownscript.query | python3 -c \
    "import json,sys; print(next((x['id'] for x in json.load(sys.stdin) if (x.get('comment') or '')=='$COMMENT'),''))"
}

if [ "$UNINSTALL" = 1 ]; then
  systemctl stop "$UNIT" 2>/dev/null || true
  id="$(find_id)"
  if [ -n "$id" ]; then midclt call initshutdownscript.delete "$id" >/dev/null && echo "Removed init script id $id."; fi
  echo "Service stopped and autostart removed. Driver files left in $DEST (delete manually if you want)."
  exit 0
fi

echo "Installing to $DEST ..."
mkdir -p "$DEST"
install -m 0755 "$SELF_DIR/rn426_panel.py" "$DEST/rn426_panel.py"
cat > "$DEST/start.sh" <<SH
#!/bin/bash
modprobe i2c-dev 2>/dev/null
modprobe i2c-i801 2>/dev/null
exec /usr/bin/python3 $DEST/rn426_panel.py run
SH
chmod +x "$DEST/start.sh"
modprobe i2c-dev 2>/dev/null || true
modprobe i2c-i801 2>/dev/null || true

CMD="systemd-run --unit=$UNIT --property=Restart=always --property=RestartSec=10 /bin/bash $DEST/start.sh"
id="$(find_id)"
if [ -z "$id" ]; then
  midclt call initshutdownscript.create \
    "{\"type\":\"COMMAND\",\"command\":\"$CMD\",\"when\":\"POSTINIT\",\"enabled\":true,\"timeout\":30,\"comment\":\"$COMMENT\"}" >/dev/null
  echo "Registered POSTINIT autostart."
else
  midclt call initshutdownscript.update "$id" "{\"command\":\"$CMD\",\"enabled\":true}" >/dev/null
  echo "Updated existing POSTINIT autostart (id $id)."
fi

systemctl stop "$UNIT" 2>/dev/null || true; sleep 1
eval "$CMD"; sleep 3
echo "Service: $(systemctl is-active "$UNIT")"
echo
echo "Done. The display should show the hostname/IP page."
echo "If the buttons don't respond, do ONE full power-off/on -- a warm reboot will"
echo "not reset the front-board MCU."
