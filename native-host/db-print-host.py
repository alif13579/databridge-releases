#!/usr/bin/env python3
"""DataBridge print host — native-messaging bridge to the SPRT TL5X label printer.

Stdin/stdout protocol (Chrome native messaging): each message is a 4-byte
little-endian length prefix followed by a UTF-8 JSON body.

Requests:
  {"action": "ping"}                                   -> {"ok": true, ...}
  {"action": "print_label", consignment, customer,
   phone, address, cod, branch, copies, device,
   dry_run}                                            -> {"ok": true, ...}
    dry_run=true builds the TSPL bytes but never touches the serial port
    (safe to verify layout without spending a sticker).

The site's own central-print → Sticker Print (SPRT) path POSTs its default
3x4in label HTML to http://localhost:55555/api/print; this host serves that
local agent, renders the HTML with Chrome and prints the bitmap. That is the
path used on /central-print (no custom template).

print_label and the local HTTP service open the serial port. Everything else
is side-effect free.
Label: site default 75x102mm (600x816 dots) for the local agent; the built-in
print_label template remains 48x80mm TSPL.
"""
import sys
import os
import json
import struct
import time
import subprocess
import socket
import threading
import io
import base64
import tempfile
import shutil
import http.server
import urllib.parse


def load_config():
    """Per-machine overrides (e.g. Windows COM port): db-print-config.json
    next to this script, e.g. {"device": "COM4"}. Missing = defaults."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "db-print-config.json"), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_config(patch):
    """Merge patch into the per-machine config file. Returns (ok, error)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "db-print-config.json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
                if not isinstance(d, dict):
                    d = {}
        except Exception:
            d = {}
        d.update(patch)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        global CONFIG
        CONFIG = d
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


CONFIG = load_config()
DEVICE_DEFAULT = CONFIG.get("device") or CONFIG.get("bluetooth") or (
    "/dev/cu.TL5XBTPrinter" if os.name != "nt" else "COM4"
)


def conn_type(dev):
    """bluetooth vs cable, for the toast ('which printer printed?').
    Explicit per-machine override wins (db-print-config.json {"conn": ...});
    otherwise guessed from the device name."""
    override = str(CONFIG.get("conn") or "").strip().lower()
    if override in ("bluetooth", "bt"):
        return "bluetooth"
    if override in ("cable", "usb"):
        return "cable"
    d = str(dev or "").lower()
    if "bluetooth" in d or "btprinter" in d:
        return "bluetooth"
    if "usb" in d or "cable" in d:
        return "cable"
    return "unknown"


def stock_dims(p):
    """Dynamic sticker size: payload width_mm/height_mm wins, else per-machine
    config, else 48x80 (proved by the CALIB 48x80 photo). Returns
    (w_mm, h_mm, W_dots, H_dots, align). Align: left/center/right."""
    def num(v, default, lo, hi):
        try:
            f = float(v)
            if f != f or f == float("inf") or f == float("-inf"):
                return default
            return max(lo, min(hi, int(round(f))))
        except (TypeError, ValueError):
            return default
    w = num(p.get("width_mm", CONFIG.get("width_mm", 48)), 48, 30, 110)
    h = num(p.get("height_mm", CONFIG.get("height_mm", 80)), 80, 20, 150)
    a = str(p.get("align", CONFIG.get("align", "left")) or "left").strip().lower()
    if a not in ("left", "center", "centre", "right"):
        a = "left"
    if a == "centre":
        a = "center"
    return w, h, w * 8, h * 8, a


# TSPL built-in font dot widths (approx, x_mul=1): used to center TEXT.
TSPL_FONT_W = {"1": 8, "2": 12, "3": 16, "4": 24, "5": 32}


def stock_offset(p):
    """Print-position fine tune in dots (signed): payload offset_x_mm /
    offset_y_mm wins, else per-machine config, else 0. +x = right, +y = down
    (negative y moves content up — for printers that feed extra top space).
    Range clamped to ±20mm so a typo can't throw content off-stock."""
    def mm(v):
        try:
            f = float(v)
            if f != f or f == float("inf") or f == float("-inf"):
                return 0
            return max(-20, min(20, int(round(f * 8))))
        except (TypeError, ValueError):
            return 0
    ox = mm(p.get("offset_x_mm", CONFIG.get("offset_x_mm", 0)))
    oy = mm(p.get("offset_y_mm", CONFIG.get("offset_y_mm", 0)))
    return ox, oy


def border_on(p):
    """Per-print 'border' payload wins; else per-machine config key."""
    if isinstance(p, dict) and "border" in p:
        v = p.get("border")
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "on", "yes")
    return bool(CONFIG.get("border", False))


def border_line(W, H):
    """Calibration frame: 1mm inset from every edge, 3-dot pen. Makes the
    true printable rectangle visible on every sticker so margins can be
    judged against the physical die-cut and corrected via offsets."""
    ins = 8
    return "BOX %d,%d,%d,%d,3" % (ins, ins, max(2, W - ins * 2), max(2, H - ins * 2))


def apply_offset(tspl, ox, oy):
    """Shift a built TEXT-path TSPL job by (ox, oy) dots. Covers TEXT,
    BARCODE, QRCODE, BAR and BITMAP origins; coordinates clamp at 0 so a
    big negative offset clips at the edge instead of emitting invalid TSPL.
    BITMAP *pixels* are shifted separately in pages_to_tspl."""
    import re
    if not ox and not oy:
        return tspl
    def shift(m):
        cmd, x, y = m.group(1), int(m.group(2)) + ox, int(m.group(3)) + oy
        return "%s %d,%d" % (cmd, max(0, x), max(0, y))
    # NB: BITMAP origins stay 0,0 — bitmap *pixels* are pre-shifted in
    # pages_to_tspl, so moving the origin as well would double-shift.
    return re.sub(r"\b(TEXT|BARCODE|QRCODE|BAR|BOX) (\d+),(\d+)",
                  shift, tspl.decode("ascii", errors="replace")).encode("ascii", errors="replace")


def shift_page(im, ox, oy):
    """Shift a 1-bit page image by (ox, oy) dots on a white canvas of the
    same size (negative = crop that edge, exposed area stays white)."""
    if not ox and not oy:
        return im
    from PIL import Image as _I
    W, H = im.size
    cv = _I.new("L", (W, H), 255)
    try:
        cv.paste(im, (ox, oy))
    except Exception:
        pass
    return cv


