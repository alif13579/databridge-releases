#!/usr/bin/env python3
"""DataBridge print-host installer (Windows) — zero-touch, double-click OK.

No sticker is printed during install. Everything is auto-detected:
  1. pip deps (pyserial + pillow + qrcode + python-barcode) install via the running interpreter
  2. TL5X COM port scan (FALLS BACK to runtime discovery — no prompt)
  3. Extension ID read from Chrome/Edge Preferences (matched by folder path,
     so unpacked extensions just work — no ID to copy)

Optional overrides (rarely needed):
  install-windows.py [COM_PORT] [EXTRA_EXT_ID]

Exit code 0 = ready to print (after one extension reload).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

STORE_EXT_ID = "gnchjfgedcimmpmoheolhajinihcnipb"
HOST_NAME = "com.databridge.print"
TARGET = os.path.join(os.environ.get("LOCALAPPDATA", ""), "DataBridge")
# Fallback when db-print-host.py is not next to this script (e.g. the
# one-file setup ran us from %TEMP%): fetch it from GitHub. The URL points at
# the PUBLIC mirror repo (databridge-releases) — the source repo is private,
# so raw.githubusercontent.com 404s for anonymous Windows PCs there.
RAW_HOST_URL = ("https://raw.githubusercontent.com/alif13579/"
                "databridge-releases/main/native-host/db-print-host.py")


def say(msg=""):
    print(msg, flush=True)


def norm(p):
    try:
        return os.path.normcase(os.path.abspath(p or ""))
    except Exception:
        return ""


def ext_root():
    """The extension folder = parent of the folder holding this script."""
    return norm(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def ensure_deps():
    say("[1/6] python deps (pyserial + pillow + qrcode + python-barcode)...")
    # Embedded/portable Pythons have no user site — retry without --user.
    for args in (["--user", "--quiet"], ["--quiet"]):
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install"] + args +
                ["pyserial", "pillow", "qrcode", "python-barcode"],
                capture_output=True, text=True, timeout=300)
        except Exception as e:
            return False, "pip launch failed: %s" % e
        if r.returncode == 0:
            say("      OK")
            return True, ""
        last = (r.stderr or r.stdout or "")[-300:]
    return False, "pip install failed (internet?): %s" % last


def detect_com(override=""):
    """Returns (port or '', how). Only pins a fixed port when confident
    (TL5X-named or a single bluetooth candidate) — otherwise the host scans
    all ports at print time, so guessing wrong is worse than not guessing."""
    say("[2/6] printer COM port scan...")
    if override:
        say("      override: %s" % override)
        return override, "manual override"
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
    except Exception as e:
        say("      scan unavailable (%s) — runtime discovery will handle it" % e)
        return "", "scan unavailable"
    bt, tl = [], ""
    for p in ports:
        desc = ((p.description or "") + " " + (p.hwid or "") + " " +
                (p.device or "")).lower()
        if "usb" in desc and "bluetooth" not in desc:
            continue  # cable is found at print time; not our problem here
        if "tl5x" in desc or "sprt" in desc:
            tl = p.device
        if "bluetooth" in desc or "bt" in desc or "rfcomm" in desc:
            bt.append(p.device)
    if tl:
        say("      found TL5X: %s" % tl)
        return tl, "TL5X match"
    if len(bt) == 1:
        say("      single bluetooth port: %s" % bt[0])
        return bt[0], "only bluetooth port"
    if len(bt) > 1:
        say("      %d bluetooth ports (%s) — leaving to runtime discovery"
            % (len(bt), ", ".join(bt)))
    else:
        say("      no bluetooth port yet (pair the TL5X first) — "
            "runtime discovery will pick it up once paired")
    return "", "runtime discovery"


def ids_from_prefs(prefs_path, root):
    """Extension IDs whose installed path == our folder (copy first: the
    browser locks this file while running)."""
    found = []
    try:
        if not os.path.isfile(prefs_path):
            return found
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as t:
            tmp = t.name
        try:
            shutil.copy(prefs_path, tmp)
            with open(tmp, encoding="utf-8") as f:
                data = json.load(f)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        settings = (data.get("extensions") or {}).get("settings") or {}
        for eid, info in settings.items():
            if not isinstance(info, dict):
                continue
            p = norm(info.get("path") or "")
            if p and (p == root or p.startswith(root + os.sep)):
                found.append(eid)
    except Exception:
        pass
    return found


def detect_ids(extra=""):
    say("[3/6] extension ID detect...")
    root = ext_root()
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [
        os.path.join(local, "Google", "Chrome", "User Data", "Default", "Preferences"),
        os.path.join(local, "Microsoft", "Edge", "User Data", "Default", "Preferences"),
    ]
    # a couple of extra profiles, cheap to check
    for prof in ("Profile 1", "Profile 2"):
        cands.append(os.path.join(local, "Google", "Chrome", "User Data", prof, "Preferences"))
        cands.append(os.path.join(local, "Microsoft", "Edge", "User Data", prof, "Preferences"))
    ids = []
    for cp in cands:
        for eid in ids_from_prefs(cp, root):
            if eid not in ids:
                ids.append(eid)
    if extra and extra not in ids:
        ids.append(extra)
    if STORE_EXT_ID not in ids:
        ids.append(STORE_EXT_ID)
    if len(ids) > 1:
        say("      detected: %s (+ store ID)" % ", ".join(ids[:-1]))
    else:
        say("      store ID only (extension folder not found in browser "
            "profiles — install the extension first, then re-run)")
    return ids


def stop_existing_hosts():
    """Stop old print-host processes before replacing the file.

    Without this an already-running --serve process keeps the OLD code (and
    the old serial port) alive after an update, so fixes never take effect
    until the next reboot.
    """
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*db-print-host.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=60)
    except Exception:
        pass


def install_files(com_port):
    say("[4/6] installing host files...")
    def host_source():
        here = os.path.dirname(os.path.abspath(__file__))
        local = os.path.join(here, "db-print-host.py")
        if os.path.isfile(local):
            return local
        say("      fetching print host from GitHub...")
        import urllib.request
        tmp = os.path.join(TARGET, "db-print-host.py.download")
        os.makedirs(TARGET, exist_ok=True)
        urllib.request.urlretrieve(RAW_HOST_URL, tmp)
        if os.path.getsize(tmp) < 10000:
            raise RuntimeError("downloaded host looks truncated")
        return tmp

    try:
        os.makedirs(TARGET, exist_ok=True)
        src = host_source()
        shutil.copy(src, os.path.join(TARGET, "db-print-host.py"))
        # merge into existing per-machine config — never wipe sticker
        # size/offset the user already tuned
        cfg_path = os.path.join(TARGET, "db-print-config.json")
        cfg = {}
        try:
            with open(cfg_path, encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    cfg = d
        except Exception:
            cfg = {}
        if com_port:
            cfg["bluetooth"] = com_port
        # LAN print was removed in 7.4.116 — strip its stale keys so an old
        # relay_only/relay_key can never affect printing again.
        for stale in ("relay_enabled", "relay_share", "relay_use",
                      "relay_only", "relay_key", "relay_station",
                      "relay_block", "relay_scope", "relay_port",
                      "relay_discovery_port", "node_name"):
            cfg.pop(stale, None)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        with open(os.path.join(TARGET, "db-print-host.bat"), "w") as f:
            f.write("@echo off\r\n")
            f.write('"%s" "%%~dp0db-print-host.py" %%*\r\n' % sys.executable)
        say("      %s" % TARGET)
        return True, ""
    except Exception as e:
        return False, "file install failed: %s" % e


def register(ids):
    say("[5/6] registering with Chrome + Edge...")
    manifest = os.path.join(TARGET, HOST_NAME + ".json")
    origins = ["chrome-extension://%s/" % i for i in ids]
    try:
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"name": HOST_NAME,
                       "description": "DataBridge label print host (SPRT TL5X)",
                       "path": os.path.join(TARGET, "db-print-host.bat"),
                       "type": "stdio",
                       "allowed_origins": origins}, f, indent=2)
    except Exception as e:
        return False, "manifest write failed: %s" % e
    keys = [
        r"HKCU\Software\Google\Chrome\NativeMessagingHosts\%s" % HOST_NAME,
        r"HKCU\Software\Microsoft\Edge\NativeMessagingHosts\%s" % HOST_NAME,
    ]
    for key in keys:
        try:
            r = subprocess.run(
                ["reg", "add", key, "/ve", "/t", "REG_SZ",
                 "/d", manifest, "/f"],
                capture_output=True, text=True, timeout=30)
        except Exception as e:
            return False, "registry tool failed: %s" % e
        if r.returncode != 0:
            return False, "registry write failed for %s" % key
        say("      %s OK" % ("Chrome" if "Chrome" in key else "Edge"))
    return True, ""


def selftest():
    say("selftest (no sticker)...")
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(TARGET, "db-print-host.py"),
             "--selftest"],
            capture_output=True, text=True, timeout=60)
    except Exception as e:
        return False, "selftest launch failed: %s" % e
    out = (r.stdout or "").strip()
    if r.returncode != 0 or '"ok": true' not in out:
        return False, "selftest bad reply: %s" % (out or r.stderr or "?")[:200]
    say("      %s" % out[:160])
    return True, ""


def host_exe():
    """Interpreter for the background print service — windowless if possible."""
    d = os.path.dirname(sys.executable)
    for name in ("pythonw.exe", os.path.basename(sys.executable)):
        cand = os.path.join(d, name)
        if cand and os.path.isfile(cand):
            return cand
    return sys.executable


def install_autostart():
    """Run `db-print-host.py --serve` at login so printing keeps working
    even when Chrome/Edge is closed."""
    say("[6/6] background print service...")
    host = os.path.join(TARGET, "db-print-host.py")
    exe = host_exe()
    value = "DataBridgePrintHost"
    run_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        if exe.lower().endswith("pythonw.exe"):
            cmd = '"%s" "%s" --serve' % (exe, host)
        else:
            # Console Python: hidden VBS wrapper so no window flashes at login.
            vbs = os.path.join(TARGET, "db-print-serve.vbs")
            with open(vbs, "w", encoding="utf-8") as f:
                f.write('Set sh = CreateObject("WScript.Shell")\r\n')
                f.write('sh.Run """%s"" ""%s"" --serve", 0, False\r\n'
                        % (exe, host))
            cmd = 'wscript.exe //B "%s"' % vbs
        r = subprocess.run(
            ["reg", "add", run_key, "/v", value, "/t", "REG_SZ",
             "/d", cmd, "/f"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, "autostart registry write failed"
    except Exception as e:
        return False, "autostart failed: %s" % e
    # Start it now too, so background printing works before the next login.
    try:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        if exe.lower().endswith("python.exe"):
            flags |= 0x08000000  # CREATE_NO_WINDOW
        subprocess.Popen([exe, host, "--serve"], cwd=TARGET,
                         creationflags=flags, close_fds=True)
    except Exception as e:
        say("      (will start at next login: %s)" % e)
    say("      OK — %s" % value)
    return True, ""


def remove_legacy_firewall_rules():
    """Best effort: delete inbound rules left by pre-7.4.116 LAN installs."""
    for name in ("DataBridge LAN print TCP 55559",
                 "DataBridge LAN print UDP 55557"):
        try:
            subprocess.run(
                'netsh advfirewall firewall delete rule name="%s"' % name,
                capture_output=True, text=True, timeout=30)
        except Exception:
            pass


def main(argv):
    say("== DataBridge print host install (Windows, automatic) ==")
    com_override = (argv[0] if len(argv) > 0 else "").strip()
    extra_id = (argv[1] if len(argv) > 1 else "").strip()
    if not os.environ.get("LOCALAPPDATA"):
        say("ERROR: %LOCALAPPDATA% missing — not a normal Windows env.")
        return 1
    ok, err = ensure_deps()
    if not ok:
        say("ERROR: " + err)
        return 1
    com_port, _how = detect_com(com_override)
    ids = detect_ids(extra_id)
    stop_existing_hosts()
    ok, err = install_files(com_port)
    if not ok:
        say("ERROR: " + err)
        return 1
    ok, err = register(ids)
    if not ok:
        say("ERROR: " + err)
        return 1
    ok, err = install_autostart()
    if not ok:
        say("WARNING: " + err)
    remove_legacy_firewall_rules()
    ok, err = selftest()
    if not ok:
        say("ERROR: " + err)
        return 1
    say()
    say("INSTALL DONE — reload the extension once, then print. "
        "No COM port or ID typing needed.")
    say("Background print service installed: printing keeps working after "
        "Chrome/Edge closes. Everything stays on this PC (no network server).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
