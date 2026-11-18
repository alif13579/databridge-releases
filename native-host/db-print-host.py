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

Run the host with --serve to keep the local print service (55555/55556)
alive in the background even when Chrome/Edge is closed. All printing is
local to one machine; nothing is exposed to the network.
"""
import sys
import os
import re
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
import urllib.request
import urllib.error


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
        # Atomic replace so the running coordinator's mtime reload can never
        # read a half-written JSON file.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
        global CONFIG
        CONFIG = d
        try:
            _CONFIG_MTIME["ts"] = os.path.getmtime(path)
        except Exception:
            pass
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
    if os.name == "nt":
        try:
            if dev in _win_usb_printer_map():
                return "cable"
        except Exception:  # noqa: BLE001
            pass
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
    if os.name == "nt":
        # A Windows Bluetooth SPP port is just "COM5" — ask the port list.
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                if p.device == dev:
                    desc = ((p.description or "") + " " + (p.hwid or "")).lower()
                    if "bthenum" in desc or "bluetooth" in desc:
                        return "bluetooth"
                    return "cable"
        except Exception:
            pass
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


CODE93_CHARS = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%")


def code128_modules(n):
    """Total Code128 modules (worst case subset B): start(11) + data(11/char) + check(11) + stop(13)."""
    return 11 * int(n) + 35


def code128_modules_min(cid):
    """Best-case Code128 module count, using subset C for digit runs so
    numeric IDs get the widest possible bars."""
    s = str(cid or "")
    if not s.isdigit():
        return code128_modules(len(s))
    n = len(s)
    # subset C: one symbol per digit pair; odd length needs one subset-B char + switch
    pairs = n // 2
    odd = n % 2
    return 11 * (pairs + (1 if odd else 0) + (1 if odd else 0)) + 35


def code93_modules(n):
    """Total Code93 modules: start/stop(9+9) + data(9/char) + checks(9+9) + term(1)."""
    return 9 * int(n) + 37


def barcode_wide(sym, narrow):
    """TSPL BARCODE wide element. Use 1:3 ratio for all symbologies — wider
    bars/gaps survive thermal ink spread and scan reliably."""
    return int(narrow) * 3


def barcode_plan(cid, W, align, inset=0, quiet_modules=10):
    """(symbology, x, narrow) for a barcode that a scanner can actually read.

    A 14-char Pathao ID is 189 Code128 modules = 378 dots at narrow=2 on a
    384-dot (48 mm) stock: edge to edge, with no quiet zone at all, so the
    start/stop patterns sit inside the label tolerance and scans fail.
    Code93 carries the same text in 163 modules (326 dots) and leaves
    ~3.6 mm of white on each side. Prefer Code128 whenever it fits with a
    full 10-module quiet zone; fall back to Code93 (same text, Zebra/TSPL
    scanners decode it) when Code128 is too wide. Never clip at the edge.
    """
    cid = str(cid or "")
    n = len(cid)

    def place(modules, narrow, qm=None):
        bw = modules * narrow
        qz = max(int(inset or 0), (qm if qm is not None else quiet_modules) * narrow)
        if bw + 2 * qz > W:
            return None
        if align == "center":
            x = (W - bw) // 2
        elif align == "right":
            x = W - qz - bw
        else:
            x = qz
        return max(0, int(x)), int(narrow)

    if n:
        c128_mods = code128_modules_min(cid)
        for qm in (quiet_modules, max(4, quiet_modules // 2)):
            for narrow in (4, 3, 2):
                p = place(c128_mods, narrow, qm)
                if p:
                    return "128", p[0], p[1]
        if all(c in CODE93_CHARS for c in cid):
            for qm in (quiet_modules, max(4, quiet_modules // 2)):
                for narrow in (4, 3, 2):
                    p = place(code93_modules(n), narrow, qm)
                    if p:
                        return "93", p[0], p[1]
        # Last resort: Code128 at 1 dot, reduced quiet zone.
        p = place(c128_mods, 1, max(4, quiet_modules // 2))
        if p:
            return "128", p[0], p[1]
    return "128", int(inset or 0), 2


def barcode_fit(cid, W, align, margin=3):
    """Back-compat (tests / old callers): (x, narrow) from barcode_plan()."""
    _sym, x, narrow = barcode_plan(cid, W, align)
    return x, narrow


def barcode_x(cid, W, align, narrow=2, margin=6):
    """Back-compat wrapper (tests / old callers). Prefers narrow=2 fit."""
    x, _ = barcode_fit(cid, W, align, margin=margin)
    return x


def tspl_layout(tspl, extra=None):
    """Small JSON-able summary of what the TSPL actually prints.

    The extension stores this in its layout log: SIZE/BARCODE are the real
    numbers after bitmap/font fitting, so "too small / too big" can be
    explained without guessing from the form values.
    """
    out = {"host": HOST_VERSION}
    try:
        txt = (tspl or b"").decode("ascii", "replace")
    except Exception:
        txt = ""
    m = re.search(
        r"(?mi)^SIZE\s+([0-9]+(?:\.[0-9]+)?)\s*mm\s*,\s*([0-9]+(?:\.[0-9]+)?)\s*mm",
        txt)
    if m:
        w_mm = float(m.group(1))
        h_mm = float(m.group(2))
        out["label_mm"] = [round(w_mm, 1), round(h_mm, 1)]
        out["label_dots"] = [int(round(w_mm * 8)), int(round(h_mm * 8))]
    bm = None
    for bm in re.finditer(
            r'(?mi)^BARCODE\s+(-?\d+),(-?\d+),"([^"]*)",(\d+),(\d+),(\d+),(\d+),(\d+)(?:,"([^"]*)")?',
            txt):
        pass
    if bm:
        x = int(bm.group(1))
        sym = bm.group(3).upper()
        narrow = int(bm.group(7))
        data = bm.group(9) or ""
        bc = {
            "sym": sym,
            "x": x,
            "y": int(bm.group(2)),
            "h_dots": int(bm.group(4)),
            "narrow": narrow,
            "wide": int(bm.group(8)),
            "narrow_mm": round(narrow / 8.0, 2),
            "data_len": len(data),
        }
        try:
            if sym == "93":
                mods = code93_modules(len(data))
            elif sym == "128":
                mods = code128_modules_min(data)
            else:
                mods = None
            if mods:
                wdots = mods * narrow
                bc["modules"] = mods
                bc["width_dots"] = wdots
                bc["width_mm"] = round(wdots / 8.0, 1)
                bc["right_dots"] = x + wdots
                if "label_dots" in out:
                    bc["fits"] = (x + wdots) <= out["label_dots"][0]
        except Exception:
            pass
        out["barcode"] = bc
    if extra:
        try:
            out.update(extra)
        except Exception:
            pass
    return out


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
    """All candidate label printers as [(device, conn)] — cable listed first.
    Callers decide which transport to try first. Unknown devices are skipped
    (never poke random hardware)."""
    found = []
    try:
         if os.name == "nt":
             from serial.tools import list_ports
             for p in list_ports.comports():
                 desc = ((p.description or "") + " " + (p.device or "")).lower()
                 if "bluetooth" in desc or "bt" in desc or "rfcomm" in desc:
                     found.append((p.device, "bluetooth"))
                 else:
                     # Treat every non-Bluetooth serial port as a cable candidate
                     # (TL5X over USB often shows up as a plain COM port whose
                     # description lacks "usb", e.g. "Serial Port (COM5)").
                     found.append((p.device, "cable"))
             # USB printer-class models (e.g. Spirit SPRT) have no COM port;
             # they print through the Windows spooler in RAW mode.
             try:
                 for qname in _win_usb_printer_map():
                     found.append((qname, "cable"))
             except Exception:  # noqa: BLE001
                 pass
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


# Cable candidates are ranked by how strongly Windows describes them as a
# USB serial bridge / printer. Rank 0 = clearly not a printer (phone, modem,
# GPS...), rank 2 = known bridge, rank 1 = unknown plain serial port.
_CABLE_RANK_CACHE = {"ts": 0.0, "map": {}}
_CABLE_GOOD = (
    "ch340", "ch341", "cp210", "ft232", "ftdi", "prolific", "pl2303",
    "usb serial", "usb-serial", "uart", "tl5x", "sprt", "printer",
    "receipt", "pos printer", "label",
)
_CABLE_BAD = (
    "samsung", "android", "mtp", "adb", "modem", "phone", "iphone",
    "ipad", "huawei", "xiaomi", "oppo", "vivo", "realme", "oneplus",
    "qualcomm", "diagnostic", "gps", "wwan", "lte", "cdc acm",
)
_CABLE_GOOD_VIDS = (0x1A86, 0x10C4, 0x0403, 0x067B, 0x2341, 0x1FC9)


def _cable_rank_map():
    now = time.time()
    if now - float(_CABLE_RANK_CACHE.get("ts") or 0.0) < 10.0:
        return _CABLE_RANK_CACHE.get("map") or {}
    ranks = {}
    if os.name == "nt":
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                desc = " ".join(str(x or "") for x in (
                    getattr(p, "description", None),
                    getattr(p, "manufacturer", None),
                    getattr(p, "product", None),
                    getattr(p, "hwid", None),
                )).lower()
                try:
                    vid = int(getattr(p, "vid", 0) or 0)
                except Exception:  # noqa: BLE001
                    vid = 0
                if any(b in desc for b in _CABLE_BAD):
                    rank = 0
                elif any(g in desc for g in _CABLE_GOOD) or vid in _CABLE_GOOD_VIDS:
                    rank = 2
                else:
                    rank = 1
                ranks[p.device] = rank
        except Exception:  # noqa: BLE001
            pass
    _CABLE_RANK_CACHE.update(ts=now, map=ranks)
    return ranks


def _cable_rank(dev):
    # A live Windows spooler queue is the strongest cable signal: prefer it
    # over raw COM ports, which can open successfully while printing nowhere.
    if os.name == "nt":
        try:
            if dev in _win_usb_printer_map():
                return 3
        except Exception:  # noqa: BLE001
            pass
    ranks = _cable_rank_map()
    if dev in ranks:
        return ranks[dev]
    return 2 if os.name != "nt" else 1


# USB label printers that use the Windows printer class (no COM port) are
# printed through the spooler with raw TSPL. Queue names are matched against
# this whitelist so office laser printers are never picked.
_USB_PRINT_GOOD = (
    "tl5x", "sprt", "spirit", "label", "thermal", "barcode", "tspl",
    "zebra", "xprinter", "godex", "tsc", "citizen", "argox", "postek",
    "hprt",
)
_USB_PRINT_BAD = (
    "brother", "laser", "inkjet", "deskjet", "officejet", "pdf", "onenote",
    "fax", "xps", "canon", "epson", "hewlett", "hp ",
)
_USB_PRINT_CACHE = {"ts": 0.0, "map": {}}


def _win_usb_port_present(port):
    """True when the USB printer device behind a spooler port is plugged in."""
    try:
        import ctypes
        import winreg
        key = (r"SYSTEM\CurrentControlSet\Control\Print\Monitors"
               r"\USB Monitor\Ports\%s" % port)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            devid = str(winreg.QueryValueEx(k, "Device Id")[0] or "")
        if not devid:
            return False
        cfg = ctypes.WinDLL("cfgmgr32")
        cfg.CM_Locate_DevNodeW.argtypes = [
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p, ctypes.c_uint32]
        cfg.CM_Locate_DevNodeW.restype = ctypes.c_uint32
        inst = ctypes.c_uint32()
        return cfg.CM_Locate_DevNodeW(
            ctypes.byref(inst), devid, 0) == 0
    except Exception:  # noqa: BLE001
        return False


def _win_usb_printer_map():
    """{queue name: USB port} for present USB label printers."""
    if os.name != "nt":
        return {}
    now = time.time()
    if now - float(_USB_PRINT_CACHE.get("ts") or 0.0) < 3.0:
        return dict(_USB_PRINT_CACHE.get("map") or {})
    out = {}
    try:
        import winreg
        root = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Print\Printers")
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if not name or name.startswith("S-1-5-") or ":" in name:
                continue
            try:
                sub = winreg.OpenKey(root, name)
            except OSError:
                continue
            def val(v):
                try:
                    return str(winreg.QueryValueEx(sub, v)[0] or "")
                except OSError:
                    return ""
            port = val("Port")
            blob = " ".join((name, val("Printer Driver"),
                             val("Description"))).lower()
            if not port.upper().startswith("USB"):
                continue
            if any(b in blob for b in _USB_PRINT_BAD):
                continue
            if not any(g in blob for g in _USB_PRINT_GOOD):
                continue
            if not _win_usb_port_present(port):
                continue
            out[name] = port
        winreg.CloseKey(root)
    except Exception:  # noqa: BLE001
        pass
    _USB_PRINT_CACHE.update(ts=now, map=dict(out))
    return out


def _win_raw_print(queue, data):
    """Send raw TSPL bytes to a Windows printer queue (USB printer class)."""
    if os.name != "nt":
        raise OSError("Windows spooler only")
    import ctypes
    from ctypes import wintypes
    winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

    class DOC_INFO_1W(ctypes.Structure):
        _fields_ = [
            ("pDocName", wintypes.LPWSTR),
            ("pOutputFile", wintypes.LPWSTR),
            ("pDatatype", wintypes.LPWSTR),
        ]

    winspool.OpenPrinterW.argtypes = [
        wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    winspool.OpenPrinterW.restype = wintypes.BOOL
    winspool.StartDocPrinterW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(DOC_INFO_1W)]
    winspool.StartDocPrinterW.restype = wintypes.DWORD
    winspool.StartPagePrinter.argtypes = [wintypes.HANDLE]
    winspool.StartPagePrinter.restype = wintypes.BOOL
    winspool.WritePrinter.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    winspool.WritePrinter.restype = wintypes.BOOL
    winspool.EndPagePrinter.argtypes = [wintypes.HANDLE]
    winspool.EndDocPrinter.argtypes = [wintypes.HANDLE]
    winspool.ClosePrinter.argtypes = [wintypes.HANDLE]
    h = wintypes.HANDLE()
    if not winspool.OpenPrinterW(str(queue), ctypes.byref(h), None):
        raise OSError("OpenPrinter failed (%d)" % ctypes.get_last_error())
    try:
        di = DOC_INFO_1W("DataBridge", None, "RAW")
        if not winspool.StartDocPrinterW(h, 1, ctypes.byref(di)):
            raise OSError("StartDocPrinter failed (%d)"
                          % ctypes.get_last_error())
        page = False
        try:
            # RAW jobs do not require page brackets, and some drivers reject
            # StartPagePrinter. Treat them as best effort instead of failing
            # the whole print.
            page = bool(winspool.StartPagePrinter(h))
            buf = ctypes.create_string_buffer(bytes(data), len(data))
            written = wintypes.DWORD(0)
            if not winspool.WritePrinter(h, buf, len(data), ctypes.byref(written)):
                raise OSError("WritePrinter failed (%d)"
                              % ctypes.get_last_error())
            if written.value != len(data):
                raise OSError("short write %d/%d"
                              % (written.value, len(data)))
        finally:
            if page:
                winspool.EndPagePrinter(h)
            winspool.EndDocPrinter(h)
    finally:
        winspool.ClosePrinter(h)


_PRINTER_NAME_CACHE = {}


def printer_name(dev):
    """Human-friendly printer name for a device path (ledger + UI).

    Tries the paired Bluetooth name, then the Windows COM description, then
    a cleaned-up device basename. Never raises, always returns a short string.
    Cached 60s so repeated status calls never re-run system_profiler.
    """
    dev = str(dev or "")
    if not dev:
        return ""
    ent = _PRINTER_NAME_CACHE.get(dev)
    if ent and time.time() - ent[0] < 60:
        return ent[1]

    def resolve():
        # Paired/connected Bluetooth names (macOS system_profiler, cached).
        try:
            connected, paired = bt_link_lists()
            for n in (connected or []) + (paired or []):
                if bt_name_connected(dev, [n]):
                    return str(n)[:60]
        except Exception:
            pass
        # Windows SPP COM port → paired device name / USB description.
        if os.name == "nt":
            try:
                n = _win_com_bt_name(dev)
                if n:
                    return str(n)[:60]
            except Exception:
                pass
            try:
                from serial.tools import list_ports
                for p in list_ports.comports():
                    if p.device == dev and p.description:
                        return str(p.description)[:60]
            except Exception:
                pass
        # Fallback: "cu.TL5XBTPrinter" → "TL5XBTPrinter".
        base = os.path.basename(dev)
        return (base[3:] if base.startswith("cu.") else base)[:60]

    name = resolve()
    _PRINTER_NAME_CACHE[dev] = (time.time(), name)
    return name


# ── Real connection status ───────────────────────────────────────────────
# macOS keeps /dev/cu.TL5XBTPrinter around while the printer is paired even
# when it is powered off — so "a device node exists" is NOT "connected".
# That false positive made the popup show 🟢 Connected with the printer off.
# Bluetooth link state comes from system_profiler (device_connected vs
# device_not_connected); cable/Windows ports are checked by opening them.
_BT_CACHE = {"at": 0.0, "connected": [], "paired": []}


def _norm_name(s):
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def bt_link_lists():
    """(connected, paired) Bluetooth device names on macOS, cached 5s.
    Returns (None, None) when the platform/profiler can't answer."""
    if sys.platform != "darwin":
        return None, None
    if _BT_CACHE["at"] and time.time() - _BT_CACHE["at"] < 5:
        return _BT_CACHE["connected"], _BT_CACHE["paired"]
    try:
        out = subprocess.run(
            ["/usr/sbin/system_profiler", "SPBluetoothDataType", "-json"],
            capture_output=True, text=True, timeout=8).stdout
        data = json.loads(out)
    except Exception:
        return None, None
    connected, paired = [], []

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if key in ("device_connected", "device_not_connected") and isinstance(val, list):
                    target = connected if key == "device_connected" else paired
                    for item in val:
                        if isinstance(item, dict):
                            target.extend(str(n) for n in item.keys())
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    _BT_CACHE.update(at=time.time(), connected=connected, paired=paired)
    return connected, paired