def text_x(text, font, W, align, margin=12):
    """X for a TSPL TEXT so left/center/right align inside W dots."""
    if align == "center":
        tw = len(str(text or "")) * TSPL_FONT_W.get(str(font), 12)
        return max(margin, (W - tw) // 2)
    if align == "right":
        tw = len(str(text or "")) * TSPL_FONT_W.get(str(font), 12)
        return max(margin, W - margin - tw)
    return margin


def barcode_fit(cid, W, align, margin=3):
    """(x, narrow) for a Code128 barcode with safe edge clearance.

    Margin bug fixed: a 14-char ID at narrow=2 is ~378 dots — on a 384-dot
    stock starting at x=6 it ended exactly at dot 384 (zero right margin,
    any printer tolerance clipped it). Now x shifts left to keep `margin`
    dots of clearance (14-char → x=3, ends at 381), and only long IDs fall
    back to narrow=1. narrow=2 stays preferred — this TL5X prints narrow=1
    faintly, so we keep 2 whenever it fits.
    """
    cid = str(cid or "")
    for narrow in (2, 1):
        bw = (11 * len(cid) + 35) * narrow
        room = W - margin * 2 - bw
        if room < 0:
            continue  # doesn't fit even edge-to-edge minus margins
        if align == "center":
            return max(margin, (W - bw) // 2), narrow
        if align == "right":
            return max(margin, W - margin - bw), narrow
        # left: keep historic x≈6..12 but never cross the right margin
        return max(margin, min(12, W - margin - bw)), narrow
    # Pathological ID (still too long at narrow=1): pin left, let it clip
    # rather than go negative — caller truncates the human-readable text.
    return margin, 1


def barcode_x(cid, W, align, narrow=2, margin=6):
    """Back-compat wrapper (tests / old callers). Prefers narrow=2 fit."""
    x, _ = barcode_fit(cid, W, align, margin=margin)
    return x


def qr_size(cell=4):
    """Approx QR side in dots for a short consignment ID (v1 ≈ 21 modules)."""
    return (21 + 8) * cell


def qr_x(W, align, cell=4, margin=12):
    s = qr_size(cell)
    if align == "center":
        return max(margin, (W - s) // 2)
    if align == "right":
        return max(margin, W - margin - s)
    return margin


def bar_w(W, margin=12):
    return max(100, W - margin * 2)


def bar_x(W, align, margin=12):
    """X for a full-width BAR rule honoring the sticker align."""
    bw = bar_w(W, margin)
    if align == "center":
        return max(margin, (W - bw) // 2)
    if align == "right":
        return max(margin, W - margin - bw)
    return margin


def vscale(H, base=640):
    """Vertical scaler: layouts are authored for 80mm (640 dots); on taller
    stock (e.g. 115mm) y positions spread proportionally so the design keeps
    its proportions instead of bunching at the top. H=640 → identity."""
    return lambda y: int(y * H / base)


def wrap_chars_for(W, font="2", margin=12):
    """Address wrap width that fills the stock (was fixed 30 chars =
    full-bleed on 48mm but a narrow column on 85mm)."""
    fw = TSPL_FONT_W.get(font, 12)
    return max(30, (W - margin * 2) // fw)


def discover_printers():
    """All candidate label printers as [(device, conn)], cable FIRST then
    bluetooth — cable always gets priority when both are plugged in.
    Unknown devices are skipped (never poke random hardware)."""
    found = []
    try:
        if os.name == "nt":
            from serial.tools import list_ports
            for p in list_ports.comports():
                desc = ((p.description or "") + " " + (p.device or "")).lower()
                if "usb" in desc:
                    found.append((p.device, "cable"))
                elif "bluetooth" in desc:
                    found.append((p.device, "bluetooth"))
        else:
            import glob
            cands = sorted(glob.glob("/dev/cu.*") + glob.glob("/dev/ttyUSB*") +
                           glob.glob("/dev/ttyACM*") + glob.glob("/dev/rfcomm*"))
            for dev in cands:
                base = os.path.basename(dev).lower()
                if "incoming" in base:
                    continue  # macOS Bluetooth-Incoming-Port listener, not a printer
                if "usb" in base or "acm" in base:
                    found.append((dev, "cable"))
                elif "bluetooth" in base or "btprinter" in base or "rfcomm" in base:
                    found.append((dev, "bluetooth"))
    except Exception:
        pass
    found.sort(key=lambda x: 0 if x[1] == "cable" else 1)
    return found


# ── macOS CUPS driver health / test / restart ────────────────────────────
# The cable-free path is: website print → CUPS queue "TL5X_Bluetooth" →
# user LaunchAgent com.databridge.btprint (db-bt-daemon.py) → SPP. These
# actions let the popup answer "driver thik ache?" without a real print.
DRIVER_QUEUE = "TL5X_Bluetooth"
DRIVER_LABEL = "com.databridge.btprint"
DAEMON_LOG = "/private/tmp/databridge-daemon.log"
DAEMON_PORT = 19100


def _run(cmd, timeout=15):
    """Run a small system command, never raise. Returns (rc, out, err)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def _tail(path, nbytes=600):
    """Last non-empty line of a log file (best-effort)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            lines = [ln for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
            return lines[-1][:300] if lines else ""
    except Exception:
        return ""


def _pdf_escape(s):
    return str(s).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_test_pdf(path):
    """Minimal one-page 48x80mm PDF (Helvetica text) for a driver test print.
    Pure stdlib — reportlab/CoreGraphics are not guaranteed on a fresh Mac."""
    w, h = 136.06, 226.77
    rows = [
        (14, h - 34, 15, "DATABRIDGE"),
        (14, h - 52, 9, "BLUETOOTH DRIVER TEST"),
        (14, h - 72, 9, time.strftime("%Y-%m-%d %H:%M:%S")),
        (14, h - 112, 9, "If this label prints,"),
        (14, h - 126, 9, "the CUPS driver works."),
        (14, h - 166, 8, "queue: " + DRIVER_QUEUE),
    ]
    cmds = ["0.5 w 6 6 %.2f %.2f re S" % (w - 12, h - 12)]
    for x, y, size, text in rows:
        cmds.append("BT /F1 %.1f Tf %.2f %.2f Td (%s) Tj ET" % (size, x, y, _pdf_escape(text)))
    stream = "\n".join(cmds).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
         "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>" % (w, h)).encode(),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n" % i).encode() + body + b"\nendobj\n"
    xref = len(out)
    out += ("xref\n0 %d\n" % (len(objs) + 1)).encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref)).encode()
    with open(path, "wb") as f:
        f.write(out)
    return path


def driver_status():
    """Everything the popup needs for the green/red driver row."""
    if sys.platform != "darwin":
        return {"ok": True, "supported": False, "platform": sys.platform}
    daemon = {"loaded": False, "pid": 0, "last_exit": "", "plist": False}
    try:
        daemon["plist"] = os.path.exists(
            os.path.expanduser("~/Library/LaunchAgents/%s.plist" % DRIVER_LABEL))
    except Exception:
        pass
    rc, so, _se = _run(["/bin/launchctl", "list"])
    if rc == 0:
        for line in so.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[-1] == DRIVER_LABEL:
                daemon["loaded"] = True
                daemon["pid"] = int(parts[0]) if parts[0].isdigit() else 0
                daemon["last_exit"] = parts[1]
                break
    queue = {"exists": False, "state": ""}
    rc, so, se = _run(["/usr/bin/lpstat", "-p", DRIVER_QUEUE])
    queue["state"] = (so or se or "").splitlines()[0][:200] if (so or se) else ""
    queue["exists"] = rc == 0 and DRIVER_QUEUE in (so or "")
    listening = False
    try:
        s = socket.create_connection(("127.0.0.1", DAEMON_PORT), timeout=1)
        s.close()
        listening = True
    except Exception:
        pass
    bt_dev = [d for d, c in discover_printers() if c == "bluetooth"]
    return {
        "ok": True, "supported": True, "platform": "darwin",
        "daemon": daemon, "queue": queue,
        "port": {"listening": listening, "port": DAEMON_PORT},
        "bt_devices": bt_dev,
        "daemon_log": _tail(DAEMON_LOG),
    }


def driver_test():
    """Print a tiny built-in test label through the CUPS queue (end-to-end:
    spool → PPD passthrough → daemon → raster → TSPL → Bluetooth)."""
    if sys.platform != "darwin":
        return {"ok": False, "supported": False,
                "error": "macOS driver only — Windows prints directly via the host"}
    try:
        path = make_test_pdf("/tmp/db-driver-test.pdf")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "PDF build failed: %s" % e}
    rc, so, se = _run(["/usr/bin/lp", "-d", DRIVER_QUEUE, path], timeout=25)
    if rc != 0:
        return {"ok": False, "error": (se or so or "lp failed")[:300], "pdf": path}
    return {"ok": True, "job": so[:200], "pdf": path}


def driver_restart():
    """Kickstart the LaunchAgent (safe: KeepAlive would restart it anyway)."""
    if sys.platform != "darwin":
        return {"ok": False, "supported": False, "error": "macOS only"}
    rc, so, se = _run(["/bin/launchctl", "kickstart", "-k",
                       "gui/%d/%s" % (os.getuid(), DRIVER_LABEL)], timeout=20)
    time.sleep(0.6)
    st = driver_status()
    return {"ok": rc == 0, "output": (so or se or "")[:300],
            "daemon": st.get("daemon"), "port": st.get("port")}


BAUDS = [115200, 9600]
HOST_VERSION = 3

# ── Warm serial port ─────────────────────────────────────────────────────
# The extension now keeps ONE host process alive for every print (persistent
# native-messaging port), so the printer's serial port can stay OPEN between
# jobs: no per-print Python spawn and no per-print Bluetooth reconnect — the
# two biggest sources of "print e delay". The port is reused while the last
# print is fresh; after WARM_MAX_IDLE seconds it is reopened from scratch so
# a sleeping printer gets a fresh connection (a reconnect is what wakes it).
WARM_MAX_IDLE = 600.0
_WARM = {"dev": None, "baud": None, "ser": None, "ts": 0.0}
PRINT_LOCK = threading.Lock()


def warm_close():
    try:
        if _WARM.get("ser") is not None:
            _WARM["ser"].close()
    except Exception:
        pass
    _WARM["dev"] = None
    _WARM["baud"] = None
    _WARM["ser"] = None
    _WARM["ts"] = 0.0


def warm_try(tspl, dev=None):
    """Reuse the held-open port (same device, not idle too long). Returns a
    result dict on success, None when there is no usable warm port."""
    s = _WARM.get("ser")
    if s is None:
        return None
    if dev and dev != _WARM.get("dev"):
        return None
    if time.time() - (_WARM.get("ts") or 0.0) > WARM_MAX_IDLE:
        warm_close()
        return None
    try:
        s.write(tspl)
        s.flush()
        time.sleep(0.15)
        _WARM["ts"] = time.time()
        return {"ok": True, "device": _WARM["dev"], "baud": _WARM["baud"],
                "bytes": len(tspl), "conn": conn_type(_WARM["dev"]), "warm": True}
    except Exception:
        warm_close()
        return None


def read_msg():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (ln,) = struct.unpack("<I", raw_len)
    if ln <= 0 or ln > 1024 * 1024:
        return None
    data = sys.stdin.buffer.read(ln)
    if len(data) < ln:
        return None
    return json.loads(data.decode("utf-8"))


def write_msg(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def tspl_text(s, n):
    return str(s or "").replace('"', "'").replace("\n", " ").strip()[:n]


def wrap(s, width, max_lines=2):
    """Word-wrap that RESPECTS the user's own newlines: each paragraph wraps
    separately, blank lines stay blank. So typed line breaks print as-is."""
    out = []
    for para in str(s or "").split("\n"):
        words = para.split()
        if not words:
            out.append("")
        else:
            lines, cur = [], ""
            for w in words:
                trial = (cur + " " + w).strip()
                if len(trial) <= width:
                    cur = trial
                else:
                    if cur:
                        lines.append(cur)
                    cur = w
            if cur:
                lines.append(cur)
            out.extend(lines)
        if len(out) >= max_lines:
            break
    return out[:max_lines]


def wrap2(s, width):
    """Exactly 2 lines (address fields) — pads short text, truncates long."""
    return (wrap(s, width, max_lines=2) + ["", ""])[:2]


def render_margin_test_pages(W=384, H=640, w_mm=48, h_mm=80):
    """Full-bleed edge-cut test: nested borders at 0/3/6/12 dots + side
    labels on all four sides. One print shows exactly which edge clips:
    - outer (0-dot) border fully visible = zero clipping on that side
    - outer clipped but 3-dot visible = ~0.4mm cut, etc.
    Labels sit inside the 12-dot line so they stay readable even when the
    outer borders clip."""
    from PIL import Image, ImageDraw
    img = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(img)
    # nested inset guides: 0 (edge), 3, 6, 12 dots
    dr.rectangle([0, 0, W - 1, H - 1], outline=0, width=2)
    dr.rectangle([3, 3, W - 4, H - 4], outline=0, width=1)
    dr.rectangle([6, 6, W - 7, H - 7], outline=0, width=1)
    dr.rectangle([12, 12, W - 13, H - 13], outline=0, width=2)
    fbig = font_for(24, True, False)
    f = font_for(18, True, False)
    fsmall = font_for(16, False, False)

    def ct(y, text, font):
        try:
            tw = dr.textlength(text, font=font)
        except Exception:
            tw = len(text) * 10
        dr.text((max(14, (W - tw) // 2), y), text, font=font, fill=0)

    ct(16, "TOP", f)
    ct(H - 44, "BOTTOM", f)
    ct(44, "%dx%d" % (w_mm, h_mm), fbig)
    ct(72, "MARGIN TEST", f)
    # LEFT / RIGHT labels (horizontal, inside the safe zone)
    try:
        dr.text((16, H // 2 - 10), "LEFT", font=f, fill=0)
        rlab = "RIGHT"
        rtw = dr.textlength(rlab, font=f)
        dr.text((W - 16 - rtw, H // 2 - 10), rlab, font=f, fill=0)
    except Exception:
        pass
    # tick numbers along top (x) and left (y) edges
    for x in range(0, W, 48):
        dr.line([(x, 0), (x, 8)], fill=0, width=2)
        try:
            dr.text((x + 2, 96), str(x), font=fsmall, fill=0)
        except Exception:
            pass
    for y in range(0, H, 80):
        dr.line([(0, y), (8, y)], fill=0, width=2)
        dr.line([(W - 1, y), (W - 9, y)], fill=0, width=2)
        try:
            dr.text((20, y + 4), str(y), font=fsmall, fill=0)
            ylab = str(y)
            ytw = dr.textlength(ylab, font=fsmall)
            dr.text((W - 24 - ytw, y + 4), ylab, font=fsmall, fill=0)
        except Exception:
            pass
    # inset markers: which guide line is which
    try:
        dr.text((W - 60, 16), "0/3/6/12", font=fsmall, fill=0)
        dr.text((W - 60, H - 64), "0/3/6/12", font=fsmall, fill=0)
    except Exception:
        pass
    # thin center cross (does not touch any edge)
    dr.line([(W // 2, 120), (W // 2, H - 120)], fill=0, width=1)
    return [img]


def render_width_test_pages(W=680, w_mm=85):
    """Max-print-width finder: one short sticker (40mm tall) with a numbered
    ruler across the full Settings width. Wherever the print STOPS = the
    print-head limit (e.g. numbers stop at 48 = 48mm head). Heavy lines at
    the 48mm and 80mm marks for instant reading."""
    from PIL import Image, ImageDraw
    H = 320  # 40mm — short, saves paper
    img = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W - 1, H - 1], outline=0, width=2)
    fbig = font_for(28, True, False)
    f = font_for(20, True, False)
    fsmall = font_for(16, False, False)
    try:
        title = "WIDTH TEST %dmm" % w_mm
        tw = dr.textlength(title, font=fbig)
        dr.text(((W - tw) // 2, 8), title, font=fbig, fill=0)
    except Exception:
        pass
    # mm ruler: line every 10mm (80 dots), numbered top + bottom
    for mm in range(0, w_mm + 1, 10):
        x = mm * 8
        if x >= W:
            break
        dr.line([(x, 0), (x, H - 1)], fill=0, width=2)
        lab = str(mm)
        try:
            dr.text((x + 4, 48), lab, font=f, fill=0)
            btw = dr.textlength(lab, font=f)
            dr.text((x + 4, H - 76), lab, font=f, fill=0)
            # dot count underneath (exact cutoff reading)
            dots = str(x)
            dr.text((x + 4, H - 44), dots, font=fsmall, fill=0)
        except Exception:
            pass
    # landmark lines: 48mm (384 dots) and 80mm (640 dots)
    for mark_mm, tag in ((48, "48mm"), (80, "80mm")):
        x = mark_mm * 8
        if 0 < x < W:
            dr.line([(x, 0), (x, H - 1)], fill=0, width=4)
            try:
                dr.text((x - 74, 100), tag, font=fbig, fill=0)
            except Exception:
                pass
    # dot ticks every 48 across the top (fine-grained cutoff)
    for x in range(0, W, 48):
        dr.line([(x, 0), (x, 14)], fill=0, width=2)
    return [img]


def build_bitmap_sticker(p):
    """Full-stock bitmap rasterization of a pathao/std sticker — bypasses the
    flaky built-in TEXT/QRCODE firmware path entirely: every glyph is drawn
    by PIL (config border on = the calibration frame prints), the barcode
    stays NATIVE TSPL (that path is proven reliable), QR via the `qrcode`
    lib when it is installed (else clean skip). Content rests inside a
    3mm inset on all four sides — equal margins no matter the label size,
    and the declared page still equals the true stock so pagination syncs
    ( cannon rule: SIZE+GAP must equal label+gap, content never fills)."""
    from PIL import Image, ImageDraw
    w_mm, h_mm, W, H, align = stock_dims(p)
    ins = 24                       # 3mm breathing margin on every side
    canvas = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(canvas)
    if border_on(p):
        dr.rectangle([ins, ins, W - ins, H - ins], outline=0, width=3)
    k = (H - 2 * ins) / 640.0      # design rows authored on a 0..640 grid
    L, R = ins + 6, W - ins - 6
    def S(y):
        return int(ins + y * k)
    def fs(sz):
        return int(round(sz * k))
    def cx():
        return (ins + W - ins) // 2
    def place(y, s, size, bold=False):
        if not s:
            return
        f = font_for(fs(size), bold, False)
        if align == "center":
            dr.text((cx(), S(y)), s, font=f, fill=0, anchor="mm")
        elif align == "right":
            dr.text((R, S(y)), s, font=f, fill=0, anchor="rm")
        else:
            dr.text((L, S(y)), s, font=f, fill=0, anchor="lm")
    def hline(y):
        dr.line([(ins, S(y)), (W - ins, S(y))], fill=0, width=2)
    cid = tspl_text(p.get("consignment"), 20)
    place(16, "PATHAO", 26, True)
    place(56, cid, 24, True)
    place(172, tspl_text(p.get("customer"), 30), 22, True)
    place(212, tspl_text(p.get("phone"), 20), 15)
    aw = wrap_chars_for(W)
    addr_lines = (wrap(tspl_text(p.get("address"), aw * 3), aw, max_lines=3) + ["", "", ""])[:3]
    for i, ln in enumerate(addr_lines):
        place(244 + i * 30, ln, 15)
    place(360, "COD Tk %s" % (tspl_text(p.get("cod"), 12) or "0"), 22, True)
    place(428, tspl_text(p.get("branch") or p.get("hub"), 24), 22, True)
    import datetime
    place(468, datetime.date.today().strftime("%d-%b-%y"), 14)
    # QR (bitmap) — 96-dot square, centered, if the qrcode lib is present.
    qr_obj = None
    if cid:
        try:
            import qrcode
            qr = qrcode.QRCode(box_size=2, border=1,
                               error_correction=qrcode.constants.ERROR_CORRECT_L)
            qr.add_data(cid); qr.make(fit=True)
            qr_obj = qr.make_image(fill_color="black", back_color="white")
        except Exception:
            qr_obj = None
    if qr_obj is not None:
        try:
            qsize = 96
            qimg = qr_obj.get_image().convert("L").resize((qsize, qsize), Image.LANCZOS)
            canvas.paste(qimg, (cx() - qsize // 2, S(496)), qimg.point(lambda v: 255 if v > 128 else 0))
            place(612, cid, 13)
        except Exception:
            pass
    else:
        place(560, cid, 13)
    hline(344)
    hline(412)
    narrow = max(2, barcode_fit(cid, W - 2 * ins, align)[1] or 2)
    bar_y = S(96)
    bar_x = (W - min(W - 2 * ins, len(cid) * 13 * narrow)) // 2
    out = [("SIZE %d mm, %d mm" % (w_mm, h_mm)).encode(),
           b"GAP 2 mm, 0", b"DIRECTION 1", b"CLS",
           pyt(canvas, w_mm, h_mm),
           ('BARCODE %d,%d,"128",%d,1,0,%d,%d,"%s"'
            % (bar_x, bar_y, int(64 * k), narrow, narrow, cid)).encode(),
           b"PRINT 1,1"]
    return b"\r\n".join(out) + b"\r\n"


def pyt(canvas, w_mm, h_mm):
    """Single-page BITMAP TSPL chunk (no SIZE/CLS/PRINT) — bytes for one
    raster regardless of the caller's surrounding commands."""
    W, H = canvas.size
    px = canvas.convert("L").load()
    row_bytes = W // 8
    buf = bytearray()
    for yy in range(H):
        for xb in range(row_bytes):
            b = 0
            for bit in range(8):
                if px[xb * 8 + bit, yy] >= 128:
                    b |= 0x80 >> bit
            buf.append(b)
    return ("BITMAP 0,0,%d,%d,0," % (row_bytes, H)).encode() + bytes(buf)


def build_tspl(p):
    tpl = (p.get("template") or "pathao")
    if tpl in ("pathao", "std") and (p.get("render") or CONFIG.get("render", "bitmap")) == "bitmap":
        return build_bitmap_sticker(p)
    if tpl == "run":
        return build_run_tspl(p)
    if tpl == "margins":
        w_mm, h_mm, W, H, _al = stock_dims(p)
        ox, oy = stock_offset(p)
        return pages_to_tspl(render_margin_test_pages(W, H, w_mm, h_mm),
                             h_mm=h_mm, w_mm=w_mm, ox=ox, oy=oy)
    if tpl == "widthtest":
        w_mm, _h_mm, W, _H, _al = stock_dims(p)
        ox, oy = stock_offset(p)
        return pages_to_tspl(render_width_test_pages(W, w_mm),
                             h_mm=40, w_mm=w_mm, ox=ox, oy=oy)
    if tpl == "calibrate":
        cpages, ch_mm = render_calibrate_pages()
        ox, oy = stock_offset(p)
        return pages_to_tspl(cpages, h_mm=ch_mm, ox=ox, oy=oy)
    if tpl in ("note", "letter"):
        return build_note_tspl(p)
    if tpl == "pathao":
        return build_pathao_tspl(p)
    return build_std_tspl(p)


def render_calibrate_pages():
    """Full-label numbered ruler (384x640 = 48x80mm). Covers the whole stock
    so one photo maps the true printable area: report which numbers/lines
    land on the label edges."""
    from PIL import Image, ImageDraw
    W, H = 384, 640
    img = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W - 1, H - 1], outline=0, width=2)
    f = font_for(16, False, False)
    fbig = font_for(24, True, False)
    for x in range(0, W, 48):
        dr.text((x + 2, 4), str(x), font=f, fill=0)
        dr.line([(x, 0), (x, 10)], fill=0, width=2)
    for y in range(0, H, 40):
        dr.text((2, y + 2), str(y), font=f, fill=0)
        dr.line([(0, y), (10, y)], fill=0, width=2)
        dr.text((W - 32, y + 2), str(y), font=f, fill=0)
        dr.line([(W - 1, y), (W - 11, y)], fill=0, width=2)
    # mm reference lines: where 30mm and 40mm end (240 / 320 dots)
    for mm, yy in (("30mm LINE", 240), ("40mm LINE", 320)):
        dr.line([(0, yy), (W - 1, yy)], fill=0, width=1)
        dr.text((W // 2 - 70, yy + 4), mm, font=fbig, fill=0)
    dr.text((W // 2 - 95, H // 2 - 20), "CALIB 48x80", font=fbig, fill=0)
    return [img], 80


def build_note_tspl(p):
    """Rich note sticker. PIL renderer (bold/head/columns/orientation, later
    Bangla/logo) when Pillow exists; plain TSPL-text fallback otherwise.
    Honors width_mm/height_mm/align for full customisation."""
    try:
        from PIL import Image  # noqa: F401 - availability check
    except ImportError:
        return build_note_tspl_text(p)
    w_mm, h_mm, W, H, align = stock_dims(p)
    pages = render_note_pages(p.get("title"), p.get("body"),
                              note_orientation(p),
                              letter=((p.get("from"), p.get("to"))
                                      if (p.get("template") or "") == "letter" else None),
                              W=W, H=H, align=align)
    ox, oy = stock_offset(p)
    return pages_to_tspl(pages, h_mm=h_mm, w_mm=w_mm, ox=ox, oy=oy)


LATIN_FONTS = [
    ("/System/Library/Fonts/Helvetica.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
    ("C:\\Windows\\Fonts\\arial.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
]
LATIN_BOLD_FONTS = [
    ("/System/Library/Fonts/Helvetica.ttc", 1),
    ("C:\\Windows\\Fonts\\arialbd.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
]
BANGLA_FONTS = [
    ("/System/Library/Fonts/KohinoorBangla.ttc", 0),
    ("C:\\Windows\\Fonts\\Nirmala.ttf", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf", 0),
]
BANGLA_BOLD_FONTS = [
    ("/System/Library/Fonts/KohinoorBangla.ttc", 1),
    ("C:\\Windows\\Fonts\\NirmalaB.ttf", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf", 0),
]
_font_cache = {}


def has_bengali(s):
    return any("\u0980" <= ch <= "\u09FF" for ch in str(s or ""))


def font_for(size, bold, bengali):
    key = (size, bold, bengali)
    if key in _font_cache:
        return _font_cache[key]
    cands = []
    if bengali:
        cands += BANGLA_BOLD_FONTS if bold else BANGLA_FONTS
    if bold:
        cands += LATIN_BOLD_FONTS
    cands += LATIN_FONTS
    f = None
    for path, idx in cands:
        try:
            f = __import__("PIL.ImageFont", fromlist=["truetype"]).truetype(path, size, index=idx)
            break
        except Exception:
            pass
        if idx == 0:
            try:
                f = __import__("PIL.ImageFont", fromlist=["truetype"]).truetype(path, size)
                break
            except Exception:
                pass
    if f is None:
        try:
            from PIL import ImageFont
            try:
                f = ImageFont.load_default(size=size)
            except TypeError:
                f = ImageFont.load_default()
        except Exception:
            f = None
    _font_cache[key] = f
    return f


def parse_note_markup(title, body):
    """Blocks: ('head', text) | ('cols', left, right) | ('segs', [(text, bold)]).
    Markup: '# head', 'left | right', '*bold*'. Unmatched '*' = literal."""
    blocks = []
    if (title or "").strip():
        blocks.append(("head", title.strip()))
    for raw in str(body or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            t = line.lstrip("#").strip()
            if t:
                blocks.append(("head", t))
            continue
        if "|" in line:
            l, r = line.split("|", 1)
            blocks.append(("cols", l.strip(), r.strip()))
            continue
        segs, buf, bold = [], "", False
        for ch in line:
            if ch == "*":
                if buf:
                    segs.append((buf, bold))
                    buf = ""
                bold = not bold
            else:
                buf += ch
        if buf:
            segs.append((buf, bold))
        if segs:
            blocks.append(("segs", segs))
    return blocks


def render_note_pages(title, body, orientation, letter=None, W=None, H=None, align="left"):
    """Render note to 1-bit PIL pages (default 384x640 = 48x80mm). Landscape
    renders tall then rotates onto the stock. Align left/center/right.
    letter=(from,to): From/To two-column header above the body."""
    from PIL import Image, ImageDraw
    landscape = (orientation or "portrait") == "landscape"
    if W is None or H is None:
        W, H = 384, 640
    if landscape:
        # pre-rotate canvas so the post-rotate page is W x H
        W, H = H, W
    F_HEAD, F_NORM = 30, 21
    pages = []
    img = dr = None
    y = 0

    def new_page():
        nonlocal img, dr, y
        img = Image.new("L", (W, H), 255)  # L-mode: 0 black .. 255 white
        dr = ImageDraw.Draw(img)
        y = 6
        pages.append(img)

    def need(h):
        if y + h > H - 4:
            new_page()

    def text_w(text, size, bold):
        f = font_for(size, bold, has_bengali(text))
        try:
            return dr.textlength(text, font=f)
        except Exception:
            return len(text) * size * 0.6

    def put(x, yy, text, size, bold):
        f = font_for(size, bold, has_bengali(text))
        if f is None:
            return
        try:
            dr.text((x, yy), text, font=f, fill=0)
        except Exception:
            pass

    def ax(text, size, bold):
        """X honoring the sticker align (middle print = center)."""
        if align == "left":
            return 8
        try:
            tw = dr.textlength(text, font=font_for(size, bold, has_bengali(text)))
        except Exception:
            tw = len(str(text or "")) * size * 0.6
        if align == "center":
            return max(8, int((W - tw) // 2))
        return max(8, int(W - 8 - tw))

    def colx(text, size, bold, x0, x1):
        """X for two-column blocks (FROM/TO, a|b): left keeps the old edge
        positions; center middle-aligns each column inside its half; right
        pushes each column to its half's right edge."""
        if align == "left":
            return x0
        try:
            tw = dr.textlength(text, font=font_for(size, bold, has_bengali(text)))
        except Exception:
            tw = len(str(text or "")) * size * 0.6
        if align == "center":
            return max(x0, int(x0 + (x1 - x0 - tw) // 2))
        return max(x0, int(x1 - tw))

    new_page()
    blocks = []
    if letter and ((letter[0] or "").strip() or (letter[1] or "").strip()):
        # Letter order: title on TOP, then From/To columns, then the body.
        if (title or "").strip():
            blocks.append(("head", title.strip()))
        blocks.append(("fromto", wrap(letter[0], 40, max_lines=3),
                       wrap(letter[1], 40, max_lines=3)))
        blocks += [b for b in parse_note_markup("", body)]
    else:
        blocks += parse_note_markup(title, body)
    for blk in blocks:
        if len(pages) >= 6:
            break
        if blk[0] == "head":
            need(F_HEAD + 8)
            t = blk[1][:40]
            put(ax(t, F_HEAD, True), y, t, F_HEAD, True)
            y += F_HEAD + 8
        elif blk[0] == "cols":
            need(F_NORM + 8)
            half = W // 2
            lt = blk[1][: (half - 12) // 11]
            rt = blk[2][: (W - half - 12) // 11]
            put(colx(lt, F_NORM, False, 8, half - 4), y, lt, F_NORM, False)
            put(colx(rt, F_NORM, False, half + 4, W - 8), y, rt, F_NORM, False)
            y += F_NORM + 8
        elif blk[0] == "fromto":
            # Narrow stock (landscape) gets a smaller font so addresses fit.
            fs = 16 if W < 300 else F_NORM
            cw = max(8, int((W // 2 - 16) / (fs * 0.55)))
            need(fs + 8)
            half = W // 2
            put(colx("FROM", fs, True, 8, half - 4), y, "FROM", fs, True)
            put(colx("TO", fs, True, half + 4, W - 8), y, "TO", fs, True)
            y += fs + 8
            flines, tlines = blk[1], blk[2]
            for i in range(max(len(flines), len(tlines))):
                if len(pages) >= 6:
                    break
                need(fs + 6)
                if i < len(flines):
                    fl = flines[i][:cw]
                    put(colx(fl, fs, False, 8, half - 4), y, fl, fs, False)
                if i < len(tlines):
                    tl = tlines[i][:cw]
                    put(colx(tl, fs, False, half + 4, W - 8), y, tl, fs, False)
                y += fs + 6
            need(10)
            try:
                dr.line([(8, y + 2), (W - 8, y + 2)], fill=0, width=2)
            except Exception:
                pass
            y += 10
        else:
            # flatten to words, greedy-fill by pixel width
            words = []
            for text, bold in blk[1]:
                for w in text.split(" "):
                    words.append((w, bold))
            max_w = W - 16
            cur = []
            cur_w = 0
            lines = []

            def flush():
                if cur:
                    lines.append(cur[:])

            for w, bold in words:
                if not w:
                    continue
                ww = text_w(w + " ", F_NORM, bold)
                if cur and cur_w + ww > max_w:
                    flush()
                    cur, cur_w = [], 0
                    ww = text_w(w + " ", F_NORM, bold)
                cur.append((w, bold))
                cur_w += ww
            flush()
            for segs in lines:
                if len(pages) >= 6:
                    break
                need(F_NORM + 6)
                try:
                    line_w = sum(text_w(w + " ", F_NORM, b) for w, b in segs)
                except Exception:
                    line_w = 0
                if align == "center":
                    x = max(8, int((W - line_w) // 2))
                elif align == "right":
                    x = max(8, int(W - 8 - line_w))
                else:
                    x = 8
                for i, (w, bold) in enumerate(segs):
                    put(x, y, w, F_NORM, bold)
                    x += text_w(w + " ", F_NORM, bold)
                y += F_NORM + 6
    if landscape:
        pages = [im.rotate(-90, expand=True) for im in pages]
    return pages


def pil_to_b64(im, max_w=384):
    """PIL image -> base64 PNG data URL piece (no file read needed by the
    extension). Downscales wide images so the preview stays small."""
    import base64
    import io
    try:
        w, h = im.size
        if w > max_w:
            im = im.resize((max_w, int(h * max_w / w)))
        buf = io.BytesIO()
        im.convert("L").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


def render_parcel_preview(p):
    """Approx preview of the TSPL parcel/run sticker as a PIL image (same
    SIZE + align as the real print). Layout mirrors build_pathao/std/run so
    what you see is what prints — without spending a sticker."""
    from PIL import Image, ImageDraw
    tpl = (p.get("template") or "pathao")
    w_mm, h_mm, W, H, align = stock_dims(p)
    S = vscale(H)  # same vertical spread as the TSPL builders
    img = Image.new("L", (W, H), 255)
    dr = ImageDraw.Draw(img)
    dr.rectangle([0, 0, W - 1, H - 1], outline=0, width=2)

    def tx(yb, text, size, bold=False):
        # yb is the EXACT base-space y from the TSPL builders below, so the
        # preview lands where the real print lands.
        y = S(yb)
        f = font_for(size, bold, has_bengali(text))
        if f is None:
            return
        try:
            tw = dr.textlength(text, font=f)
        except Exception:
            tw = len(text) * size * 0.6
        if align == "center":
            x = max(4, int((W - tw) // 2))
        elif align == "right":
            x = max(4, int(W - 4 - tw))
        else:
            x = 8
        try:
            dr.text((x, y), text, font=f, fill=0)
        except Exception:
            pass

    def bars(yb, label, h=64):
        # fake barcode: alternating bars (same height as the TSPL barcode)
        y = S(yb)
        bw = min(W - 16, max(120, len(label) * 12))
        x0 = 8 if align == "left" else (W - 8 - bw if align == "right" else (W - bw) // 2)
        for i in range(0, bw, 4):
            dr.rectangle([x0 + i, y, x0 + i + 2, y + h], fill=0)
        return yb

    def rule(yb):
        dr.line([(8, S(yb)), (W - 8, S(yb))], fill=0, width=2)

    def qrbox(yb, label):
        # Stacked to mirror the TSPL (QR above its caption — side-by-side
        # overflowed on center/right). Cell ~3 => ~87 dots for short IDs.
        y = S(yb)
        s = min(87, H - y - 32)
        if s < 40:
            return H
        x0 = 8 if align == "left" else (W - 8 - s if align == "right" else (W - s) // 2)
        dr.rectangle([x0, y, x0 + s, y + s], outline=0, width=2)
        dr.line([(x0, y), (x0 + s, y + s)], fill=0, width=1)
        dr.line([(x0 + s, y), (x0, y + s)], fill=0, width=1)
        # No caption here — the TSPL caption is drawn separately at base
        # y=590/578 (see tx() calls below), same as the real print.
        return  # terminal block

    # Base-space y values are copied 1:1 from build_run/pathao/std_tspl above,
    # so the preview shows the real positions (only barcode/QR glyphs differ).
    if tpl == "run":
        tx(16, tspl_text(p.get("consignment"), 20) or "ID", 24, True)
        bars(56, tspl_text(p.get("consignment"), 20), h=72)
        tx(148, tspl_text(p.get("status"), 24) or "Status", 28, True)
        tx(212, tspl_text(p.get("run"), 30) or "Run", 22)
        tx(264, tspl_text(p.get("hub"), 30), 22)
        tx(316, tspl_text(p.get("date"), 20), 20)
        rule(368)
        tx(384, tspl_text(p.get("run"), 30) or "Run", 22)
    elif tpl == "std":
        tx(16, tspl_text(p.get("consignment"), 20) or "ID", 24, True)
        bars(56, tspl_text(p.get("consignment"), 20), h=64)
        tx(140, tspl_text(p.get("customer"), 30), 22)
        tx(188, tspl_text(p.get("phone"), 20), 20)
        _aw0 = wrap_chars_for(W)
        _al0 = (wrap(tspl_text(p.get("address"), _aw0 * 3), _aw0, max_lines=3) + ["", "", ""])[:3]
        for _i0, _ln0 in enumerate(_al0):
            if _ln0:
                tx(220 + _i0 * 32, _ln0, 20)
        rule(324)
        _cod0 = tspl_text(p.get("cod"), 12)
        if _cod0:
            tx(336, "COD Tk %s" % _cod0, 28, True)
        rule(392)
        tx(404, tspl_text(p.get("branch") or p.get("hub"), 24), 22)
        tx(452, tspl_text(p.get("consignment"), 20), 20)
        qrbox(484, tspl_text(p.get("consignment"), 20))
        if _cod0:
            tx(578, "COD Tk %s" % _cod0, 20)
    else:
        if tpl == "pathao":
            tx(12, "PATHAO", 28, True)
        tx(56, tspl_text(p.get("consignment"), 20) or "ID", 24, True)
        bars(92, tspl_text(p.get("consignment"), 20), h=64)
        tx(172, tspl_text(p.get("customer"), 30), 22)
        tx(212, tspl_text(p.get("phone"), 20), 20)
        _aw = wrap_chars_for(W)
        _alines = (wrap(tspl_text(p.get("address"), _aw * 3), _aw, max_lines=3) + ["", "", ""])[:3]
        for _i, _ln in enumerate(_alines):
            if _ln:
                tx(244 + _i * 32, _ln, 20)
        rule(344)
        cod = tspl_text(p.get("cod"), 12) or "0"
        tx(356, "COD Tk %s" % cod, 28, True)
        rule(412)
        hub = tspl_text(p.get("branch") or p.get("hub"), 24)
        if hub:
            tx(424, hub, 22)
        qrbox(496, tspl_text(p.get("consignment"), 20))
        tx(590, tspl_text(p.get("consignment"), 20), 20)
    return img


def pages_to_tspl(pages, h_mm=80, w_mm=48, ox=0, oy=0):
    """PIL pages -> TSPL BITMAP job. SIZE must match the rendered canvas
    (dynamic width bug fixed: was hardcoded to 48mm wide). The offset is
    applied to the *pixels* (see apply_offset) so the origin stays 0,0."""
    out = [("SIZE %d mm, %d mm" % (w_mm, h_mm)).encode(),
           b"GAP 2 mm, 0", b"DIRECTION 1", b"CLS"]
    for im in pages:
        W, H = im.size
        target_w = max(240, w_mm * 8)  # 30mm->240 .. 80mm->640
        if W != target_w:
            im = im.resize((target_w, int(H * target_w / W)))
            W, H = im.size
        im = shift_page(im, ox, oy)
        px = im.convert("L").load()
        row_bytes = W // 8
        buf = bytearray()
        for yy in range(H):
            for xb in range(row_bytes):
                b = 0
                for bit in range(8):
                    if px[xb * 8 + bit, yy] >= 128:
                        b |= 0x80 >> bit
                buf.append(b)
        out.append(("BITMAP 0,0,%d,%d,0," % (row_bytes, H)).encode() + bytes(buf))
        out.append(b"PRINT 1,1")
    return b"\r\n".join(out) + b"\r\n"


def note_orientation(p):
    """Explicit orientation wins; otherwise letter defaults to landscape."""
    if p.get("orientation"):
        return p.get("orientation")
    return "landscape" if (p.get("template") or "") == "letter" else "portrait"


def build_note_tspl_text(p):
    """Fallback when Pillow is missing: plain-text note, dynamic SIZE + align."""
    w_mm, h_mm, W, H, align = stock_dims(p)
    title = tspl_text(p.get("title"), 24) or "NOTE"
    body_lines = wrap(tspl_text(p.get("body"), 600), 30, max_lines=40)
    per_page = max(5, (H - 60) // 36)
    pages, chunk = [], []
    for ln in body_lines:
        chunk.append(ln)
        if len(chunk) == per_page:
            pages.append(chunk)
            chunk = []
    if chunk or not pages:
        pages.append(chunk)
    out = ["SIZE %d mm, %d mm" % (w_mm, h_mm), "GAP 2 mm, 0", "DIRECTION 1"]
    for page in pages:
        out.append("CLS")
        out.append('TEXT %d,8,"3",0,1,1,"%s"' % (text_x(title, "3", W, align), title))
        y = 44
        for ln in page:
            out.append('TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(ln, "2", W, align), y, ln))
            y += 36
        out.append("PRINT 1,1")
    return ("\r\n".join(out) + "\r\n").encode("ascii", errors="replace")


def build_run_tspl(p):
    """Run-page relabel sticker (default 48x80mm, dynamic SIZE + align).
    Middle print = align center. Barcode keeps 4-dot edge clearance."""
    import datetime
    w_mm, h_mm, W, H, align = stock_dims(p)
    S = vscale(H)
    cid = tspl_text(p.get("consignment"), 20)
    status = tspl_text(p.get("status"), 24)
    run = tspl_text(p.get("run"), 30)
    hub = tspl_text(p.get("hub"), 30)
    date = tspl_text(p.get("date"), 20) or datetime.date.today().strftime("%d-%b-%y")
    bx, narrow = barcode_fit(cid, W, align)
    bw = bar_w(W)
    rx = bar_x(W, align)
    lines = [
        "SIZE %d mm, %d mm" % (w_mm, h_mm),
        "GAP 2 mm, 0",
        "DIRECTION 1",
        "CLS",
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(cid, "3", W, align), S(16), cid),
        'BARCODE %d,%d,"128",72,1,0,%d,%d,"%s"' % (bx, S(56), narrow, narrow, cid),
        'TEXT %d,%d,"4",0,1,1,"%s"' % (text_x(status, "4", W, align), S(148), status),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(run, "3", W, align), S(212), run),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(hub, "3", W, align), S(264), hub),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(date, "2", W, align), S(316), date),
        "BAR %d,%d,%d,2" % (rx, S(368), bw),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(run, "3", W, align), S(384), run),
        'TEXT %d,%d,"4",0,1,1,"%s"' % (text_x(status, "4", W, align), S(440), status),
        'TEXT %d,%d,"2",0,1,1,"%s %s"' % (text_x(hub + " " + date, "2", W, align), S(508), hub, date),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(cid, "3", W, align), S(544), cid),
        "PRINT 1,1",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii", errors="replace")


def build_pathao_tspl(p):
    """Pathao-style sticker (default 48x80mm, dynamic SIZE + align).
    Margin-safe: barcode keeps edge clearance (auto narrow fallback),
    address wraps at 30 chars (was 32 = full-bleed overflow), QR + ID/COD
    stack vertically so center/right align never pushes text off-stock,
    bottom keeps ~3mm clearance above the perforation."""
    import datetime
    w_mm, h_mm, W, H, align = stock_dims(p)
    S = vscale(H)
    aw = wrap_chars_for(W)
    cid = tspl_text(p.get("consignment"), 20)
    cust = tspl_text(p.get("customer"), 30)
    phone = tspl_text(p.get("phone"), 20)
    addr_lines = wrap(tspl_text(p.get("address"), aw * 3), aw, max_lines=3)
    addr_lines = (addr_lines + ["", "", ""])[:3]
    a1, a2, a3 = addr_lines
    cod = tspl_text(p.get("cod"), 12) or "0"
    hub = tspl_text(p.get("branch") or p.get("hub"), 24)
    today = datetime.date.today().strftime("%d-%b-%y")
    bx, narrow = barcode_fit(cid, W, align)
    bw = bar_w(W)
    rx = bar_x(W, align)
    qx = qr_x(W, align, cell=3)
    cod_line = "COD Tk %s" % cod
    lines = [
        "SIZE %d mm, %d mm" % (w_mm, h_mm),
        "GAP 2 mm, 0",
        "DIRECTION 1",
        "CLS",
    ] + ([border_line(W, H)] if border_on(p) else []) + [
        'TEXT %d,%d,"4",0,1,1,"PATHAO"' % (text_x("PATHAO", "4", W, align), S(12)),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(cid, "3", W, align), S(56), cid),
        'BARCODE %d,%d,"128",64,1,0,%d,%d,"%s"' % (bx, S(92), narrow, narrow, cid),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(cust, "3", W, align), S(172), cust),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(phone, "2", W, align), S(212), phone),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(a1, "2", W, align), S(244), a1),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(a2, "2", W, align), S(276), a2),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(a3, "2", W, align), S(308), a3),
        "BAR %d,%d,%d,2" % (rx, S(344), bw),
        'TEXT %d,%d,"4",0,1,1,"COD Tk %s"' % (text_x(cod_line, "4", W, align), S(356), cod),
        "BAR %d,%d,%d,2" % (rx, S(412), bw),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(hub, "3", W, align), S(424), hub),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(today, "2", W, align), S(468), today),
        'QRCODE %d,%d,"L",3,"A",0,"%s"' % (qx, S(496), cid),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(cid, "2", W, align), S(590), cid),
        "PRINT 1,1",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii", errors="replace")


def build_std_tspl(p):
    """Standard sticker (default 48x80mm, dynamic SIZE + align).
    Same margin guarantees as pathao: barcode clearance, 30-char wrap,
    QR stacked above its caption so center/right never clips."""
    w_mm, h_mm, W, H, align = stock_dims(p)
    S = vscale(H)
    aw = wrap_chars_for(W)
    cid = tspl_text(p.get("consignment"), 20)
    cust = tspl_text(p.get("customer"), 30)
    phone = tspl_text(p.get("phone"), 20)
    addr = tspl_text(p.get("address"), aw * 3)
    cod = tspl_text(p.get("cod"), 12)
    hub = tspl_text(p.get("branch") or p.get("hub"), 24)
    addr_lines = (wrap(addr, aw, max_lines=3) + ["", "", ""])[:3]
    a1, a2, a3 = addr_lines
    bx, narrow = barcode_fit(cid, W, align)
    bw = bar_w(W)
    rx = bar_x(W, align)
    qx = qr_x(W, align, cell=3)
    lines = [
        "SIZE %d mm, %d mm" % (w_mm, h_mm),
        "GAP 2 mm, 0",
        "DIRECTION 1",
        "CLS",
    ] + ([border_line(W, H)] if border_on(p) else []) + [
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(cid, "3", W, align), S(16), cid),
        'BARCODE %d,%d,"128",64,1,0,%d,%d,"%s"' % (bx, S(56), narrow, narrow, cid),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(cust, "3", W, align), S(140), cust),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(phone, "2", W, align), S(188), phone),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(a1, "2", W, align), S(220), a1),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(a2, "2", W, align), S(252), a2),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(a3, "2", W, align), S(284), a3),
        "BAR %d,%d,%d,2" % (rx, S(324), bw),
        'TEXT %d,%d,"4",0,1,1,"COD Tk %s"' % (text_x("COD Tk " + cod, "4", W, align), S(336), cod) if cod else 'TEXT %d,%d,"4",0,1,1,""' % (text_x("", "4", W, align), S(336)),
        "BAR %d,%d,%d,2" % (rx, S(392), bw),
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(hub, "3", W, align), S(404), hub),
        'TEXT %d,%d,"2",0,1,1,"%s"' % (text_x(cid, "2", W, align), S(452), cid),
        'QRCODE %d,%d,"L",3,"A",0,"%s"' % (qx, S(484), cid),
        'TEXT %d,%d,"2",0,1,1,"COD Tk %s"' % (text_x("COD Tk " + cod, "2", W, align), S(578), cod) if cod else 'TEXT %d,%d,"2",0,1,1,""' % (text_x("", "2", W, align), S(578)),
    ]
    copies = p.get("copies") or 1
    try:
        copies = max(1, min(5, int(copies)))
    except (TypeError, ValueError):
        copies = 1
    lines.append("PRINT 1,%d" % copies)
    return ("\r\n".join(lines) + "\r\n").encode("ascii", errors="replace")


def _do_print(p):
    try:
        import serial
    except ImportError:
        return {"ok": False, "error": "pyserial missing on this Mac (pip install pyserial)"}
    if p.get("_tspl") is not None:
        tspl = bytes(p.get("_tspl") or b"")
        if not tspl:
            return {"ok": False, "error": "empty tspl"}
    else:
        if (p.get("template") or "pathao") in ("pathao", "std", "run") and not tspl_text(p.get("consignment"), 20):
            return {"ok": False, "error": "empty consignment id"}
        ox, oy = stock_offset(p)
        tspl = apply_offset(build_tspl(p), ox, oy)
    # Connection choice — `prefer` (payload or config, default "auto"):
    #   auto  -> the CONNECTED (preferred) device first, then Bluetooth,
    #            then cable. Bluetooth-first: the extension acts as the
    #            Bluetooth printer driver (no cable needed).
    #   bt    -> Bluetooth only (never touches a cable port).
    #   cable -> cable only.
    # An explicit message/config device still wins over everything.
    pref = str(p.get("prefer") or CONFIG.get("prefer") or "auto").strip().lower()
    if pref == "bluetooth":
        pref = "bt"
    if pref == "usb":
        pref = "cable"
    if pref not in ("auto", "bt", "cable"):
        pref = "auto"
    # Fast path: a warm port that doesn't contradict the choice skips
    # discovery + connect entirely (this is what makes back-to-back prints
    # instant on Windows Bluetooth SPP).
    warm_dev = _WARM.get("dev")
    if warm_dev and not p.get("device") and not CONFIG.get("device"):
        if pref == "auto" or conn_type(warm_dev) == pref:
            st = warm_try(tspl, warm_dev)
            if st:
                return st
    if p.get("device"):
        cands = [(p.get("device"), conn_type(p.get("device")))]
    elif CONFIG.get("device"):
        cands = [(CONFIG.get("device"), conn_type(CONFIG.get("device")))]
    else:
        cands = []
        if CONFIG.get("preferred"):
            pdev = CONFIG.get("preferred")
            pconn = conn_type(pdev)
            if pref == "auto" or pconn == pref:
                cands.append((pdev, pconn))
        disc = discover_printers()
        if pref in ("auto", "bt"):
            cands += [c for c in disc if c[1] == "bluetooth"]
        if pref in ("auto", "cable"):
            cands += [c for c in disc if c[1] == "cable"]
        seen, uniq_cands = set(), []
        for dev, conn in cands:
            if dev not in seen:
                seen.add(dev)
                uniq_cands.append((dev, conn))
        if not uniq_cands:
            dconn = conn_type(DEVICE_DEFAULT)
            if pref == "auto" or dconn == pref:
                uniq_cands = [(DEVICE_DEFAULT, dconn)]
        cands = uniq_cands
    if not cands:
        return {"ok": False, "error": "no %s printer found (pair the TL5X / check connection)" % pref}
    last = None
    tried = []
    for dev, conn in cands:
        st = warm_try(tspl, dev)  # covers the explicit-device case too
        if st:
            return st
        for baud in BAUDS:
            try:
                # write_timeout: a sleeping/offline BT printer must fail fast,
                # never hang the caller forever on a blocked write.
                s = serial.Serial(dev, baud, timeout=5, write_timeout=5)
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
                # Success — keep this port warm for the next print.
                warm_close()
                _WARM["dev"] = dev
                _WARM["baud"] = baud
                _WARM["ser"] = s
                _WARM["ts"] = time.time()
                return {"ok": True, "device": dev, "baud": baud,
                        "bytes": len(tspl), "conn": conn}
            except Exception as e:  # noqa: BLE001 - try next candidate
                last = "%s @%s: %s" % (dev, baud, e)
        tried.append(dev)
    return {"ok": False, "error": "no printer worked (tried %s): %s" % (tried, last)}


def do_print(p):
    """Serialize native and local-service prints on the single hot port."""
    with PRINT_LOCK:
        return _do_print(p)


def probe_device(dev):
    """Open+close at each baud without printing. (True, baud) or (False, err)."""
    try:
        import serial
    except ImportError:
        return False, "pyserial missing (pip install pyserial)"
    last = None
    for baud in BAUDS:
        try:
            s = serial.Serial(dev, baud, timeout=3, write_timeout=3)
            try:
                s.close()
            except Exception:
                pass
            return True, baud
        except Exception as e:  # noqa: BLE001
            last = e
    return False, "unreachable (%s): %s" % (dev, last)


def handle(msg):
    action = (msg.get("action") or "").strip()
    if action == "ping":
        return {"ok": True, "host": "db-print", "version": HOST_VERSION,
                "device": DEVICE_DEFAULT, "conn": conn_type(DEVICE_DEFAULT),
                "preferred": CONFIG.get("preferred") or "",
                "prefer": CONFIG.get("prefer", "auto"),
                "warm": bool(_WARM.get("ser")),
                "width_mm": CONFIG.get("width_mm", 48),
                "height_mm": CONFIG.get("height_mm", 80),
                "align": CONFIG.get("align", "left"),
                "offset_x_mm": CONFIG.get("offset_x_mm", 0),
                "offset_y_mm": CONFIG.get("offset_y_mm", 0),
                "border": bool(CONFIG.get("border", False)),
                "printers": [{"device": d, "conn": c} for d, c in discover_printers()]}
    if action == "driver_status":
        return driver_status()
    if action == "driver_test":
        return driver_test()
    if action == "driver_restart":
        return driver_restart()
    if action == "set_sticker_config":
        try:
            w = max(30, min(110, int(float(msg.get("width_mm", 48)))))
            h = max(20, min(150, int(float(msg.get("height_mm", 80)))))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad width/height"}
        a = str(msg.get("align", "left") or "left").strip().lower()
        if a == "centre":
            a = "center"
        if a not in ("left", "center", "right"):
            return {"ok": False, "error": "align must be left/center/right"}
        try:
            ox = max(-20, min(20, float(msg.get("offset_x_mm", 0))))
            oy = max(-20, min(20, float(msg.get("offset_y_mm", 0))))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad offset"}
        brd = (str(msg.get("border", "false")).strip().lower()
               in ("1", "true", "on", "yes"))
        patch = {"width_mm": w, "height_mm": h, "align": a,
                 "offset_x_mm": ox, "offset_y_mm": oy, "border": brd}
        new_prefer = None
        if msg.get("prefer") is not None:
            pv = str(msg.get("prefer")).strip().lower()
            if pv in ("bluetooth", "bt"):
                pv = "bt"
            elif pv in ("usb", "cable"):
                pv = "cable"
            else:
                pv = "auto"
            if pv != (CONFIG.get("prefer") or "auto"):
                # Re-evaluate the device on the next print (drop the warm
                # port so a stale choice can't keep printing).
                warm_close()
            patch["prefer"] = pv
            new_prefer = pv
        ok, err = save_config(patch)
        if not ok:
            return {"ok": False, "error": "config save failed: " + err}
        out = {"ok": True, "width_mm": w, "height_mm": h, "align": a,
               "offset_x_mm": ox, "offset_y_mm": oy, "border": brd}
        if new_prefer is not None:
            out["prefer"] = new_prefer
        return out
    if action == "connect_device":
        dev = tspl_text(msg.get("device"), 128)
        if not dev:
            return {"ok": False, "error": "empty device"}
        warm_close()  # never probe a port we are holding open ourselves
        ok, info = probe_device(dev)
        if not ok:
            return {"ok": False, "error": info, "device": dev}
        saved, err = save_config({"preferred": dev})
        if not saved:
            return {"ok": False, "error": "config save failed: " + err, "device": dev}
        return {"ok": True, "device": dev, "conn": conn_type(dev), "baud": info}
    if action == "print_label":
        if msg.get("dry_run"):
            if (msg.get("template") or "") == "calibrate":
                try:
                    pages, _ch_mm = render_calibrate_pages()
                    paths = []
                    imgs = []
                    for i, im in enumerate(pages):
                        path = "/tmp/db-calib-preview-%d.png" % i
                        im.convert("L").save(path)
                        paths.append(path)
                        b64 = pil_to_b64(im)
                        if b64:
                            imgs.append(b64)
                    return {"ok": True, "dry_run": True, "pages": len(paths),
                            "preview": paths, "images": imgs}
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": "render failed: %s" % e}
            if (msg.get("template") or "") in ("note", "letter"):
                try:
                    _w, _h, Wd, Hd, al = stock_dims(msg)
                    pages = render_note_pages(msg.get("title"), msg.get("body"),
                                              note_orientation(msg),
                                              letter=((msg.get("from"), msg.get("to"))
                                                      if (msg.get("template") or "") == "letter" else None),
                                              W=Wd, H=Hd, align=al)
                    paths = []
                    imgs = []
                    for i, im in enumerate(pages):
                        path = "/tmp/db-note-preview-%d.png" % i
                        im.convert("L").save(path)
                        paths.append(path)
                        b64 = pil_to_b64(im)
                        if b64:
                            imgs.append(b64)
                    return {"ok": True, "dry_run": True, "pages": len(paths),
                            "preview": paths, "images": imgs}
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": "render failed: %s" % e}
            if (msg.get("template") or "") in ("margins", "widthtest"):
                try:
                    _w, _h, Wd, Hd, _al = stock_dims(msg)
                    if (msg.get("template") or "") == "widthtest":
                        mpages = render_width_test_pages(Wd, _w)
                        tag = "widthtest"
                    else:
                        mpages = render_margin_test_pages(Wd, Hd, _w, _h)
                        tag = "margins"
                    paths, imgs = [], []
                    for i, im in enumerate(mpages):
                        path = "/tmp/db-%s-preview-%d.png" % (tag, i)
                        im.convert("L").save(path)
                        paths.append(path)
                        b64m = pil_to_b64(im)
                        if b64m:
                            imgs.append(b64m)
                    return {"ok": True, "dry_run": True, "pages": len(paths),
                            "preview": paths, "images": imgs}
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": "render failed: %s" % e}
            try:
                parcel_img = render_parcel_preview(msg)
                b64 = pil_to_b64(parcel_img)
            except Exception:  # noqa: BLE001 - preview best-effort only
                b64 = ""
            ox, oy = stock_offset(msg)
            tspl = apply_offset(build_tspl(msg), ox, oy)
            out = {"ok": True, "dry_run": True, "bytes": len(tspl),
                   "conn": conn_type(msg.get("device") or DEVICE_DEFAULT),
                   "tspl": tspl.decode("ascii", errors="replace")}
            if b64:
                out["images"] = [b64]
                out["pages"] = 1
            return out
        return do_print(msg)
    if action == "print_html":
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return {"ok": False, "error": "missing payload"}
        return print_html_label(payload)
    return {"ok": False, "error": "unknown action: %s" % action}


# ── Local print service: the site's own SPRT path ───────────────────────
# Hermes' central-print "SPRT" option does not send ZPL/TSPL. It builds the
# default 3x4in label as HTML and POSTs it to a local agent on port 55555
# (55556 on macOS). With the extension in the loop, this host *is* that
# agent: it renders the site's HTML with the installed Chrome (headless),
# converts the screenshot to a 1-bit bitmap and sends it to the TL5X over
# Bluetooth. No custom sticker template, no click interception — the site's
# own print/status flow runs exactly as it does on a cable setup.
LABEL_SERVICE_PORT = 55556 if sys.platform == "darwin" else 55555
LABEL_WIDTH_MM = 75
LABEL_HEIGHT_MM = 102
LABEL_W_DOTS = 600
LABEL_H_DOTS = 816
_HTTP_STARTED = False

# Minimal Tailwind subset used by the site's label templates. Kept local so
# the service works offline and cannot be broken by a Hermes asset deploy.
LABEL_CSS = r"""
html,body{margin:0;padding:0;background:#fff;}
*{box-sizing:border-box;}
body{font-family:Arial,Helvetica,sans-serif;color:#000;}
.flex{display:flex}.justify-between{justify-content:space-between}.justify-center{justify-content:center}.items-center{align-items:center}.flex-1{flex:1 1 0%}
.text-center{text-align:center}.font-semibold{font-weight:600}.font-medium{font-weight:500}.font-normal{font-weight:400}
.text-xs{font-size:12px}.text-sm{font-size:14px}.text-base{font-size:16px}.text-lg{font-size:18px}.text-xl{font-size:20px}.text-2xl{font-size:24px}
.leading-none{line-height:1}.leading-tight{line-height:1.25}.truncate{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.w-full{width:100%}.w-1\/2{width:50%}.w-1\/3{width:33.333333%}.w-2\/3{width:66.666667%}.w-1\/5{width:20%}.w-2\/5{width:40%}.w-3\/5{width:60%}.w-4\/5{width:80%}
.border{border:1px solid #000}.border-black{border-color:#000}.border-b{border-bottom:1px solid #000}.border-l{border-left:1px solid #000}
.bg-white{background:#fff}.bg-black{background:#000}.text-white{color:#fff}
.py-1{padding-top:4px;padding-bottom:4px}.py-2{padding-top:8px;padding-bottom:8px}
.px-1{padding-left:4px;padding-right:4px}.px-2{padding-left:8px;padding-right:8px}
.pl-1{padding-left:4px}.pb-1{padding-bottom:4px}.mb-1{margin-bottom:4px}.mb-2{margin-bottom:8px}
.mt-1{margin-top:4px}.mt-2{margin-top:8px}.mt-4{margin-top:16px}.mt-5{margin-top:20px}
.mr-1{margin-right:4px}.my-1{margin-top:4px;margin-bottom:4px}
.overflow-hidden{overflow:hidden}.h-11{height:44px}.h-16{height:64px}.min-w-0{min-width:0}
table{border-collapse:collapse}th,td{padding:0}.table-fixed{table-layout:fixed}.border-collapse{border-collapse:collapse}
.self-center{align-self:center}
@page{size:75mm 102mm;margin:0}
"""


def _barcode_data_url(text):
    """Code128 PNG as a data URI (the site's local agent replaces the
    __barcode__ placeholder with the real barcode)."""
    text = str(text or "").strip()
    if not text:
        return ""
    try:
        import barcode as _barcode
        from barcode.writer import ImageWriter
        obj = _barcode.get("code128", text, writer=ImageWriter())
        img = obj.render(writer_options={
            "write_text": False,
            "module_height": 8.0,
            "module_width": 0.25,
            "quiet_zone": 1.0,
            "dpi": 300,
        })
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def _qr_data_url(text):
    text = str(text or "").strip()
    if not text:
        return ""
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=8, border=2,
                           error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").get_image().convert("L")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def _build_label_html(payload):
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty label content")
    bc = payload.get("barcodeProperties") or {}
    qr = payload.get("qrProperties") or {}
    bc_text = str(bc.get("content") or "").strip() if isinstance(bc, dict) else ""
    qr_text = str(qr.get("content") or "").strip() if isinstance(qr, dict) else ""
    try:
        qr_w = max(32, min(200, int(qr.get("width") or 72)))
        qr_h = max(32, min(200, int(qr.get("height") or 72)))
    except (TypeError, ValueError):
        qr_w = qr_h = 72
    bc_url = _barcode_data_url(bc_text)
    qr_url = _qr_data_url(qr_text)
    if bc_url:
        content = content.replace(
            "__barcode__",
            '<img src="' + bc_url + '" style="height:36px;max-width:100%;display:block;">')
    if qr_url:
        content = content.replace(
            "__qr_code__",
            '<img src="' + qr_url + '" style="width:%dpx;height:%dpx;display:block;">' % (qr_w, qr_h))
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<style>' + LABEL_CSS + '</style></head>' + content + '</html>')


def _find_chrome():
    cands = [
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def _render_label_tspl(payload):
    """Render the site's HTML at 203dpi and return raw TSPL bitmap bytes."""
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("Chrome/Edge not found for label rendering")
    html = _build_label_html(payload)
    tmpdir = tempfile.mkdtemp(prefix="db-label-")
    html_path = os.path.join(tmpdir, "label.html")
    png_path = os.path.join(tmpdir, "label.png")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        url = "file:///" + urllib.parse.quote(
            os.path.abspath(html_path).replace("\\", "/"), safe="/:")
        args = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--user-data-dir=" + os.path.join(tmpdir, "profile"),
            "--screenshot=" + png_path,
            "--window-size=283,384",
            "--force-device-scale-factor=2.115",
            "--virtual-time-budget=2000",
            url,
        ]
        proc = subprocess.run(args, capture_output=True, timeout=45)
        if not os.path.isfile(png_path) or os.path.getsize(png_path) < 100:
            err = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
            raise RuntimeError("screenshot failed: " + (err or "no output"))
        from PIL import Image
        img = Image.open(png_path).convert("L")
        img = img.resize((LABEL_W_DOTS, LABEL_H_DOTS), Image.LANCZOS)
        img = img.point(lambda v: 0 if v < 160 else 255)
        out = [
            ("SIZE %d mm, %d mm" % (LABEL_WIDTH_MM, LABEL_HEIGHT_MM)).encode(),
            b"GAP 2 mm, 0",
            b"DIRECTION 1",
            b"CLS",
            pyt(img, LABEL_WIDTH_MM, LABEL_HEIGHT_MM),
            b"PRINT 1,1",
        ]
        return b"\r\n".join(out) + b"\r\n"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def print_html_label(payload):
    try:
        tspl = _render_label_tspl(payload)
    except Exception as e:  # noqa: BLE001 - report to the site
        return {"ok": False, "error": "label render failed: %s" % e}
    req = {"_tspl": tspl}
    dev = CONFIG.get("device") or CONFIG.get("preferred") or ""
    if dev:
        req["device"] = dev
    return do_print(req)


class _LabelHTTPHandler(http.server.BaseHTTPRequestHandler):
    server_version = "DataBridgePrint/1.0"

    def log_message(self, fmt, *args):
        pass  # stdout is the native-messaging channel

    def _cors(self):
        origin = self.headers.get("Origin") or ""
        if origin == "https://hermes.pathaointernal.com":
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        req_headers = self.headers.get("Access-Control-Request-Headers")
        self.send_header("Access-Control-Allow-Headers",
                         req_headers or "Content-Type, Authorization, X-Requested-With, "
                                        "X-CSRF-TOKEN, X-XSRF-TOKEN, Accept")
        self.send_header("Access-Control-Max-Age", "86400")
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/ping", "/health"):
            self._json(200, {"ok": True, "host": "db-print", "version": HOST_VERSION})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/print", "/print"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 2_000_000:
                self._json(400, {"ok": False, "error": "bad content length"})
                return
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            res = print_html_label(payload)
            self._json(200 if res.get("ok") else 500, res)
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(e)})


def _start_label_service():
    global _HTTP_STARTED
    if _HTTP_STARTED:
        return
    _HTTP_STARTED = True
    servers = []
    try:
        class _Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
        servers.append(_Server(("127.0.0.1", LABEL_SERVICE_PORT), _LabelHTTPHandler))
    except Exception as e:  # noqa: BLE001 - try IPv6 loopback below
        sys.stderr.write("db-print: IPv4 print service unavailable on %s: %s\n"
                         % (LABEL_SERVICE_PORT, e))
    try:
        class _Server6(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6
            daemon_threads = True
            allow_reuse_address = True
        servers.append(_Server6(("::1", LABEL_SERVICE_PORT), _LabelHTTPHandler))
    except Exception:
        pass  # IPv6 disabled or already covered by ::1
    if not servers:
        sys.stderr.write("db-print: local print service unavailable on port %s\n"
                         % LABEL_SERVICE_PORT)
        return
    for i, srv in enumerate(servers):
        t = threading.Thread(target=srv.serve_forever,
                             name="db-print-http-%d" % i, daemon=True)
        t.start()


def main():
    _start_label_service()
    while True:
        try:
            msg = read_msg()
        except Exception as e:  # noqa: BLE001 - malformed frame
            write_msg({"ok": False, "error": "bad frame: %s" % e})
            continue
        if msg is None:
            break  # EOF: Chrome closed the port
        try:
            res = handle(msg)
        except Exception as e:  # noqa: BLE001 - never die on one request
            res = {"ok": False, "error": str(e)}
        try:
            # Echo the request id: the extension keeps ONE host process for
            # every call (persistent port), so replies are matched by id.
            if isinstance(res, dict) and isinstance(msg, dict) and msg.get("id") is not None:
                res = dict(res)
                res["id"] = msg.get("id")
            write_msg(res)
        except Exception:
            try:
                write_msg({"ok": False, "error": "response encode failed",
                           "id": (msg.get("id") if isinstance(msg, dict) else None)})
            except Exception:
                break


if __name__ == "__main__":
    # --selftest: used by the installers to verify the host without Chrome
    # and without spending a sticker (ping only, no serial touch).
    if "--selftest" in sys.argv[1:]:
        print(json.dumps(handle({"action": "ping"})))
        sys.exit(0)
    main()
