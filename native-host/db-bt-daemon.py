#!/usr/bin/env python3
"""DataBridge Bluetooth print daemon — the "TL5X Bluetooth" CUPS driver backend.

How it fits together:

  macOS print system  ──socket──▶  this daemon  ──serial/SPP──▶  TL5X printer
  (queue "TL5X Bluetooth",        127.0.0.1:19100               /dev/cu.TL5XBTPrinter
   PPD: application/pdf raw)

The queue is created with device-uri socket://127.0.0.1:19100 and a mini PPD
that declares `*cupsFilter: "application/pdf 0 -"`: the spooled job arrives
here as the *raw PDF Chrome produced* (no CUPS filters). We then:

  1. render every PDF page with the system PDF engine (CoreGraphics via the
     db-raster.jxa helper — crisp 384 px = 48 mm @ 203 dpi; sips fallback),
  2. auto-crop the whitespace and fit the content onto the 48x80 mm stock
     (384x640 dots), centered,
  3. build a TSPL BITMAP job (db-print-host.py's pyt(), same builder the
     extension host uses) — one SIZE/CLS/BITMAP/PRINT block per page,
  4. write it to the printer over Bluetooth SPP, keeping the serial port
     WARM between jobs (no per-print reconnect delay).

So a print from ANY app to "TL5X Bluetooth" lands on the sticker printer —
no cable, no browser extension involved.

Env overrides (for testing):
  DB_BT_PORT=19100      TCP port to listen on
  DB_BT_DRYRUN=1        build the TSPL but do not touch the serial port

Log:  /private/tmp/databridge-daemon.log
Jobs: /private/tmp/databridge-jobs/ (last 40 pdf/png/tspl kept for debugging)
"""
import importlib.util
import json
import os
import socket
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_PY = os.path.join(HERE, "db-print-host.py")
RASTER_JXA = os.path.join(HERE, "db-raster.jxa")
CONNECT_JXA = os.path.join(HERE, "db-bt-connect.jxa")
PORT = int(os.environ.get("DB_BT_PORT") or 19100)
DRY = os.environ.get("DB_BT_DRYRUN") == "1"
LOG = "/private/tmp/databridge-daemon.log"
JOBS = "/private/tmp/databridge-jobs"
W, H = 384, 640          # 48 x 80 mm @ 203 dpi
W_MM, H_MM = 48, 80
MAX_JOB = 80 * 1024 * 1024
IDLE = 1.2               # seconds without data => job complete


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def load_host():
    """Import db-print-host.py (same folder) — pyt() bitmap builder, warm-port
    machinery, printer discovery and config all live there."""
    spec = importlib.util.spec_from_file_location("dbhost", HOST_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── Rasterize ────────────────────────────────────────────────────────────

def rasterize(pdf_path, prefix):
    """PDF -> [png paths] (one per page). Quartz JXA first, sips fallback
    (sips renders page 1 only)."""
    try:
        r = subprocess.run(
            ["osascript", "-l", "JavaScript", RASTER_JXA, pdf_path, prefix, str(W)],
            capture_output=True, text=True, timeout=90)
        pngs = []
        for line in (r.stdout or "").splitlines():
            if "-> " in line:
                p = line.rsplit("-> ", 1)[1].strip()
                if os.path.exists(p):
                    pngs.append(p)
        if r.returncode == 0 and pngs:
            return pngs
        log("raster(jxa): rc=%s out=%s err=%s" % (
            r.returncode, (r.stdout or "").strip()[:180], (r.stderr or "").strip()[:180]))
    except Exception as e:  # noqa: BLE001
        log("raster(jxa) exception: %r" % e)
    out = prefix + "1.png"
    try:
        r = subprocess.run(["sips", "-s", "format", "png", pdf_path, "--out", out],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and os.path.exists(out):
            log("raster: sips fallback used (page 1 only)")
            return [out]
        log("raster(sips): rc=%s err=%s" % (r.returncode, (r.stderr or "").strip()[:180]))
    except Exception as e:  # noqa: BLE001
        log("raster(sips) exception: %r" % e)
    return []


def fit_page(png_path):
    """Crop whitespace, scale to fit 384x640 (blur-capped upscale), center on
    white canvas. None for a blank page."""
    try:
        from PIL import Image
    except ImportError:
        log("fit_page: PIL missing — check /usr/bin/python3 -m pip install pillow")
        return None
    img = Image.open(png_path).convert("L")
    bb = img.point(lambda v: 255 if v < 248 else 0).getbbox()
    if not bb:
        return None
    img = img.crop(bb)
    w, h = img.size
    k = 1.0
    if w > W or h > H:
        k = min(W / w, H / h)
    else:
        k = min(W / w, H / h, 1.6)  # content already ~label sized: mild upscale
    if abs(k - 1.0) > 0.02:
        img = img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    canvas = Image.new("L", (W, H), 255)
    canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    return canvas


# ── TSPL + serial ────────────────────────────────────────────────────────

def pages_to_tspl(canvases, host):
    blocks = []
    for c in canvases:
        b = [("SIZE %d mm, %d mm" % (W_MM, H_MM)).encode(),
             b"GAP 2 mm, 0", b"DIRECTION 1", b"CLS",
             host.pyt(c, W_MM, H_MM), b"PRINT 1,1"]
        blocks.append(b"\r\n".join(b))
    return b"\r\n".join(blocks) + b"\r\n"


def ensure_bt_connect(dev, host):
    """Force the macOS Bluetooth link up before opening a BT serial device.
    The /dev/cu node of a sleeping SPP printer can silently swallow writes
    while the device sits "Not Connected"; forcing the ACL connect first
    makes the SPP channel come up on the next open."""
    if host.conn_type(dev) != "bluetooth" or not os.path.exists(CONNECT_JXA):
        return True
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", CONNECT_JXA,
                            os.path.basename(dev)],
                           capture_output=True, text=True, timeout=20)
        out = (r.stdout or "").strip()
        log("bt-connect: %s" % out[:160])
        return out.startswith("connected=1")
    except Exception as e:  # noqa: BLE001
        log("bt-connect exception: %r" % e)
        return True