def bt_name_connected(dev, names):
    base = os.path.basename(str(dev or ""))
    if base.startswith("cu."):
        base = base[3:]
    want = _norm_name(base)
    if not want:
        return False
    for name in names or []:
        n = _norm_name(name)
        if n == want:
            return True
        if len(n) >= 5 and (n in want or want in n):
            return True
    return False


# ── Windows Bluetooth link state ─────────────────────────────────────────
# A paired-but-off printer still leaves its SPP COM port in the port list, and
# opening that phantom port can succeed without printing anywhere. Ask the
# Windows Bluetooth stack which devices are actually connected, then map the
# COM port back to its device name via BTHENUM + BTHPORT registry keys.
_WIN_BT_CACHE = {"at": 0.0, "names": None}


def _win_bt_connected_names():
    """Normalised names of connected Bluetooth devices, or None when the
    Windows Bluetooth API is unavailable (callers then fall back to probing)."""
    if os.name != "nt":
        return None
    if _WIN_BT_CACHE["at"] and time.time() - _WIN_BT_CACHE["at"] < 5:
        return _WIN_BT_CACHE["names"]
    names = None
    try:
        import ctypes
        from ctypes import wintypes

        class SYSTEMTIME(ctypes.Structure):
            _fields_ = [("wYear", wintypes.WORD), ("wMonth", wintypes.WORD),
                        ("wDayOfWeek", wintypes.WORD), ("wDay", wintypes.WORD),
                        ("wHour", wintypes.WORD), ("wMinute", wintypes.WORD),
                        ("wSecond", wintypes.WORD), ("wMilliseconds", wintypes.WORD)]

        class BLUETOOTH_ADDRESS(ctypes.Structure):
            _fields_ = [("ullLong", ctypes.c_ulonglong)]

        class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("Address", BLUETOOTH_ADDRESS),
                        ("ulClassofDevice", wintypes.ULONG),
                        ("fConnected", wintypes.BOOL),
                        ("fRemembered", wintypes.BOOL),
                        ("fAuthenticated", wintypes.BOOL),
                        ("stLastSeen", SYSTEMTIME),
                        ("stLastUsed", SYSTEMTIME),
                        ("szName", wintypes.WCHAR * 248)]

        class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("fReturnAuthenticated", wintypes.BOOL),
                        ("fReturnRemembered", wintypes.BOOL),
                        ("fReturnUnknown", wintypes.BOOL),
                        ("fReturnConnected", wintypes.BOOL),
                        ("fIssueInquiry", wintypes.BOOL),
                        ("cTimeoutMultiplier", ctypes.c_ubyte),
                        ("hRadio", wintypes.HANDLE)]

        bth = None
        for dll in ("bthprops.cpl", "irprops.cpl"):
            try:
                bth = ctypes.WinDLL(dll)
                break
            except Exception:
                bth = None
        if bth is None:
            raise OSError("bluetooth API unavailable")
        bth.BluetoothFindFirstDevice.argtypes = [
            ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS),
            ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
        bth.BluetoothFindFirstDevice.restype = wintypes.HANDLE
        bth.BluetoothFindNextDevice.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
        bth.BluetoothFindNextDevice.restype = wintypes.BOOL
        bth.BluetoothFindDeviceClose.argtypes = [wintypes.HANDLE]

        params = BLUETOOTH_DEVICE_SEARCH_PARAMS()
        params.dwSize = ctypes.sizeof(params)
        params.fReturnAuthenticated = True
        params.fReturnRemembered = True
        params.fReturnUnknown = False
        params.fReturnConnected = True
        params.fIssueInquiry = False
        params.cTimeoutMultiplier = 0
        info = BLUETOOTH_DEVICE_INFO()
        info.dwSize = ctypes.sizeof(info)
        found = set()
        h = bth.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
        if h:
            try:
                while True:
                    if info.fConnected:
                        n = _norm_name(info.szName)
                        if n:
                            found.add(n)
                    info.dwSize = ctypes.sizeof(info)
                    if not bth.BluetoothFindNextDevice(h, ctypes.byref(info)):
                        break
            finally:
                try:
                    bth.BluetoothFindDeviceClose(h)
                except Exception:
                    pass
        names = found
    except Exception:
        names = None
    _WIN_BT_CACHE.update(at=time.time(), names=names)
    return names


