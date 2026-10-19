# native-host (public mirror)

These files are what the extension's **Settings → 🖥 Windows setup file** needs
on the target Windows PC. They live in this public repo because the source repo
(`databridge-extension`) is private — `raw.githubusercontent.com` returns 404
for private repos, which broke the one-click setup with
`ERROR: download failed. Check internet and double-click again.`

- **DataBridge-Windows-Setup.bat** — the file downloaded from the extension.
  Bootstraps Python (portable, if none) and fetches `install-windows.py` from
  this folder, then runs it.
- **install-windows.py** — runs on the PC: pip deps, COM port scan, extension ID
  detection, installs the native print host into `%LOCALAPPDATA%\DataBridge`,
  registers it with Chrome + Edge, selftest. It fetches `db-print-host.py` from
  this folder unless that file sits next to the script.
- **db-print-host.py** — the actual native-messaging print host.
- **install-mac.sh** / **install-windows.bat** — manual/developer variants.

Keep in sync with `DataBridge-Extension/native-host/` (after editing the files
there, copy them here and push — otherwise the one-file setup would silently
roll back to old versions).
