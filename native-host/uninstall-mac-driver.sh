#!/bin/bash
# DataBridge macOS Bluetooth print driver — uninstaller.
# Removes the LaunchAgent + the "TL5X Bluetooth" CUPS queue.
# (~/bin files are kept so you can re-install instantly.)
set -u
BIN="$HOME/bin"
PLIST="$HOME/Library/LaunchAgents/com.databridge.btprint.plist"
LABEL="com.databridge.btprint"
QUEUE="TL5X_Bluetooth"
UID_N="$(id -u)"

launchctl bootout "gui/$UID_N/$LABEL" >/dev/null 2>&1
launchctl unload -w "$PLIST" >/dev/null 2>&1
rm -f "$PLIST"
lpadmin -x "$QUEUE" 2>&1 | grep -v "deprecated" || true
cupsreject "$QUEUE" >/dev/null 2>&1 || true
echo "Removed: LaunchAgent + printer queue \"$QUEUE\"."
echo "(~/bin/db-bt-daemon.py etc. kept — delete them manually if you want.)"