def _win_bt_name_from_mac(mac):
    try:
        import winreg
        path = (r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters"
                r"\Devices\%s" % str(mac).lower())
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            for value in ("Name", "FriendlyName"):
                try:
                    data, _typ = winreg.QueryValueEx(k, value)
                except OSError:
                    continue
                txt = (data.decode("utf-8", "ignore")
                       if isinstance(data, bytes) else str(data))
                txt = txt.replace("\x00", "").strip()
                if txt:
                    return txt
    except Exception:
        pass
    return None


_WIN_COM_NAME_CACHE = {}


def _win_com_bt_name(dev):
    """Cached COM-port → Bluetooth device-name lookup (registry scan is cheap
    but not free; pairing changes are picked up within 30s)."""
    port = str(dev or "").strip().upper()
    if not port:
        return None
    ent = _WIN_COM_NAME_CACHE.get(port)
    if ent and time.time() - ent[0] < 30:
        return ent[1]
    name = _win_com_bt_name_raw(dev)
    _WIN_COM_NAME_CACHE[port] = (time.time(), name)
    return name


def _win_com_bt_name_raw(dev):
    """Map a Bluetooth SPP COM port (e.g. COM5) to its paired device name.
    None when the registry layout cannot be resolved (caller falls back)."""
    if os.name != "nt":
        return None
    try:
        import re as _re
        import winreg
        port = str(dev or "").strip().upper()
        if not port:
            return None
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SYSTEM\CurrentControlSet\Enum\BTHENUM")
    except Exception:
        return None
    try:
        services = []
        i = 0
        while True:
            try:
                services.append(winreg.EnumKey(root, i))
            except OSError:
                break
            i += 1
        for svc in services:
            try:
                skey = winreg.OpenKey(root, svc)
            except OSError:
                continue
            try:
                j = 0
                while True:
                    try:
                        inst = winreg.EnumKey(skey, j)
                    except OSError:
                        break
                    j += 1
                    try:
                        ikey = winreg.OpenKey(skey, inst)
                    except OSError:
                        continue
                    try:
                        try:
                            pn, _ = winreg.QueryValueEx(ikey, "PortName")
                        except OSError:
                            pn = None
                        if pn and str(pn).strip().upper() == port:
                            m = _re.search(r"&([0-9A-Fa-f]{12})(?:_[0-9A-Fa-f]+)?$",
                                           inst)
                            if m:
                                name = _win_bt_name_from_mac(m.group(1))
                                if name:
                                    return name
                            try:
                                fn, _ = winreg.QueryValueEx(ikey, "FriendlyName")
                            except OSError:
                                fn = None
                            if fn:
                                fn = str(fn)
                                if "serial over bluetooth" not in fn.lower():
                                    return fn
                    finally:
                        try:
                            winreg.CloseKey(ikey)
                        except Exception:
                            pass
            finally:
                try:
                    winreg.CloseKey(skey)
                except Exception:
                    pass
    finally:
        try:
            winreg.CloseKey(root)
        except Exception:
            pass
    return None


