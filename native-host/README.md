# native-host (public mirror)

These files are what the extension's **Settings → 🖥 Windows setup file** and
the Mac installers need. They live in this public repo because the source repo
(`databridge-extension`) is private — `raw.githubusercontent.com` returns 404
for private repos, which broke the one-click setup with
`ERROR: download failed. Check internet and double-click again.`

## Windows

- **DataBridge-Windows-Setup.bat** — the file downloaded from the extension.
  Bootstraps Python (portable, if none) and fetches `install-windows.py` from
  this folder, then runs it.
- **install-windows.py** — runs on the PC: pip deps, COM port scan, extension ID
  detection, installs the native print host into `%LOCALAPPDATA%\DataBridge`,
  registers it with Chrome + Edge, selftest. It fetches `db-print-host.py` from
  this folder unless that file sits next to the script.
- **db-print-host.py** — the actual native-messaging print host (also carries
  the `driver_status` / `driver_test` / `driver_restart` actions the popup's
  Driver health panel uses on macOS).
- **install-windows.bat** — manual/developer variant.

## macOS

- **install-mac.sh** — installs the print host + native-messaging manifests.
- **install-mac-driver.sh** — the cable-free Bluetooth driver: CUPS queue
  `TL5X_Bluetooth` (socket → 127.0.0.1:19100) + user LaunchAgent
  `com.databridge.btprint`. No sudo. Run `bash install-mac-driver.sh`.
- **uninstall-mac-driver.sh** — removes the queue + LaunchAgent.
- **db-bt-daemon.py** — the LaunchAgent: CUPS job PDF → CoreGraphics 203 dpi
  raster → TSPL bitmap → SPP write (warm port, bounded retries).
- **db-raster.jxa** — PDF→PNG via CoreGraphics (exact 384 px head width).
- **db-bt-connect.jxa** — nudges the Bluetooth ACL link up for a sleeping
  printer before the first write.
- **tl5x-bt.ppd** — mini PPD (PDF passthrough, 48×80/60×80/80×100/100×150/
  100×180 mm + A4).

## Keeping it current

`DataBridge-Extension/release.sh` runs `mirror-native-host.py` automatically
after publishing (needs `GITHUB_TOKEN`), pushing any changed file from
`native-host/` here. Manual: `GITHUB_TOKEN=... python3 mirror-native-host.py`
(or `--dry` to preview). Never add machine config (`db-print-config.json`),
keystores or logs to this folder.