def send_tspl(tspl, host):
    """Warm port first, fresh open otherwise — same strategy as the extension
    host. Bluetooth device preferred (config `preferred` or TL5X default)."""
    dev = host.CONFIG.get("preferred") or host.DEVICE_DEFAULT
    st = host.warm_try(tspl, dev)
    if st:
        st["warm"] = True
        return st
    try:
        import serial
    except ImportError:
        return {"ok": False, "error": "pyserial missing (pip install pyserial)"}
    last = None
    # Bounded retries: a freshly-woken BT printer can time out the first write
    # while RFCOMM is still settling. Attempt 1 tries every baud; retries use
    # the last-known-good/first baud only, so a dead link cannot stall a job
    # for a minute. DB_BT_ATTEMPTS overrides (1..5, default 3).
    attempts = 3
    try:
        attempts = max(1, min(5, int(os.environ.get("DB_BT_ATTEMPTS", "3"))))
    except Exception:
        pass
    preferred_baud = host._WARM.get("baud") or (host.BAUDS[0] if host.BAUDS else 115200)
    for attempt in range(attempts):
        host.warm_close()  # never reuse a handle from a failed pass
        ensure_bt_connect(dev, host)
        bauds = host.BAUDS if attempt == 0 else [preferred_baud]
        for baud in bauds:
            try:
                s = serial.Serial(dev, baud, timeout=5, write_timeout=6)
                time.sleep(0.4 + 0.4 * attempt)  # settle longer on retries
                try:
                    s.write(tspl)
                    s.flush()
                    time.sleep(0.15)
                except Exception:
                    try:
                        s.close()
                    except Exception:
                        pass
                    raise
                host.warm_close()
                host._WARM["dev"] = dev
                host._WARM["baud"] = baud
                host._WARM["ser"] = s
                host._WARM["ts"] = time.time()
                if attempt:
                    log("send recovered on attempt %d/%d @%s" % (attempt + 1, attempts, baud))
                return {"ok": True, "device": dev, "baud": baud, "bytes": len(tspl),
                        "conn": host.conn_type(dev), "attempt": attempt + 1}
            except Exception as e:  # noqa: BLE001
                last = "%s @%s: %s" % (dev, baud, e)
                log("send attempt %d/%d @%s failed: %s" % (attempt + 1, attempts, baud, e))
        time.sleep(0.8 + 1.2 * attempt)
    return {"ok": False, "error": last, "attempts": attempts}