def _win_bt_verify(dev):
    """(known, connected) for a Windows Bluetooth COM port.
    known=False means the stack could not answer — fall back to probing."""
    live = _win_bt_connected_names()
    if live is None:
        return False, False
    name = _win_com_bt_name(dev)
    want = _norm_name(name)
    if not want:
        return False, False
    for n in live:
        if n == want or (len(n) >= 5 and (n in want or want in n)):
            return True, True
    return True, False


def _bt_known_dead(dev):
    """True only when Windows positively knows a BT port is offline."""
    if os.name != "nt":
        return False
    try:
        known, live = _win_bt_verify(dev)
        return bool(known and not live)
    except Exception:  # noqa: BLE001
        return False


def device_connected(dev):
    """(connected, reason) — real reachability, not just a /dev node.
    reason: warm | bluetooth | cable | paired-not-connected | not-found |
            unreachable | node | unknown."""
    dev = dev or DEVICE_DEFAULT
    if _WARM.get("ser") is not None and _WARM.get("dev") == dev:
        return True, "warm"
    if os.name == "nt":
        try:
            if dev in _win_usb_printer_map():
                return True, "cable"
        except Exception:  # noqa: BLE001
            pass
    ctype = conn_type(dev)
    if ctype == "bluetooth" and sys.platform == "darwin":
        live, paired = bt_link_lists()
        if live is None:
            return True, "unknown"  # can't verify — never claim offline
        if bt_name_connected(dev, live):
            return True, "bluetooth"
        if bt_name_connected(dev, paired):
            return False, "paired-not-connected"
        return False, "not-found"
    if ctype == "bluetooth" and os.name == "nt":
        known, live = _win_bt_verify(dev)
        if known:
            return (True, "bluetooth") if live else (False, "paired-not-connected")
    if ctype == "cable":
        if os.name == "nt":
            # "COM8" is not a filesystem path — os.path.exists() lies on
            # Windows. A quick open/close is the only real reachability test.
            ok, _info = probe_device(dev)
            return (True, "cable") if ok else (False, "not-found")
        return (True, "cable") if os.path.exists(dev) else (False, "not-found")
    if os.name == "nt":
        ok, info = probe_device(dev)
        return (True, str(info)) if ok else (False, "unreachable")
    return os.path.exists(dev), "node"


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
    bt_linked = [d for d in bt_dev if device_connected(d)[0]]
    return {
        "ok": True, "supported": True, "platform": "darwin",
        "daemon": daemon, "queue": queue,
        "port": {"listening": listening, "port": DAEMON_PORT},
        "bt_devices": bt_dev,
        "bt_linked": bt_linked,
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
HOST_VERSION = 11

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
    bc_type, bar_x, narrow = barcode_plan(cid, W, align, inset=ins)
    bar_y = S(96)
    out = [("SIZE %d mm, %d mm" % (w_mm, h_mm)).encode(),
           b"GAP 2 mm, 0", b"DIRECTION 1", b"CLS",
           pyt(canvas, w_mm, h_mm),
           ('BARCODE %d,%d,"%s",%d,1,0,%d,%d,"%s"'
            % (bar_x, bar_y, bc_type, int(48 * k), narrow,
               barcode_wide(bc_type, narrow), cid)).encode(),
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
        # fake barcode: alternating bars drawn at the SAME x/width/narrow the
        # firmware barcode will use, so the preview shows the real white gap.
        y = S(yb)
        sym, x0, narrow = barcode_plan(label, W, align)
        mods = code93_modules(len(label)) if sym == "93" else code128_modules(len(label))
        bw = min(W, mods * narrow)
        for i in range(0, bw, 4):
            dr.rectangle([min(x0 + i, W - 1), y, min(x0 + i + 2, W - 1), y + h], fill=0)
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
    bc_type, bx, narrow = barcode_plan(cid, W, align)
    bw = bar_w(W)
    rx = bar_x(W, align)
    lines = [
        "SIZE %d mm, %d mm" % (w_mm, h_mm),
        "GAP 2 mm, 0",
        "DIRECTION 1",
        "CLS",
        'TEXT %d,%d,"3",0,1,1,"%s"' % (text_x(cid, "3", W, align), S(16), cid),
        'BARCODE %d,%d,"%s",48,1,0,%d,%d,"%s"' % (bx, S(56), bc_type, narrow, barcode_wide(bc_type, narrow), cid),
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
    bc_type, bx, narrow = barcode_plan(cid, W, align)
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
        'BARCODE %d,%d,"%s",48,1,0,%d,%d,"%s"' % (bx, S(92), bc_type, narrow, barcode_wide(bc_type, narrow), cid),
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
    bc_type, bx, narrow = barcode_plan(cid, W, align)
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
        'BARCODE %d,%d,"%s",48,1,0,%d,%d,"%s"' % (bx, S(56), bc_type, narrow, barcode_wide(bc_type, narrow), cid),
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


def _do_print(p, _meta=None):
    try:
        import serial
    except ImportError:
        return {"ok": False, "error": "pyserial missing (pip install pyserial)"}
    if p.get("_tspl") is not None:
        tspl = bytes(p.get("_tspl") or b"")
        if not tspl:
            return {"ok": False, "error": "empty tspl"}
    else:
        if (p.get("template") or "pathao") in ("pathao", "std", "run") and not tspl_text(p.get("consignment"), 20):
            return {"ok": False, "error": "empty consignment id"}
        ox, oy = stock_offset(p)
        tspl = apply_offset(build_tspl(p), ox, oy)
    if _meta is not None:
        _meta["tspl"] = tspl
    # One serial-port owner per laptop: if this process does not own the
    # local bridge (the autostart --serve instance does), hand the finished
    # bytes to it so two host processes never fight over the printer.
    if not _LOCAL_BRIDGE_BOUND:
        via = _forward_to_coordinator(tspl)
        if via is not None:
            return via
    # A paired printer can look unreachable for a moment while the Bluetooth
    # link wakes, so try the full candidate list before reporting failure.
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
        wconn = conn_type(warm_dev)
        if pref == "auto" and _bt_known_dead(warm_dev):
            pass  # sleeping BT printer: don't stall, fall through to cable
        elif pref == "auto" or wconn == pref:
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
            dead_bt = pref == "auto" and _bt_known_dead(pdev)
            if (pref == "auto" or pconn == pref) and not dead_bt:
                cands.append((pdev, pconn))
        disc = discover_printers()
        if os.name == "nt":
            # Drop Bluetooth COM ports Windows positively reports as
            # disconnected: the node stays listed while the printer sleeps and
            # a write to it "succeeds" without printing anywhere.
            filtered = []
            for d, c in disc:
                if c == "bluetooth":
                    known, live = _win_bt_verify(d)
                    if known and not live:
                        continue
                filtered.append((d, c))
            disc = filtered
        if pref in ("auto", "bt"):
            cands += [c for c in disc if c[1] == "bluetooth"]
        if pref in ("auto", "cable"):
            cables = [c for c in disc if c[1] == "cable" and _cable_rank(c[0]) > 0]
            cables.sort(key=lambda c: -_cable_rank(c[0]))
            cands += cables
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
    errors = []
    for dev, conn in cands:
        if os.name == "nt":
            try:
                umap = _win_usb_printer_map()
            except Exception:  # noqa: BLE001
                umap = {}
            is_queue = dev in umap
            if not is_queue and not re.match(r"^(\\\\\.\\)?COM\d+$",
                                              str(dev), re.I):
                # A saved spooler queue name (no COM prefix) still prints
                # through the spooler even if the 3s discovery cache missed
                # it. A real failure below is reported, never hidden.
                is_queue = True
            if is_queue:
                try:
                    _win_raw_print(dev, tspl)
                    return {"ok": True, "device": dev, "conn": "cable",
                            "bytes": len(tspl), "port": umap.get(dev, "")}
                except Exception as e:  # noqa: BLE001
                    last = "%s: %s" % (dev, e)
                    errors.append(last)
                tried.append(dev)
                continue
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
                errors.append(last)
        tried.append(dev)
    detail = "; ".join(errors[-6:]) if errors else (last or "no candidate answered")
    return {"ok": False, "error": "no printer worked (tried %s): %s" % (tried, detail)}


def do_print(p):
    """Serialize native and local-service prints on the single hot port.

    Also records one ledger entry per job (origin node, consignment, printer,
    result, timing) and exposes the in-flight job via _CURRENT_JOB.
    """
    _maybe_reload_config()
    global _JOB_SEQ, _CURRENT_JOB
    with _JOBS_LOCK:
        _JOB_SEQ += 1
        started = time.time()
        job = {
            "id": "j%d-%d" % (int(started), _JOB_SEQ),
            "ts": round(started, 1),
            "origin": NODE_NAME,
            "node": NODE_NAME,
            "consignment": str(p.get("consignment") or p.get("_consignment") or "")[:40],
            "device": str(p.get("device") or CONFIG.get("preferred") or DEVICE_DEFAULT)[:80],
            "device_name": "",
            "via": "local",
            "ok": False, "bytes": 0, "ms": 0, "error": "",
        }
    meta = {}
    try:
        with PRINT_LOCK:
            with _JOBS_LOCK:
                _CURRENT_JOB = job
            res = _do_print(p, meta)
    finally:
        with _JOBS_LOCK:
            if _CURRENT_JOB is job:
                _CURRENT_JOB = None
    try:
        if res.get("ok") and meta.get("tspl"):
            extra = {"path": "site-html" if p.get("_tspl") is not None else "tspl"}
            if p.get("_tspl") is not None:
                extra.update(site_label_mm=[LABEL_WIDTH_MM, LABEL_HEIGHT_MM],
                             render_window="283x384@2.115",
                             render_dots=[LABEL_W_DOTS, LABEL_H_DOTS])
            else:
                try:
                    w_mm, h_mm, _W, _H, al = stock_dims(p)
                    ox, oy = stock_offset(p)
                    extra.update(sticker_mm=[w_mm, h_mm], align=al,
                                 offset_mm=[ox, oy],
                                 template=p.get("template") or "")
                except Exception:
                    pass
            res["layout"] = tspl_layout(meta["tspl"], extra)
    except Exception:
        pass
    try:
        if res.get("ok") and not res.get("via_local_service"):
            _CONN_CACHE.update(ts=time.time(), ok=True, why="printed")
        elif not res.get("ok"):
            _CONN_CACHE.update(ts=time.time(), ok=False,
                               why=str(res.get("error") or "")[:80])
    except Exception:
        pass
    # ── ledger: one row per finished job ─────────────────────────────────
    try:
        with _JOBS_LOCK:
            job["ok"] = bool(res.get("ok"))
            job["bytes"] = int(res.get("bytes") or len(meta.get("tspl") or b""))
            job["ms"] = int((time.time() - started) * 1000)
            job["error"] = str(res.get("error") or "")[:160]
            job["device_name"] = printer_name(res.get("device") or job["device"])
            if res.get("via_local_service"):
                job["via"] = "local-service"
            _JOBS.insert(0, job)
            del _JOBS[100:]
    except Exception:
        pass
    try:
        host_log("job %s %s %s cons=%s via=%s %dms %s"
                  % (job["id"], "OK" if job["ok"] else "FAIL",
                     job["origin"], job["consignment"] or "-", job["via"],
                     job["ms"], job["error"]))
    except Exception:
        pass
    return res


def probe_device(dev):
    """Open+close at each baud without printing. (True, baud) or (False, err)."""
    if os.name == "nt":
        try:
            umap = _win_usb_printer_map()
            if dev in umap:
                return True, umap[dev]
        except Exception:  # noqa: BLE001
            pass
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


# ── Local identity + diagnostics log ─────────────────────────────────────
NODE_NAME = (str(socket.gethostname() or "").strip() or "this-laptop")[:40]
HOST_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "db-print-host.log")
LOCAL_BRIDGE_PORT = 55559  # loopback only: one serial-port owner per machine
_LOCAL_BRIDGE_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "db-print-host.lock")
_LOCAL_LOCK_FILE = None
_LOCAL_BRIDGE_BOUND = False
_CONN_CACHE = {"ts": 0.0, "ok": False, "why": "", "dev": "", "conn": ""}
_HTTP_FAIL_TS = {"ts": 0.0}
_CONFIG_MTIME = {"ts": 0.0}

# ── Print job ledger (who printed what, and the in-flight job) ────────────
# Bounded in-memory ring; the autostart --serve host keeps the last 100 jobs
# for the extension's Print activity panel. Each job is also appended to the
# host log so a restart never loses the audit trail.
_JOBS_LOCK = threading.Lock()
_JOBS = []              # newest first, max 100
_JOB_SEQ = 0
_CURRENT_JOB = None


def _maybe_reload_config():
    """Pick up config saved by another host process (Chrome ⇄ autostart).

    The --serve process owns the serial port, while Settings values are
    saved by the Chrome-spawned host. A cheap mtime check keeps both in
    sync without restarting the service.
    """
    global CONFIG
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "db-print-config.json")
        m = os.path.getmtime(path)
        if not m or m == _CONFIG_MTIME.get("ts"):
            return
        d = load_config()
        if isinstance(d, dict):
            CONFIG = d
            _CONFIG_MTIME["ts"] = m
    except Exception:
        pass



