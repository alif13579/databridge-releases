#!/bin/bash
# DataBridge print-host installer (macOS).
# Installs ~/bin/db-print-host.py + the Chrome native-messaging manifest so the
# extension's 🖨️ button can print stickers on the SPRT TL5X (Bluetooth).
# Safe to re-run (idempotent). No sticker is printed during install.
set -euo pipefail

STORE_ID="gnchjfgedcimmpmoheolhajinihcnipb"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
# Auto-detect the unpacked extension's ID from browser Preferences (matched by
# folder path) so no manual ID is needed; CLI arg still wins when given.
detect_id() {
  python3 - "$SRC_DIR" <<'PY' 2>/dev/null || true
import json, os, shutil, sys, tempfile
root = os.path.normcase(os.path.abspath(os.path.join(sys.argv[1], "..")))
cands = [
    os.path.expanduser("~/Library/Application Support/Google/Chrome/Default/Preferences"),
    os.path.expanduser("~/Library/Application Support/Microsoft Edge/Default/Preferences"),
]
for cp in cands:
    try:
        if not os.path.isfile(cp):
            continue
        tmp = tempfile.mktemp(suffix=".json")
        shutil.copy(cp, tmp)
        try:
            data = json.load(open(tmp, encoding="utf-8"))
        finally:
            try: os.unlink(tmp)
            except Exception: pass
        for eid, info in (((data.get("extensions") or {}).get("settings")) or {}).items():
            if not isinstance(info, dict):
                continue
            p = os.path.normcase(os.path.abspath(info.get("path") or ""))
            if p and (p == root or p.startswith(root + os.sep)):
                print(eid)
                return
    except Exception:
        pass
PY
}
if [ -n "${1:-}" ]; then
  EXT_ID="$1"
else
  EXT_ID="$(detect_id | head -1)"
  [ -z "$EXT_ID" ] && EXT_ID="$STORE_ID"
fi
HOST_DIR="$HOME/bin"
NM_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"

echo "== DataBridge print host install =="
command -v python3 >/dev/null || { echo "python3 missing — install Xcode CLT first"; exit 1; }
python3 -m pip install --user --quiet pyserial pillow || python3 -m pip install --user --quiet --break-system-packages pyserial pillow
echo "pyserial + pillow OK"

mkdir -p "$HOST_DIR" "$NM_DIR"
cp "$SRC_DIR/db-print-host.py" "$HOST_DIR/db-print-host.py"
chmod +x "$HOST_DIR/db-print-host.py"

cat > "$NM_DIR/com.databridge.print.json" <<EOF
{
  "name": "com.databridge.print",
  "description": "DataBridge label print host (SPRT TL5X)",
  "path": "$HOME/bin/db-print-host.py",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
EOF
echo "manifest OK (extension: $EXT_ID)"

# Background print service: keeps the local print agent (55555/55556) and the
# LAN relay (55559/TCP + 55557/UDP) alive even when Chrome is closed, so other
# laptops on the same network can print through this Mac's printer.
PLIST="$HOME/Library/LaunchAgents/com.databridge.printhost.plist"
LABEL="com.databridge.printhost"
UID_N="$(id -u)"
mkdir -p "$HOME/Library/LaunchAgents"
launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1 || true
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$HOME/bin/db-print-host.py</string>
    <string>--serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/private/tmp/databridge-printhost.out</string>
  <key>StandardErrorPath</key><string>/private/tmp/databridge-printhost.out</string>
</dict>
</plist>
PLISTEOF
if ! launchctl bootstrap "gui/$UID_N" "$PLIST" 2>/dev/null; then
  launchctl load -w "$PLIST" 2>/dev/null || true
fi
echo "LAN print service OK ($LABEL) — allow incoming connections if macOS asks."

# Ping test through the real native-messaging framing — no printing involved.
PING_REPLY="$(python3 -c "
import struct, json, subprocess
msg = json.dumps({'action':'ping'}).encode()
p = subprocess.run(['$HOME/bin/db-print-host.py'], input=struct.pack('<I',len(msg))+msg, capture_output=True, timeout=20)
ln = struct.unpack('<I', p.stdout[:4])[0]
print(p.stdout[4:4+ln].decode())
")"
echo "ping reply: $PING_REPLY"
echo "INSTALL DONE — open the extension, CC card → 🖨️ to print."