# ── Job pipeline ─────────────────────────────────────────────────────────

def prune():
    try:
        files = []
        for name in os.listdir(JOBS):
            p = os.path.join(JOBS, name)
            try:
                files.append((os.path.getmtime(p), p))
            except Exception:
                pass
        files.sort()
        for _, p in files[:-40]:
            try:
                os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def handle_job(data, host, tag):
    os.makedirs(JOBS, exist_ok=True)
    if not data.startswith(b"%PDF"):
        log("job %s: unsupported %d bytes (head=%r) — only PDF jobs" % (
            tag, len(data), data[:24]))
        return
    pdf = os.path.join(JOBS, "job-%s.pdf" % tag)
    with open(pdf, "wb") as f:
        f.write(data)
    log("job %s: %d bytes PDF saved" % (tag, len(data)))
    prefix = os.path.join(JOBS, "job-%s-p" % tag)
    pngs = rasterize(pdf, prefix)
    if not pngs:
        log("job %s: raster produced no pages" % tag)
        return
    canvases = []
    for i, p in enumerate(pngs):
        c = fit_page(p)
        if c is None:
            log("job %s: page %d blank — skipped" % (tag, i + 1))
            continue
        c.save(os.path.join(JOBS, "job-%s-fit%d.png" % (tag, i + 1)))
        canvases.append(c)
    if not canvases:
        log("job %s: all pages blank — nothing to print" % tag)
        return
    tspl = pages_to_tspl(canvases, host)
    with open(os.path.join(JOBS, "job-%s.tspl" % tag), "wb") as f:
        f.write(tspl)
    log("job %s: %d page(s), TSPL %d bytes" % (tag, len(canvases), len(tspl)))
    if DRY:
        log("job %s: DRY-RUN — serial send skipped" % tag)
        prune()
        return
    st = send_tspl(tspl, host)
    log("job %s: send %s" % (tag, json.dumps(st)))
    prune()


def serve():
    host = load_host()
    if os.path.exists(LOG) and os.path.getsize(LOG) > 2 * 1024 * 1024:
        try:
            os.replace(LOG, LOG + ".old")
        except Exception:
            pass
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(4)
    log("daemon up on 127.0.0.1:%d dry_run=%s device=%s" % (
        PORT, DRY, host.CONFIG.get("preferred") or host.DEVICE_DEFAULT))
    while True:
        try:
            conn, addr = srv.accept()
        except Exception as e:  # noqa: BLE001
            log("accept error: %r" % e)
            time.sleep(1)
            continue
        conn.settimeout(IDLE)
        chunks, total = [], 0
        while True:
            try:
                b = conn.recv(65536)
            except socket.timeout:
                break
            except Exception:  # noqa: BLE001
                break
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > MAX_JOB:
                log("conn %s: over %d bytes — truncated" % (addr[0], MAX_JOB))
                break
        try:
            conn.close()
        except Exception:
            pass
        data = b"".join(chunks)
        if not data:
            continue
        tag = time.strftime("%Y%m%d-%H%M%S")
        try:
            handle_job(data, host, tag)
        except Exception as e:  # noqa: BLE001
            log("job %s error: %r" % (tag, e))


if __name__ == "__main__":
    serve()