def host_log(msg):
    """Host diagnostics - stderr is not visible in hidden/autostart runs."""
    try:
        if os.path.exists(HOST_LOG_PATH) and os.path.getsize(HOST_LOG_PATH) > 1000000:
            try:
                os.replace(HOST_LOG_PATH, HOST_LOG_PATH + ".1")
            except Exception:
                pass
        with open(HOST_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s pid=%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                        os.getpid(), msg))
    except Exception:
        pass


def _try_bridge_lock():
    """Only one local host may own the printer serial port. A per-user file
    lock (released by the OS if the process dies) keeps the Chrome-spawned
    host and the --serve autostart host from both holding it open."""
    global _LOCAL_LOCK_FILE
    if _LOCAL_LOCK_FILE is not None:
        return True
    f = None
    try:
        f = open(_LOCAL_BRIDGE_LOCK_PATH, "a+")
        if os.name == "nt":
            import msvcrt
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(" ")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCAL_LOCK_FILE = f
        return True
    except Exception:
        try:
            if f is not None:
                f.close()
        except Exception:
            pass
        return False


def _release_bridge_lock():
    global _LOCAL_LOCK_FILE
    if _LOCAL_LOCK_FILE is not None:
        try:
            _LOCAL_LOCK_FILE.close()
        except Exception:
            pass
        _LOCAL_LOCK_FILE = None


