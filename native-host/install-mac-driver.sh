#!/bin/bash
# DataBridge macOS Bluetooth print driver — one-time installer.
#
# Makes the TL5X print like a normal macOS printer (no cable):
#   * installs ~/bin/db-bt-daemon.py + db-raster.jxa + tl5x-bt.ppd
#     (and refreshes db-print-host.py — the shared TSPL/config module)
#   * loads a per-user LaunchAgent so the daemon starts at login and
#     restarts itself (launchd KeepAlive)
#   * creates the CUPS queue "TL5X Bluetooth" -> socket://127.0.0.1:19100
#
# Everything runs in YOUR user account — no sudo/password needed.
# Run:  bash install-mac-driver.sh     (from the native-host folder)
set -u
cd "$(dirname "$0")"
BIN="$HOME/bin"
PLIST="$HOME/Library/LaunchAgents/com.databridge.btprint.plist"
LABEL="com.databridge.btprint"
QUEUE="TL5X_Bluetooth"
PORT=19100
UID_N="$(id -u)"

echo "== DataBridge BT print driver =="
mkdir -p "$BIN" "$HOME/Library/LaunchAgents"

echo "[1/5] files -> ~/bin"
cp -f db-print-host.py db-bt-daemon.py db-raster.jxa tl5x-bt.ppd "$BIN/"
chmod +x "$BIN/db-print-host.py" "$BIN/db-bt-daemon.py"

echo "[2/5] LaunchAgent (daemon auto-start)"
launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$BIN/db-bt-daemon.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>/private/tmp/databridge-daemon.out</string>
  <key>StandardErrorPath</key><string>/private/tmp/databridge-daemon.out</string>
</dict>
</plist>
PLISTEOF
if ! launchctl bootstrap "gui/$UID_N" "$PLIST" 2>/dev/null; then
  launchctl load -w "$PLIST" 2>/dev/null || true
fi
sleep 1

echo "[3/5] printer queue \"$QUEUE\""
lpadmin -p "$QUEUE" -D "TL5X Bluetooth (DataBridge)" -L "DataBridge" -E -v "socket://127.0.0.1:$PORT" -P "$BIN/tl5x-bt.ppd" 2>&1 | grep -v "deprecated" || true
cupsenable "$QUEUE" >/dev/null 2>&1 || true
cupsaccept "$QUEUE" >/dev/null 2>&1 || true

echo "[4/5] status"
if /usr/bin/nc -z 127.0.0.1 $PORT 2>/dev/null; then
  echo "  daemon: OK (127.0.0.1:$PORT)"
else
  echo "  daemon: NOT listening — see /private/tmp/databridge-daemon.log"
fi
lpstat -p "$QUEUE" 2>&1 | head -2

echo "[5/5] done"
echo "  Print from any app -> Printer: \"$QUEUE\" (page size 48 x 80 mm)."
echo "  CLI test:  lp -d \"$QUEUE\" /path/to/label.pdf"
echo "  Log:       /private/tmp/databridge-daemon.log"
echo "  Uninstall: bash uninstall-mac-driver.sh"