def _forward_to_coordinator(tspl):
    """Send bytes to the local bridge (loopback). None = none running."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/local_print" % LOCAL_BRIDGE_PORT,
            data=tspl, method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "X-DB-Local": "1"})
    except Exception:
        return None
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read(200000)
        res = json.loads(data.decode("utf-8"))
        return res if isinstance(res, dict) else None
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read(200000).decode("utf-8"))
            if isinstance(res, dict):
                return res
        except Exception:
            pass
        return {"ok": False, "error": "local print service error: %s" % e}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return {"ok": False, "error": "local print service timeout"}
        return None  # nothing listening on the bridge port
    except Exception:
        return None


class _LocalBridgeHandler(http.server.BaseHTTPRequestHandler):
    """Loopback-only print gateway owned by the autostart --serve host."""

    server_version = "DataBridgeLocal/1.0"

    def log_message(self, fmt, *args):
        pass  # stdout/stderr are not visible in hidden runs

    def _json(self, code, obj):
        try:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/health", "/ping"):
            self._json(200, {"ok": True, "host": "db-print",
                             "version": HOST_VERSION,
                             "connected": bool(_CONN_CACHE.get("ok")),
                             "device": _CONN_CACHE.get("dev") or ""})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/local_print":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            if self.client_address[0] not in ("127.0.0.1", "::1") \
                    or self.headers.get("X-DB-Local") != "1":
                self._json(403, {"ok": False, "error": "local prints only"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 1000000:
                self._json(400, {"ok": False, "error": "bad content length"})
                return
            body = self.rfile.read(n)
            res = dict(do_print({"_tspl": body}))
            res["via_local_service"] = True
            self._json(200 if res.get("ok") else 500, res)
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(e)})


def _start_local_bridge():
    """Bind the loopback print gateway once per machine."""
    global _LOCAL_BRIDGE_BOUND
    if _LOCAL_BRIDGE_BOUND:
        return True
    if not _try_bridge_lock():
        return False
    try:
        class _BridgeServer(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = os.name != "nt"
        srv = _BridgeServer(("127.0.0.1", LOCAL_BRIDGE_PORT),
                            _LocalBridgeHandler)
    except Exception as e:  # noqa: BLE001
        host_log("local bridge bind failed on 127.0.0.1:%d: %s"
                 % (LOCAL_BRIDGE_PORT, e))
        _release_bridge_lock()
        return False
    threading.Thread(target=srv.serve_forever, name="db-print-local-bridge",
                     daemon=True).start()
    _LOCAL_BRIDGE_BOUND = True
    host_log("local bridge listening on 127.0.0.1:%d" % LOCAL_BRIDGE_PORT)
    return True


def _bridge_watch():
    """Retake the local printer if the other host exits, and restore the
    label service if it was not bound at startup."""
    while True:
        time.sleep(20)
        try:
            if not _LOCAL_BRIDGE_BOUND:
                _start_local_bridge()
            if not _HTTP_STARTED:
                _start_label_service()
        except Exception:
            pass


def _pref_mode():
    pref = str(CONFIG.get("prefer") or "auto").strip().lower()
    if pref in ("bluetooth", "bt"):
        return "bt"
    if pref in ("usb", "cable"):
        return "cable"
    return "auto"


def _dev_label(dev):
    """bluetooth/cable for a device from discovery (ignores the legacy
    config `conn` override, which can mislabel every port)."""
    try:
        for d, c in discover_printers():
            if d == dev:
                return c
    except Exception:  # noqa: BLE001
        pass
    return conn_type(dev)


def _detect_candidates():
    """Ordered [(device, transport)] for the current prefer mode."""
    pref = _pref_mode()
    disc = discover_printers()
    labels = {d: c for d, c in disc}
    out, seen = [], set()

    def add(dev, ctype):
        if dev and dev not in seen:
            seen.add(dev)
            out.append((dev, ctype))

    if CONFIG.get("device"):
        dev = str(CONFIG.get("device"))
        return [(dev, labels.get(dev) or conn_type(dev))]

    for dev in (CONFIG.get("preferred"), CONFIG.get("bluetooth")):
        if dev:
            dev = str(dev)
            add(dev, labels.get(dev) or conn_type(dev))
    if pref in ("auto", "bt"):
        for dev, ctype in disc:
            if ctype == "bluetooth":
                add(dev, ctype)
    if pref in ("auto", "cable"):
        cables = [(d, c) for d, c in disc
                  if c == "cable" and _cable_rank(d) > 0]
        cables.sort(key=lambda x: -_cable_rank(x[0]))
        for dev, ctype in cables:
            add(dev, ctype)
    return [(d, c) for d, c in out
            if pref == "auto"
            or (pref == "bt" and c == "bluetooth")
            or (pref == "cable" and c == "cable")]


def _detect_local_printer():
    """(ok, device, transport, reason) — the port that can actually print."""
    first = last = "not-found"
    for dev, ctype in _detect_candidates():
        if ctype == "bluetooth" and os.name == "nt":
            known, live = _win_bt_verify(dev)
            if known:
                if live:
                    return True, dev, "bluetooth", "bluetooth"
                last = "paired-not-connected"
                if first == "not-found":
                    first = last
                continue
        try:
            ok, why = device_connected(dev)
        except Exception as e:  # noqa: BLE001
            ok, why = False, str(e)
        if ok:
            return True, dev, ctype or _dev_label(dev), str(why)
        last = str(why)
        if first == "not-found":
            first = last
    return False, "", "", (first if first != "not-found" else last)


def _conn_cache_refresh(force=False):
    now = time.time()
    if not force and now - float(_CONN_CACHE.get("ts") or 0.0) < 10.0:
        return _CONN_CACHE
    if _WARM.get("ser") is not None and _WARM.get("dev"):
        dev = str(_WARM.get("dev"))
        _CONN_CACHE.update(ts=now, ok=True, why="warm", dev=dev,
                           conn=_dev_label(dev))
        return _CONN_CACHE
    got = PRINT_LOCK.acquire(blocking=bool(force))
    if not got:
        return _CONN_CACHE
    try:
        ok, dev, ctype, why = _detect_local_printer()
        _CONN_CACHE.update(ts=time.time(), ok=bool(ok), why=str(why),
                           dev=dev, conn=ctype)
    except Exception as e:  # noqa: BLE001
        _CONN_CACHE.update(ts=time.time(), ok=False, why=str(e),
                           dev="", conn="")
    finally:
        if got:
            PRINT_LOCK.release()
    return _CONN_CACHE


def handle(msg):
    action = (msg.get("action") or "").strip()
    _maybe_reload_config()
    if action == "ping":
        # Never probe every COM port here: phantom Bluetooth ports take ~3s
        # per baud each, which pushed ping past the extension timeout and
        # showed "Print host missing" while the printer was connected. Use
        # the cached connection truth instead (refreshed at most every 10s).
        cc = _conn_cache_refresh(False)
        dev_ok = bool(cc.get("ok"))
        dev_why = cc.get("why") or ""
        detected = str(cc.get("dev") or "")
        detected_conn = str(cc.get("conn") or "")
        active = (detected if dev_ok and detected else
                  (CONFIG.get("preferred") or CONFIG.get("device")
                   or DEVICE_DEFAULT))
        active_conn = detected_conn if dev_ok and detected else conn_type(active)
        printers = []
        for d, c in discover_printers():
            ent = {"device": d, "conn": c}
            if d == active:
                ent["connected"] = bool(dev_ok)
            printers.append(ent)
        if active not in [p["device"] for p in printers]:
            printers.insert(0, {"device": active, "conn": active_conn,
                                "connected": bool(dev_ok)})
        return {"ok": True, "host": "db-print", "version": HOST_VERSION,
                "device": active, "conn": active_conn,
                "preferred": CONFIG.get("preferred") or "",
                "prefer": CONFIG.get("prefer", "auto"),
                "warm": bool(_WARM.get("ser")),
                "connected": dev_ok, "connected_reason": dev_why,
                "width_mm": CONFIG.get("width_mm", 48),
                "height_mm": CONFIG.get("height_mm", 80),
                "align": CONFIG.get("align", "left"),
                "offset_x_mm": CONFIG.get("offset_x_mm", 0),
                "offset_y_mm": CONFIG.get("offset_y_mm", 0),
                "border": bool(CONFIG.get("border", False)),
                "printers": printers}
    if action == "print_history":
        with _JOBS_LOCK:
            cur = dict(_CURRENT_JOB) if _CURRENT_JOB else None
            history = [dict(j) for j in _JOBS[:50]]
        ok_n = sum(1 for j in history if j.get("ok"))
        return {"ok": True, "current": cur, "history": history,
                "counts": {"total": len(history), "ok": ok_n,
                           "failed": len(history) - ok_n}}
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
            try:
                w_mm2, h_mm2, _W2, _H2, al2 = stock_dims(msg)
                out["layout"] = tspl_layout(tspl, {
                    "path": "tspl", "sticker_mm": [w_mm2, h_mm2],
                    "align": al2, "offset_mm": [ox, oy],
                    "template": msg.get("template") or "", "dry_run": True})
            except Exception:
                out["layout"] = tspl_layout(tspl, {"path": "tspl", "dry_run": True})
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
            "module_height": 20.0,
            "module_width": 0.4,
            "quiet_zone": 4.0,
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
    content = content.replace('max-height: 36px', 'max-height: 130px')
    bc_url = _barcode_data_url(bc_text)
    qr_url = _qr_data_url(qr_text)
    if bc_url:
        content = content.replace(
            "__barcode__",
            '<img src="' + bc_url + '" style="width:100%;height:36px;display:block;image-rendering:pixelated;">')
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
    dev = CONFIG.get("device") or ""
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
        return True
    servers = []
    try:
        class _Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
        servers.append(_Server(("127.0.0.1", LABEL_SERVICE_PORT), _LabelHTTPHandler))
    except Exception as e:  # noqa: BLE001 - try IPv6 loopback below
        sys.stderr.write("db-print: IPv4 print service unavailable on %s: %s\n"
                         % (LABEL_SERVICE_PORT, e))
        if time.time() - _HTTP_FAIL_TS.get("ts", 0.0) > 300:
            _HTTP_FAIL_TS["ts"] = time.time()
            host_log("local print service bind failed on %s: %s"
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
        return False
    _HTTP_STARTED = True
    for i, srv in enumerate(servers):
        t = threading.Thread(target=srv.serve_forever,
                             name="db-print-http-%d" % i, daemon=True)
        t.start()
    host_log("local print service listening on %s" % LABEL_SERVICE_PORT)
    return True


def serve():
    """Standalone mode (autostart): no Chrome stdin, keep the local print
    service and the serial-port bridge alive in the background."""
    host_log("serve mode start version=%s" % HOST_VERSION)
    while True:
        try:
            _maybe_reload_config()
            if not _HTTP_STARTED:
                _start_label_service()
            if not _LOCAL_BRIDGE_BOUND:
                _start_local_bridge()
        except Exception as e:  # noqa: BLE001 - never die
            host_log("serve loop: %s" % e)
        time.sleep(10)


def main():
    if "--serve" in sys.argv[1:]:
        serve()
        return
    _start_label_service()
    _start_local_bridge()
    try:
        threading.Thread(target=_bridge_watch, name="db-print-bridge-watch",
                         daemon=True).start()
    except Exception:
        pass
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
