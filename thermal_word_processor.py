"""Thermal Word Processor: a small word processor for thermal receipt printers.

Works with two printers. Pick one from the Printer menu and the whole window switches to suit it:
  * Epson TM-m30   - 80mm paper, on the home network (Ethernet), cuts the paper itself
  * Sunydog MPT-II - 58mm paper, paired with Windows over Bluetooth (shows up as a COM port)

The page is exactly as wide as the paper (one screen pixel per printer dot), so lines wrap on
screen where they will wrap on the receipt. Select text and use the toolbar (or Ctrl+B, Ctrl+U...)
to style it. Insert pictures and QR codes from the Insert menu. Save receipts as .receipt files.

Double-click this file (or run `python thermal_word_processor.py`) to open it.
Needs Pillow (pip install pillow) for pictures.

It's also packaged as a stand-alone ThermalWordProcessor.exe (see release.py) that needs nothing
installed and updates itself from GitHub Releases (see updater.py).
"""

import base64
import ctypes
import io
import json
import os
import queue
import random
import socket
import sys
import threading
import winreg
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageOps, ImageTk

import updater

APP_NAME = "Thermal Word Processor"
APP_VERSION = "1.0.0"                            # release.py bumps this
UPDATE_REPO = "diloon0/thermal-word-processor"   # where new versions are published (GitHub Releases)
FROZEN = getattr(sys, "frozen", False)           # True when running as the packaged .exe

# ---------------------------------------------------------------------------------------------
# The printers
# ---------------------------------------------------------------------------------------------

ALL_FEATURES = {"bold", "underline", "reverse", "small", "align", "upside", "sideways", "picture", "qr", "status"}

PRINTERS = {
    "epson": {
        "name": "Epson TM-m30",
        "paper": "80mm",
        "width_dots": 576,                 # printable width of the paper, in printer dots
        "char_dots": {"A": 12, "B": 10},   # Font A fits 48 per line, Font B ("Small") fits 57
        "connection": "network",
        "address_label": "Printer address:",
        "default_address": "10.0.0.114",
        "default_feed": 2,                 # blank lines before the cut
        "cuts": True,                      # always cut when done
        "features": ALL_FEATURES,
        "sizes": "all",
    },
    "sunydog": {
        "name": "Sunydog MPT-II",
        "paper": "58mm",
        "width_dots": 384,
        "char_dots": {"A": 12},            # 32 per line
        "connection": "bluetooth",
        "bluetooth_name": "MPT-II",        # what Windows calls it once paired; used to find its COM port
        "address_label": "Port:",
        "default_address": "COM7",
        "default_feed": 3,                 # blank lines so the message clears the tear edge
        "cuts": False,
        "features": {"bold", "align", "qr"},   # only what we know this printer handles
        "sizes": ["Normal", "Large"],
    },
}
DEFAULT_PRINTER = "sunydog" if FROZEN else "epson"   # the .exe is made for the Sunydog's new home
NETWORK_PORT = 9100   # the port network receipt printers listen on for print jobs
if FROZEN:   # the .exe can live anywhere, so its settings live in the user's own app-data folder
    SETTINGS_FILE = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME / "settings.json"
else:        # running as a script: settings sit next to it (shared with phone_print_helper.py)
    SETTINGS_FILE = Path(__file__).with_name("thermal_printer_settings.json")

SIZE_PRESETS = [("Normal", 1, 1), ("Tall", 1, 2), ("Wide", 2, 1), ("Large", 2, 2),
                ("Huge", 3, 3), ("Giant", 4, 4), ("Max", 8, 8)]
CUSTOM_SIZE = "Custom..."

# Picture darkness, matching the calibration bars on the sampler receipt (tm_m30_sampler.py).
# Thermal dots spread into each other, so about half the dots already looks solid black.
DARKNESS = [("A - darkest", 1.0), ("B", 0.70), ("C - recommended", 0.55), ("D", 0.45), ("E - lightest", 0.35)]
DEFAULT_INK_LIMIT = 0.55

PICTURE_SIZES = {"Full width": 1.0, "Half width": 0.5, "Third width": 1 / 3}

# QR size = how many printer dots each QR square takes (the printers allow 1-16)
QR_SIZES = {"Small": 5, "Medium": 9, "Large": 16}
QR_MIN_DOTS = 3          # smaller squares than this get hard for phones to scan
# Max bytes a QR code can hold at each version (1, 2, 3...) with low error correction
QR_CAPACITY = [17, 32, 53, 78, 106, 134, 154, 192, 230, 271, 321, 367, 425, 458, 520, 586, 644,
               718, 792, 858, 929, 1003, 1091, 1171, 1273, 1367, 1465, 1528, 1628, 1732]

# ESC/POS control codes
ESC, GS, DLE = b"\x1b", b"\x1d", b"\x10"
INIT = ESC + b"@"
ALIGN = {"left": ESC + b"a\x00", "center": ESC + b"a\x01", "right": ESC + b"a\x02"}
UPSIDE_ON, UPSIDE_OFF = ESC + b"{\x01", ESC + b"{\x00"       # only switches at the start of a line
SIDEWAYS_ON, SIDEWAYS_OFF = ESC + b"V\x01", ESC + b"V\x00"
FEED_AND_CUT = GS + b"VB\x00"   # feed the paper up to the cutter, then cut (leaves a tiny tab)

# ---------------------------------------------------------------------------------------------
# Styles. Inline styles apply to characters; line styles apply to whole lines.
# ---------------------------------------------------------------------------------------------

INLINE_TAGS = {"bold", "ul1", "ul2", "reverse", "small"}     # plus "size_WxH"
LINE_TAGS = {"align_center", "align_right", "upside", "sideways"}


def is_style(tag):
    return tag in INLINE_TAGS or tag in LINE_TAGS or tag.startswith("size_")


def size_of(tags):
    for tag in tags:
        if tag.startswith("size_"):
            w, h = tag[5:].split("x")
            return int(w), int(h)
    return 1, 1


def align_of(tags):
    return "center" if "align_center" in tags else "right" if "align_right" in tags else "left"


def font_family(tags, printer):
    return "B" if "small" in tags and "B" in printer["char_dots"] else "A"


def char_width(tags, printer):
    """How many printer dots one character takes with these styles."""
    return printer["char_dots"][font_family(tags, printer)] * size_of(tags)[0]


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------

def load_settings():
    settings = {"printer": DEFAULT_PRINTER, "addresses": {}, "ink_limit": DEFAULT_INK_LIMIT, "folder": "",
                "check_updates": True}
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if saved.get("printer") in PRINTERS:
            settings["printer"] = saved["printer"]
        if isinstance(saved.get("addresses"), dict):
            settings["addresses"].update(saved["addresses"])
        if saved.get("ink_limit") in [limit for _, limit in DARKNESS]:
            settings["ink_limit"] = saved["ink_limit"]
        if isinstance(saved.get("folder"), str):
            settings["folder"] = saved["folder"]
        if isinstance(saved.get("check_updates"), bool):
            settings["check_updates"] = saved["check_updates"]
    except (OSError, ValueError, AttributeError):
        pass   # no settings yet (or unreadable), so use the defaults
    return settings


def save_settings(settings):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass   # not being able to remember settings shouldn't stop printing


def find_bluetooth_port(device_name):
    """The COM port Windows gave a paired Bluetooth printer, found by its name (e.g. "MPT-II"), or None.

    Windows keeps paired devices (name and Bluetooth address) and their serial ports (address and
    COM port) in the registry, so this matches the two up. Nothing is sent to the printer.
    """
    base = r"SYSTEM\CurrentControlSet\Enum\BTHENUM"
    serial_service = "{00001101-0000-1000-8000-00805F9B34FB}"   # Bluetooth "serial port" profile

    def subkeys(path):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                names, i = [], 0
                while True:
                    try:
                        names.append(winreg.EnumKey(key, i))
                        i += 1
                    except OSError:
                        return names
        except OSError:
            return []

    def value(path, name):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    addresses = set()   # Bluetooth addresses of paired devices with this name
    for dev in subkeys(base):
        if dev.upper().startswith("DEV_"):
            for inst in subkeys(f"{base}\\{dev}"):
                if (value(f"{base}\\{dev}\\{inst}", "FriendlyName") or "").strip().lower() == device_name.lower():
                    addresses.add(dev[4:].upper())
    for svc in subkeys(base):
        if svc.upper().startswith(serial_service):
            for inst in subkeys(f"{base}\\{svc}"):
                address = inst.split("&")[-1].split("_")[0].upper()
                if address in addresses:
                    port = value(f"{base}\\{svc}\\{inst}\\Device Parameters", "PortName")
                    if port:
                        return port
    return None


# ---------------------------------------------------------------------------------------------
# Pictures and QR codes
# ---------------------------------------------------------------------------------------------

def ink(level, limit):
    """How much of an area to fill with dots to *look* like `level` (0 white .. 1 black) on paper."""
    return limit * level + (1 - limit) * level ** 8


def prepare_picture(path, width):
    """Open an image file as greyscale, `width` dots wide (rounded to a multiple of 8)."""
    img = ImageOps.exif_transpose(Image.open(path))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, img)
    img = img.convert("L")
    width = max(8, min(width, img.width) // 8 * 8)   # never blow small images up
    height = max(1, round(img.height * width / img.width))
    return img.resize((width, height), Image.LANCZOS)


def scale_picture(source, size_name, width_dots):
    """The picture at a chosen size: a share of the paper width, never larger than the original."""
    width = max(8, min(source.width, round(width_dots * PICTURE_SIZES[size_name])) // 8 * 8)
    if width == source.width:
        return source
    height = max(1, round(source.height * width / source.width))
    return source.resize((width, height), Image.LANCZOS)


def dither_picture(gray, limit):
    """Greyscale picture -> 1-bit picture, lightened so it looks right on thermal paper."""
    lut = [round(255 * (1 - ink((255 - v) / 255, limit))) for v in range(256)]
    return gray.point(lut).convert("1")   # Pillow uses Floyd-Steinberg error diffusion


def raster_bytes(bw):
    """ESC/POS bytes that print a 1-bit Pillow image (width a multiple of 8)."""
    w, h = bw.size
    row = w // 8
    data = bytes(b ^ 0xFF for b in bw.tobytes())   # Pillow: 1 = white; printer: 1 = black
    out = b""
    for top in range(0, h, 256):   # send tall pictures in bands
        band = min(256, h - top)
        out += GS + b"v0\x00" + bytes([row & 0xFF, row >> 8, band & 0xFF, band >> 8])
        out += data[top * row:(top + band) * row]
    return out


def qr_geometry(text, size_name, width_dots):
    """(squares across, dots per square) for a QR code, or ValueError if it can't fit."""
    data = text.encode("utf-8")
    version = next((v for v, cap in enumerate(QR_CAPACITY, 1) if len(data) <= cap), None)
    squares = 17 + 4 * version if version else None
    if not squares or width_dots // squares < QR_MIN_DOTS:
        raise ValueError("The QR text is too long to fit on the paper. Try a shorter link or message.")
    return squares, min(QR_SIZES[size_name], width_dots // squares)


def qr_command(fn, payload):
    """One piece of the printer's built-in QR command (GS ( k)."""
    n = len(payload) + 2
    return GS + b"(k" + bytes([n & 0xFF, n >> 8, 49, fn]) + payload


def qr_bytes(text, size_name, width_dots):
    _, dots = qr_geometry(text, size_name, width_dots)
    return (qr_command(65, bytes([50, 0]))     # QR model 2
            + qr_command(67, bytes([dots]))    # square size
            + qr_command(69, bytes([48]))      # error correction: low (fits the most text)
            + qr_command(80, b"0" + text.encode("utf-8"))
            + qr_command(81, b"0"))


def qr_preview(text, size_name, width_dots):
    """A look-alike of the QR code at its printed size. The printer draws the real one."""
    squares, dots = qr_geometry(text, size_name, width_dots)
    img = Image.new("L", (squares * dots,) * 2, 255)
    draw = ImageDraw.Draw(img)
    rng = random.Random(text)

    def square(x, y, n, fill):
        draw.rectangle([x * dots, y * dots, (x + n) * dots - 1, (y + n) * dots - 1], fill=fill)

    for x in range(squares):
        for y in range(squares):
            if rng.random() < 0.5:
                square(x, y, 1, 0)
    for fx, fy in ((0, 0), (squares - 7, 0), (0, squares - 7)):
        square(fx - 1 if fx else fx, fy - 1 if fy else fy, 8, 255)
        square(fx, fy, 7, 0)
        square(fx + 1, fy + 1, 5, 255)
        square(fx + 2, fy + 2, 3, 0)
    c = squares * dots // 2
    draw.rectangle([c - 26, c - 11, c + 26, c + 11], fill=255, outline=0)
    draw.text((c, c), "QR", fill=0, anchor="mm")
    return img


# ---------------------------------------------------------------------------------------------
# Turning the document into printer bytes
# ---------------------------------------------------------------------------------------------

def encode(text):
    # Both printers' built-in character set is the old DOS one (code page 437)
    return text.encode("cp437", errors="replace")


def style_bytes(tags, printer):
    f = printer["features"]
    w, h = size_of(tags)
    out = ESC + b"E" + (b"\x01" if "bold" in tags and "bold" in f else b"\x00")
    out += GS + b"!" + bytes([((w - 1) << 4) | (h - 1)])
    if "underline" in f:
        out += ESC + b"-" + (b"\x02" if "ul2" in tags else b"\x01" if "ul1" in tags else b"\x00")
    if "reverse" in f:
        out += GS + b"B" + (b"\x01" if "reverse" in tags else b"\x00")
    if "small" in f:
        out += ESC + b"M" + (b"\x01" if font_family(tags, printer) == "B" else b"\x00")
    return out


def wrap_runs(runs, printer):
    """Word-wrap styled text [(text, tags), ...] to the paper width, measuring in printer dots.

    Mirrors how the editor wraps: break after spaces, split words longer than a whole line.
    Returns a list of printed lines, each a list of (text, tags).
    """
    width = printer["width_dots"]
    chars = [(ch, tags) for text, tags in runs for ch in text]
    tokens, i = [], 0
    while i < len(chars):   # alternate runs of spaces and non-spaces
        j = i
        while j < len(chars) and (chars[j][0] == " ") == (chars[i][0] == " "):
            j += 1
        tokens.append(chars[i:j])
        i = j

    lines, cur, cur_w = [], [], 0

    def push():
        nonlocal cur, cur_w
        while cur and cur[-1][0] == " ":   # spaces at a wrap point aren't printed
            cur.pop()
        lines.append(cur)
        cur, cur_w = [], 0

    for tok in tokens:
        tok_w = sum(char_width(tags, printer) for _, tags in tok)
        if tok[0][0] == " ":
            cur.extend(tok)
            cur_w += tok_w
        elif cur_w + tok_w <= width:
            cur.extend(tok)
            cur_w += tok_w
        else:
            if cur:
                push()
            if tok_w <= width:
                cur.extend(tok)
                cur_w += tok_w
            else:   # a word wider than the paper gets split
                for ch, tags in tok:
                    cw = char_width(tags, printer)
                    if cur_w + cw > width and cur:
                        push()
                    cur.append((ch, tags))
                    cur_w += cw
    if cur or not lines:
        push()

    merged = []
    for line in lines:
        out = []
        for ch, tags in line:
            if out and out[-1][1] == tags:
                out[-1] = (out[-1][0] + ch, tags)
            else:
                out.append((ch, tags))
        merged.append(out)
    return merged


def build_job(printer, lines, feed, ink_limit):
    """Document lines -> (ESC/POS bytes, set of things that were skipped).

    `lines` is a list of {"items": [("text", str, tags) | ("object", obj, tags)], "nl_tags": tags}.
    """
    f = printer["features"]
    skipped = set()
    printed = []   # ("text", runs, line_tags, blank_tags, sideways) or ("object", obj, line_tags)
    for line in lines:
        items = line["items"]
        line_tags = items[0][2] if items else line["nl_tags"]
        text_runs = [(value, tags) for kind, value, tags in items if kind == "text"]
        objects = [value for kind, value, _ in items if kind == "object" and value]
        if objects:
            if any(text.strip() for text, _ in text_runs):
                printed += [("text", r, line_tags, line["nl_tags"], False) for r in wrap_runs(text_runs, printer)]
            for obj in objects:
                if obj["type"] in f:
                    printed.append(("object", obj, line_tags))
                else:
                    skipped.add(obj["type"])
            continue
        if "sideways" in line_tags and "sideways" in f and text_runs:
            printed.append(("text", text_runs, line_tags, line["nl_tags"], True))   # the printer wraps these
        else:
            printed += [("text", r, line_tags, line["nl_tags"], False) for r in wrap_runs(text_runs, printer)]

    # Upside-down lines are read with the receipt turned around, so print each block bottom-up
    ordered, i = [], 0
    flipped = lambda p: p[0] == "text" and "upside" in p[2] and "upside" in f
    while i < len(printed):
        if flipped(printed[i]):
            j = i
            while j < len(printed) and flipped(printed[j]):
                j += 1
            ordered += reversed(printed[i:j])
            i = j
        else:
            ordered.append(printed[i])
            i += 1

    job, upside = INIT, False
    for p in ordered:
        line_tags = p[2]
        if flipped(p) != upside:
            upside = flipped(p)
            job += UPSIDE_ON if upside else UPSIDE_OFF
        job += ALIGN[align_of(line_tags)] if "align" in f else ALIGN["left"]
        if p[0] == "object":
            obj = p[1]
            if obj["type"] == "qr":
                job += qr_bytes(obj["data"], obj["size"], printer["width_dots"]) + b"\n"
            else:
                job += raster_bytes(dither_picture(obj["gray"], ink_limit))
            continue
        _, runs, _, blank_tags, sideways = p
        if sideways:
            job += SIDEWAYS_ON
        if runs:
            for text, tags in runs:
                job += style_bytes(tags, printer) + encode(text)
        else:
            job += style_bytes(blank_tags, printer)   # a blank line is as tall as its style
        job += b"\n"
        if sideways:
            job += SIDEWAYS_OFF
    if upside:
        job += UPSIDE_OFF
    job += style_bytes(set(), printer) + ALIGN["left"] + b"\n" * feed
    if printer["cuts"]:
        job += FEED_AND_CUT
    return job, skipped


# ---------------------------------------------------------------------------------------------
# Sending to the printers
# ---------------------------------------------------------------------------------------------

class PrinterProblem(Exception):
    """The printer answered, but it isn't ready (no paper, cover open...)."""


# Talking to a Bluetooth COM port goes straight through Windows
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def send_bluetooth(port, data):
    # A paired Bluetooth printer shows up as a COM port; writing to it sends the bytes over Bluetooth.
    # COM ports must be opened exclusively (no sharing), which Python's open() can't do, so call Windows directly.
    handle = kernel32.CreateFileW(rf"\\.\{port}", GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        written = wintypes.DWORD()
        if not kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def read_status(conn):
    """Ask a network printer how it's doing (real-time status). Missing answers are None."""
    conn.settimeout(2)
    status = {}
    for n in (2, 3, 4):   # 2 = cover/feed, 3 = errors, 4 = paper
        conn.sendall(DLE + b"\x04" + bytes([n]))
        try:
            reply = conn.recv(1)
            status[n] = reply[0] if reply else None
        except socket.timeout:
            status[n] = None
    return status


def check_status(status):
    """Raise PrinterProblem if the printer can't print; return a warning (or None) if it can."""
    paper, cover, errors = status.get(4), status.get(2), status.get(3)
    if paper is not None and paper & 0x60:
        raise PrinterProblem("The printer is out of paper. Load a new roll and try again.")
    if cover is not None and cover & 0x04:
        raise PrinterProblem("The printer's cover is open. Close it until it clicks, then try again.")
    if errors is not None and errors & 0x08:
        raise PrinterProblem("The cutter is stuck. Open the cover, clear any jammed paper, close it, and try again.")
    if errors is not None and errors & 0x60:
        raise PrinterProblem("The printer reports an error. Try switching it off and on again.")
    if paper is not None and paper & 0x0C:
        return "Paper is running low."
    return None


def send_network(address, data, check=True):
    # A network receipt printer prints whatever bytes arrive on port 9100
    with socket.create_connection((address, NETWORK_PORT), timeout=10) as conn:
        warning = check_status(read_status(conn)) if check else None
        conn.settimeout(10)
        conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)   # tell the printer the job is complete
    return warning


# ---------------------------------------------------------------------------------------------
# The editor widget
# ---------------------------------------------------------------------------------------------

class RichText(tk.Text):
    """A Text widget that gives typed and pasted text the current style.

    A small Tcl wrapper sits in front of the widget's own command, so typing, pasting and
    our own edits all pass through it: plain inserts (no tags given) get the current style,
    and edits, cursor moves and scrolling tell the app to refresh the toolbar and margin.
    """

    def __init__(self, master, app, **kw):
        super().__init__(master, **kw)
        self.app = app
        w = self._w
        tags_cmd = self.register(self._typing_tags)
        moved_cmd = self.register(self._cursor_moved)
        changed_cmd = self.register(app.schedule_refresh)
        self.tk.eval(f"""
            rename {w} {w}_orig
            proc {w} {{args}} {{
                set cmd [lindex $args 0]
                set sub [lindex $args 1]
                if {{$cmd eq "insert" && [llength $args] == 3}} {{
                    lappend args [{tags_cmd}]
                }} elseif {{[lrange $args 0 2] eq {{mark set insert}}}} {{
                    {moved_cmd}
                }}
                set code [catch {{{w}_orig {{*}}$args}} result]
                if {{$cmd in {{insert delete replace yview see}}
                     || ($cmd eq "tag" && $sub in {{add remove}})
                     || ($cmd eq "image" && $sub eq "create")
                     || ($cmd eq "mark" && $sub eq "set")}} {{
                    {changed_cmd}
                }}
                if {{$code == 1}} {{ return -code error $result }}
                return $result
            }}
        """)

    def _typing_tags(self):
        tags = self.app.typing_tags()
        self.app.pending = None
        return " ".join(sorted(tags) + [self.app.font_tag(tags)])

    def _cursor_moved(self):
        self.app.pending = None   # clicking or arrowing elsewhere forgets a style picked for typing

    def destroy(self):
        try:
            self.tk.call("rename", self._w, "")
            self.tk.call("rename", self._w + "_orig", self._w)
        except tk.TclError:
            pass
        super().destroy()


class Dialog(tk.Toplevel):
    """A small modal dialog. Subclasses fill self.body and set self.result in ok()."""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)

    def finish(self, first_focus):
        buttons = ttk.Frame(self.body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="OK", command=self.ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        first_focus.focus_set()
        self.wait_window()

    def ok(self):
        self.destroy()


class QRDialog(Dialog):
    def __init__(self, parent, data="", size="Medium", title="Insert QR code"):
        super().__init__(parent, title)
        ttk.Label(self.body, text="Link or text for the QR code:").pack(anchor="w")
        self.text = tk.Text(self.body, width=40, height=3, wrap="char", font=("Consolas", 11))
        self.text.pack(fill="x", pady=(2, 8))
        if data:
            self.text.insert("1.0", data)
            self.text.tag_add("sel", "1.0", "end-1c")   # ready to type over, or click to tweak
        self.size = tk.StringVar(value=size)
        row = ttk.Frame(self.body)
        row.pack(anchor="w")
        ttk.Label(row, text="Size:").pack(side="left", padx=(0, 6))
        for name in QR_SIZES:
            ttk.Radiobutton(row, text=name, value=name, variable=self.size).pack(side="left", padx=(0, 8))
        self.text.bind("<Return>", lambda e: (self.ok(), "break")[1])
        self.finish(self.text)

    def ok(self):
        data = self.text.get("1.0", "end").strip()
        if data:
            self.result = (data, self.size.get())
        self.destroy()


class ChoiceDialog(Dialog):
    def __init__(self, parent, title, prompt, choices, default):
        super().__init__(parent, title)
        ttk.Label(self.body, text=prompt).pack(anchor="w", pady=(0, 6))
        self.choice = tk.StringVar(value=default)
        first = None
        for name in choices:
            b = ttk.Radiobutton(self.body, text=name, value=name, variable=self.choice)
            b.pack(anchor="w")
            first = first or b
        self.bind("<Return>", lambda e: self.ok())
        self.finish(first)

    def ok(self):
        self.result = self.choice.get()
        self.destroy()


class SizeDialog(Dialog):
    def __init__(self, parent, w, h):
        super().__init__(parent, "Custom text size")
        ttk.Label(self.body, text="Each letter can be 1 to 8 times wider and taller.").pack(anchor="w", pady=(0, 8))
        self.w, self.h = tk.IntVar(value=w), tk.IntVar(value=h)
        row = ttk.Frame(self.body)
        row.pack(anchor="w")
        ttk.Label(row, text="Width:").pack(side="left")
        first = ttk.Spinbox(row, from_=1, to=8, width=3, textvariable=self.w)
        first.pack(side="left", padx=(4, 12))
        ttk.Label(row, text="Height:").pack(side="left")
        ttk.Spinbox(row, from_=1, to=8, width=3, textvariable=self.h).pack(side="left", padx=(4, 0))
        self.bind("<Return>", lambda e: self.ok())
        self.finish(first)

    def ok(self):
        try:
            self.result = (min(8, max(1, int(self.w.get()))), min(8, max(1, int(self.h.get()))))
        except (tk.TclError, ValueError):
            pass
        self.destroy()


# ---------------------------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------------------------

class ThermalWordProcessor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.busy = False
        self.pending = None          # style chosen with nothing selected, for the next thing typed
        self.objects = {}            # embedded picture / QR name -> its data
        self.photos = {}             # keep preview images alive
        self.fonts = {}
        self.file_path = None
        self.cleared = None          # what Clear removed, until Ctrl+Z brings it back
        self.content = None
        self.text = None
        self._refresh_queued = False
        self.ui_queue = queue.Queue()   # work handed from background threads to the window
        if FROZEN:
            try:
                self.iconbitmap(default=sys.executable)   # the receipt icon built into the .exe
            except tk.TclError:
                pass

        self.printer_var = tk.StringVar(value=self.settings["printer"])
        self.ink_var = tk.DoubleVar(value=self.settings["ink_limit"])
        self.auto_update_var = tk.BooleanVar(value=self.settings["check_updates"])
        self.build_menus()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.build_window()
        self.poll_ui_queue()
        if FROZEN:
            self.after(5000, updater.remove_old_copy)   # left behind by the last update, if any
            if self.settings["check_updates"]:
                self.after(3000, self.check_for_updates)

    # ---------- menus and window ----------

    def build_menus(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="New", accelerator="Ctrl+N", command=self.new_doc)
        file_menu.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_doc)
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_doc)
        file_menu.add_command(label="Save as...", command=lambda: self.save_doc(ask=True))
        file_menu.add_command(label="Clear page", command=self.clear_page)
        file_menu.add_separator()
        file_menu.add_command(label="Print", accelerator="Ctrl+P", command=self.print_receipt)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit_app)
        menubar.add_cascade(label="File", menu=file_menu)

        self.insert_menu = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label="Insert", menu=self.insert_menu)

        self.printer_menu = printer_menu = tk.Menu(menubar, tearoff=False)
        for key, printer in PRINTERS.items():
            printer_menu.add_radiobutton(label=f"{printer['name']}  ({printer['paper']} paper)",
                                         value=key, variable=self.printer_var, command=self.switch_printer)
        printer_menu.add_separator()
        darkness = tk.Menu(printer_menu, tearoff=False)
        for label, limit in DARKNESS:
            darkness.add_radiobutton(label=label, value=limit, variable=self.ink_var, command=self.change_darkness)
        printer_menu.add_cascade(label="Picture darkness", menu=darkness)
        self.darkness_index = printer_menu.index("end")
        menubar.add_cascade(label="Printer", menu=printer_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Check for updates...", command=lambda: self.check_for_updates(manual=True))
        help_menu.add_checkbutton(label="Check for updates automatically", variable=self.auto_update_var,
                                  command=self.toggle_auto_updates)
        help_menu.add_separator()
        help_menu.add_command(label=f"About {APP_NAME}", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menubar)

        self.shortcuts = (("n", self.new_doc), ("o", self.open_doc), ("s", self.save_doc), ("p", self.print_receipt),
                          ("b", lambda: self.toggle_shortcut("bold")), ("u", self.cycle_underline),
                          ("l", lambda: self.set_align("left")), ("e", lambda: self.set_align("center")),
                          ("r", lambda: self.set_align("right")), ("t", self.insert_tickbox))
        self.bind_shortcuts(self)

    def bind_shortcuts(self, widget):
        # Bound on the editor too, so its built-in Ctrl shortcuts (Ctrl+O = new line...) don't fire first
        for key, action in self.shortcuts:
            widget.bind(f"<Control-{key}>", lambda e, a=action: (a(), "break")[1])

    def build_window(self, doc=None):
        """Lay out the whole window for the chosen printer (and only that printer)."""
        self.printer_key = self.printer_var.get()
        printer = self.printer = PRINTERS[self.printer_key]
        f = printer["features"]
        self.fonts.clear()
        self.objects.clear()
        self.photos.clear()
        self.printer_menu.entryconfigure(self.darkness_index, state="normal" if "picture" in f else "disabled")

        self.insert_menu.delete(0, "end")
        if "picture" in f:
            self.insert_menu.add_command(label="Picture...", command=self.insert_picture)
        if "qr" in f:
            self.insert_menu.add_command(label="QR code...", command=self.insert_qr)
        self.insert_menu.add_command(label="Tick box  [ ]", accelerator="Ctrl+T", command=self.insert_tickbox)
        self.insert_menu.add_command(label="Divider line", command=self.insert_divider)

        if self.content is not None:
            self.content.destroy()
        self.content = outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # Toolbar: character styles
        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        self.vars = {}

        def toggle(parent, label, tag):
            v = self.vars[tag] = tk.BooleanVar()
            b = ttk.Checkbutton(parent, text=label, variable=v, style="Toolbutton",
                                command=lambda: self.set_inline(tag, v.get()))
            b.pack(side="left", padx=(0, 2))
            return b

        if "bold" in f:
            toggle(bar, "Bold", "bold")
        if "underline" in f:
            self.ul_var = tk.StringVar(value="off")
            mb = ttk.Menubutton(bar, text="Underline", style="Toolbutton")
            menu = tk.Menu(mb, tearoff=False)
            for value, label in (("off", "No underline"), ("ul1", "Thin underline"), ("ul2", "Thick underline")):
                menu.add_radiobutton(label=label, value=value, variable=self.ul_var,
                                     command=lambda: self.set_underline(self.ul_var.get()))
            mb["menu"] = menu
            mb.pack(side="left", padx=(0, 2))
            self.ul_button = mb
        if "reverse" in f:
            toggle(bar, "Reverse", "reverse")
        if "small" in f:
            toggle(bar, "Small", "small")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Label(bar, text="Size:").pack(side="left", padx=(0, 4))
        names = [n for n, _, _ in SIZE_PRESETS if printer["sizes"] == "all" or n in printer["sizes"]]
        if printer["sizes"] == "all":
            names.append(CUSTOM_SIZE)
        self.size_box = ttk.Combobox(bar, values=names, state="readonly", width=10)
        self.size_box.set("Normal")
        self.size_box.bind("<<ComboboxSelected>>", lambda e: self.choose_size())
        self.size_box.pack(side="left")
        ttk.Button(bar, text="Title", style="Toolbutton", command=self.apply_title).pack(side="left", padx=(6, 0))

        # Toolbar: line styles and inserts
        bar2 = ttk.Frame(outer)
        bar2.pack(fill="x", pady=(4, 6))
        self.align_var = tk.StringVar(value="left")
        if "align" in f:
            for value, label in (("left", "Left"), ("center", "Center"), ("right", "Right")):
                ttk.Radiobutton(bar2, text=label, value=value, variable=self.align_var, style="Toolbutton",
                                command=lambda v=value: self.set_align(v)).pack(side="left", padx=(0, 2))
        for tag, label in (("upside", "Upside-down"), ("sideways", "Sideways")):
            if tag in f:
                v = self.vars[tag] = tk.BooleanVar()
                ttk.Checkbutton(bar2, text=label, variable=v, style="Toolbutton",
                                command=lambda t=tag, v=v: self.set_line_tag(t, v.get())).pack(side="left", padx=(0, 2))
        ttk.Separator(bar2, orient="vertical").pack(side="left", fill="y", padx=6)
        if "picture" in f:
            ttk.Button(bar2, text="Picture...", style="Toolbutton", command=self.insert_picture).pack(side="left", padx=(0, 2))
        if "qr" in f:
            ttk.Button(bar2, text="QR code...", style="Toolbutton", command=self.insert_qr).pack(side="left", padx=(0, 2))
        ttk.Button(bar2, text="[ ] Tick box", style="Toolbutton", command=self.insert_tickbox).pack(side="left", padx=(0, 2))
        ttk.Button(bar2, text="Divider", style="Toolbutton", command=self.insert_divider).pack(side="left")

        # The page: exactly as wide as the paper, one screen pixel per printer dot
        page_row = ttk.Frame(outer)
        page_row.pack(fill="both", expand=True)
        self.gutter = tk.Canvas(page_row, width=40, highlightthickness=0, background=self.cget("background"))
        self.gutter.pack(side="left", fill="y")
        pad = 8
        page = tk.Frame(page_row, width=printer["width_dots"] + 2 * pad + 2, height=520,   # +2 for the border
                        background="white", highlightthickness=1, highlightbackground="#bbbbbb")
        page.pack(side="left", fill="y", expand=False)
        page.pack_propagate(False)
        self.text = RichText(page, self, wrap="word", undo=True, padx=pad, pady=pad, borderwidth=0,
                             highlightthickness=0, background="white", insertwidth=2,
                             font=self.font_for("A", 1, False), spacing1=0, spacing3=0)
        scroll = ttk.Scrollbar(page_row, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(fill="both", expand=True)
        scroll.pack(side="left", fill="y")

        self.text.tag_configure("ul1", underline=True)
        self.text.tag_configure("ul2", underline=True)
        self.text.tag_configure("align_center", justify="center")
        self.text.tag_configure("align_right", justify="right")
        self.text.tag_configure("upside", background="#fff1dc")
        self.text.tag_configure("sideways", background="#ece6f7")
        self.text.tag_configure("reverse", background="black", foreground="white")
        self.text.tag_raise("reverse")
        self.text.tag_raise("sel")
        self.text.bind("<<Modified>>", lambda e: self.update_title())
        self.bind_shortcuts(self.text)
        self.text.bind("<Return>", self.on_return)
        self.text.bind("<KP_Enter>", self.on_return)
        self.text.bind("<Control-z>", self.undo_clear)
        self.text.bind("<Double-Button-1>", self.on_double_click)
        self.text.bind("<Button-3>", self.on_right_click)

        # Bottom row: connection, spacing, print
        info = ttk.Label(outer, foreground="gray", text=self.paper_note())
        info.pack(anchor="w", pady=(6, 0))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(6, 0))
        self.print_button = ttk.Button(buttons, text="Print  (Ctrl+P)", command=self.print_receipt)
        self.print_button.pack(side="right")
        ttk.Button(buttons, text="Clear", command=self.clear_page).pack(side="right", padx=(0, 8))
        self.feed_var = tk.IntVar(value=printer["default_feed"])
        ttk.Spinbox(buttons, from_=0, to=10, width=3, textvariable=self.feed_var).pack(side="right", padx=(0, 12))
        ttk.Label(buttons, text="Blank lines at end:").pack(side="right", padx=(0, 4))
        address = self.settings["addresses"].get(self.printer_key)
        if not address and printer["connection"] == "bluetooth":
            address = find_bluetooth_port(printer["bluetooth_name"])   # first run on a new computer
        self.address_var = tk.StringVar(value=address or printer["default_address"])
        ttk.Label(buttons, text=printer["address_label"]).pack(side="left")
        ttk.Entry(buttons, textvariable=self.address_var,
                  width=14 if printer["connection"] == "network" else 7).pack(side="left", padx=(4, 0))
        if printer["connection"] == "bluetooth":
            ttk.Button(buttons, text="Find", width=5, command=self.find_port).pack(side="left", padx=(4, 0))

        self.status = ttk.Label(outer, text=f"Ready to print on the {printer['name']}.", foreground="gray")
        self.status.pack(anchor="w", pady=(8, 0))

        if doc:
            self.load_content(doc)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.update_title()
        self.text.focus_set()

        self.geometry("")
        self.update_idletasks()
        self.minsize(self.winfo_reqwidth(), 400)
        self.status.config(wraplength=self.winfo_reqwidth() - 30)
        self.schedule_refresh()

    def paper_note(self):
        p = self.printer
        note = f"{p['paper']} paper: {p['width_dots'] // p['char_dots']['A']} characters per line"
        if "B" in p["char_dots"]:
            note += f" ({p['width_dots'] // p['char_dots']['B']} in Small)"
        if "upside" in p["features"]:
            note += ".   Shaded lines print upside-down (orange) or sideways (purple)."
        return note

    def update_title(self):
        name = Path(self.file_path).stem if self.file_path else "Untitled"
        star = "*" if self.text is not None and self.text.edit_modified() else ""
        self.title(f"{star}{name} - Thermal Word Processor - {self.printer['name']}")

    # ---------- fonts ----------

    def font_for(self, family, w, bold):
        """A screen font whose characters are exactly as wide as the printed ones (1 px per dot)."""
        target = self.printer["char_dots"].get(family, 12) * w
        key = (target, bold)
        if key not in self.fonts:
            face = "Consolas" if "Consolas" in tkfont.families(self) else "Courier New"
            probe = tkfont.Font(self, family=face, size=-10)
            best = 4
            for px in range(max(4, int(target * 1.2)), int(target * 2.6) + 2):
                probe.configure(size=-px)
                m = probe.measure("0")
                if m <= target:
                    best = px
                if m >= target:
                    break
            self.fonts[key] = tkfont.Font(self, family=face, size=-best, weight="bold" if bold else "normal")
        return self.fonts[key]

    def font_tag(self, tags):
        fam = font_family(tags, self.printer)
        w = size_of(tags)[0]
        bold = "bold" in tags and "bold" in self.printer["features"]
        name = f"font_{fam}{w}{'b' if bold else ''}"
        if name not in self.text.tag_names():
            self.text.tag_configure(name, font=self.font_for(fam, w, bold))
            self.text.tag_lower(name)
        return name

    def refresh_fonts(self, start="1.0", end="end"):
        for name in self.text.tag_names():
            if name.startswith("font_"):
                self.text.tag_remove(name, start, end)
        for text, tags, index in self.segments(start, end):
            self.text.tag_add(self.font_tag(tags), index, f"{index}+{len(text)}c")

    def segments(self, start="1.0", end="end-1c"):
        """[(text, style tags, index)] for every run of identically styled text in a range."""
        active = {t for t in self.text.tag_names(start) if is_style(t)}
        out = []
        for key, value, index in self.text.dump(start, end, text=True, tag=True):
            if key == "tagon" and is_style(value):
                active.add(value)
            elif key == "tagoff":
                active.discard(value)
            elif key == "text":
                out.append((value, frozenset(active), index))
        return out

    # ---------- styling ----------

    def selection(self):
        rng = self.text.tag_ranges("sel")
        return (str(rng[0]), str(rng[1])) if rng else None

    def typing_tags(self):
        if self.pending is not None:
            return set(self.pending)
        at_line_start = self.text.compare("insert", "==", "insert linestart")
        source = "insert" if at_line_start else "insert-1c"
        return {t for t in self.text.tag_names(source) if is_style(t)}

    def edited(self):
        self.text.edit_modified(True)
        self.schedule_refresh()

    def set_inline(self, tag, on, group=()):
        rng = self.selection()
        if rng:
            for g in group:
                self.text.tag_remove(g, *rng)
            (self.text.tag_add if on else self.text.tag_remove)(tag, *rng)
            self.refresh_fonts(*rng)
            self.edited()
        else:
            tags = self.typing_tags() - set(group)
            tags = tags | {tag} if on else tags - {tag}
            self.pending = tags
        self.text.focus_set()

    def toggle_shortcut(self, tag):
        if tag in self.printer["features"]:
            v = self.vars[tag]
            v.set(not v.get())
            self.set_inline(tag, v.get())

    def set_underline(self, value):
        rng = self.selection()
        if rng:
            self.text.tag_remove("ul1", *rng)
            self.text.tag_remove("ul2", *rng)
            if value != "off":
                self.text.tag_add(value, *rng)
            self.edited()
        else:
            tags = self.typing_tags() - {"ul1", "ul2"}
            self.pending = tags | ({value} if value != "off" else set())
        self.text.focus_set()

    def cycle_underline(self):
        if "underline" in self.printer["features"]:
            order = ["off", "ul1", "ul2"]
            self.ul_var.set(order[(order.index(self.ul_var.get()) + 1) % 3])
            self.set_underline(self.ul_var.get())

    def size_tags(self):
        return [t for t in self.text.tag_names() if t.startswith("size_")]

    def choose_size(self):
        name = self.size_box.get()
        if name == CUSTOM_SIZE:
            w, h = size_of(self.typing_tags())
            dialog = SizeDialog(self, w, h)
            if not dialog.result:
                self.schedule_refresh()
                return
            w, h = dialog.result
        else:
            _, w, h = next(p for p in SIZE_PRESETS if p[0] == name)
        self.apply_size(w, h)

    def apply_size(self, w, h, rng=None):
        tag = f"size_{w}x{h}"
        rng = rng or self.selection()
        if rng:
            for t in self.size_tags():
                self.text.tag_remove(t, *rng)
            if (w, h) != (1, 1):
                self.text.tag_add(tag, *rng)
            self.refresh_fonts(*rng)
            self.edited()
        else:
            tags = {t for t in self.typing_tags() if not t.startswith("size_")}
            self.pending = tags | ({tag} if (w, h) != (1, 1) else set())
        self.text.focus_set()

    def line_span(self):
        """(first line, last line) touched by the selection, or the cursor's line."""
        rng = self.selection()
        start, end = rng if rng else ("insert", "insert")
        first = int(self.text.index(start).split(".")[0])
        last_index = self.text.index(end)
        last = int(last_index.split(".")[0])
        if rng and last > first and last_index.endswith(".0"):
            last -= 1   # a selection ending at the start of a line doesn't include that line
        return first, last

    def set_line_tag(self, tag, on, remove=()):
        first, last = self.line_span()
        for line in range(first, last + 1):
            span = (f"{line}.0", f"{line}.end+1c")
            for t in remove:
                self.text.tag_remove(t, *span)
            (self.text.tag_add if on else self.text.tag_remove)(tag, *span)
        self.pending = None
        self.edited()
        self.text.focus_set()

    def set_align(self, value):
        if "align" not in self.printer["features"]:
            return
        self.align_var.set(value)
        tag = {"center": "align_center", "right": "align_right"}.get(value)
        self.set_line_tag(tag or "align_center", bool(tag), remove=("align_center", "align_right"))

    def on_return(self, event=None):
        """Enter ends the current line and starts a plain one (Normal, left, upright).

        Each line's style lives on its characters *and* its line break. Without this, Enter would
        push the styled line break down onto the new line, and the new line would copy its style.
        Splitting a line in the middle keeps the text after the cursor as it was.
        """
        rng = self.selection()
        if rng:
            self.text.delete(*rng)
        tags = self.typing_tags()
        self.text.insert("insert", "\n", " ".join(sorted(tags) + [self.font_tag(tags)]))
        empty_lines = []
        if self.text.get("insert") == "\n":                        # Enter at the end: new line below is empty
            empty_lines.append(self.text.index("insert"))
        if self.text.compare("insert-1c", "==", "insert-1c linestart"):   # Enter at the start: empty line above
            empty_lines.append(self.text.index("insert-1c"))
        for index in empty_lines:   # an empty line starts plain: clear the style off its line break
            for t in self.text.tag_names(index):
                if is_style(t) or t.startswith("font_"):
                    self.text.tag_remove(t, index)
            self.text.tag_add(self.font_tag(set()), index)
        self.pending = None
        self.text.see("insert")
        self.edited()
        return "break"

    def clear_page(self):
        """Empty the page. Ctrl+Z straight afterwards brings it all back."""
        if not self.text.get("1.0", "end-1c") and not self.objects:
            return
        self.cleared = (self.serialize(), self.file_path, self.text.edit_modified())
        self.text.delete("1.0", "end")
        self.objects.clear()
        self.photos.clear()
        self.pending = None
        self.file_path = None   # so Save can't overwrite the old file with a blank page
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.update_title()
        self.set_status("Cleared. Press Ctrl+Z to bring it back.")
        self.text.focus_set()

    def undo_clear(self, event=None):
        if self.cleared and not self.text.get("1.0", "end-1c") and not self.objects:
            doc, path, modified = self.cleared
            self.cleared = None
            self.load_content(doc)
            self.file_path = path
            self.text.edit_reset()
            self.text.edit_modified(modified)
            self.update_title()
            self.set_status("Brought back what was cleared.")
            return "break"
        return None   # otherwise a normal undo

    def apply_title(self):
        """Big, bold and centered, for the whole line."""
        first, last = self.line_span()
        rng = (f"{first}.0", f"{last}.end+1c")
        if "bold" in self.printer["features"]:
            self.text.tag_add("bold", *rng)
        self.apply_size(2, 2, rng)
        self.set_align("center")

    # ---------- inserting things ----------

    def start_new_line(self):
        if self.text.compare("insert", "!=", "insert linestart"):
            self.text.insert("insert", "\n", " ".join(self.line_tags_here()))

    def line_tags_here(self):
        return {t for t in self.typing_tags() if t in LINE_TAGS}

    def insert_object(self, obj):
        self.start_new_line()
        line_tags = self.line_tags_here()
        photo = self.preview_for(obj)
        name = self.text.image_create("insert", image=photo)
        self.photos[name] = photo
        self.objects[name] = obj
        for t in line_tags:
            self.text.tag_add(t, "insert-1c")
        # keep the picture on its own line, with the cursor on the line after it
        if self.text.get("insert") == "\n" and self.text.compare("insert lineend", "!=", "end-1c"):
            self.text.mark_set("insert", "insert+1c")
        else:
            self.text.insert("insert", "\n", " ".join(line_tags))
        self.edited()
        self.text.see("insert")
        self.text.focus_set()

    def preview_for(self, obj):
        if obj["type"] == "qr":
            img = qr_preview(obj["data"], obj["size"], self.printer["width_dots"])
        else:
            img = dither_picture(obj["gray"], self.ink_var.get()).convert("L")
        return ImageTk.PhotoImage(img, master=self)

    def insert_picture(self):
        path = filedialog.askopenfilename(
            parent=self, title="Choose a picture", initialdir=self.settings.get("folder") or None,
            filetypes=[("Pictures", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tif *.tiff"), ("All files", "*.*")])
        if not path:
            return
        size = ChoiceDialog(self, "Picture size", "How wide should the picture print?",
                            list(PICTURE_SIZES), "Full width").result
        if not size:
            return
        try:
            # Keep a full-width copy, so the picture can be resized later without getting blurry
            source = prepare_picture(path, self.printer["width_dots"])
        except (OSError, ValueError) as err:
            self.set_status(f"Couldn't open that picture. ({err})", "red")
            return
        self.insert_object({"type": "picture", "source": source, "size": size,
                            "gray": scale_picture(source, size, self.printer["width_dots"])})
        self.set_status("Picture added. Double-click it to change its size. "
                        "Change how dark pictures print in Printer > Picture darkness.")

    def insert_qr(self):
        result = QRDialog(self).result
        if not result:
            return
        data, size = result
        try:
            qr_geometry(data, size, self.printer["width_dots"])
        except ValueError as err:
            self.set_status(str(err), "red")
            return
        self.insert_object({"type": "qr", "data": data, "size": size})
        self.set_status("QR code added. Double-click it to edit it. The preview is a look-alike; "
                        "the printer draws the real, scannable code.")

    def object_at(self, x, y):
        """Name of the picture or QR code under a point in the editor, or None."""
        index = self.text.index(f"@{x},{y}")
        for candidate in (index, self.text.index(f"{index}-1c")):
            for key, value, _ in self.text.dump(candidate, image=True):
                box = self.text.bbox(candidate)
                if (key == "image" and value in self.objects and box
                        and box[0] <= x < box[0] + box[2] and box[1] <= y < box[1] + box[3]):
                    return value
        return None

    def qr_at(self, x, y):
        name = self.object_at(x, y)
        return name if name and self.objects[name]["type"] == "qr" else None

    def edit_object(self, name):
        (self.edit_qr if self.objects[name]["type"] == "qr" else self.resize_picture)(name)

    def resize_picture(self, name):
        obj = self.objects[name]
        current = obj.get("size", "Full width")
        size = ChoiceDialog(self, "Picture size", "How wide should the picture print?",
                            list(PICTURE_SIZES), current).result
        if not size or size == current:
            return
        source = obj.get("source", obj["gray"])
        new = {"type": "picture", "source": source, "size": size,
               "gray": scale_picture(source, size, self.printer["width_dots"])}
        photo = self.preview_for(new)
        self.objects[name] = new
        self.photos[name] = photo
        self.text.image_configure(name, image=photo)
        self.edited()
        wanted = round(self.printer["width_dots"] * PICTURE_SIZES[size]) // 8 * 8
        note = " (It's as big as the original picture allows.)" if new["gray"].width < wanted else ""
        self.set_status(f"Picture is now {size.lower()}.{note}")
        self.text.focus_set()

    def edit_qr(self, name):
        obj = self.objects[name]
        result = QRDialog(self, obj["data"], obj["size"], title="Edit QR code").result
        if not result or result == (obj["data"], obj["size"]):
            return
        data, size = result
        try:
            qr_geometry(data, size, self.printer["width_dots"])
        except ValueError as err:
            self.set_status(f"{err} The old QR code is unchanged.", "red")
            return
        new = {"type": "qr", "data": data, "size": size}
        photo = self.preview_for(new)
        self.objects[name] = new
        self.photos[name] = photo
        self.text.image_configure(name, image=photo)
        self.edited()
        self.set_status("QR code updated.")
        self.text.focus_set()

    def on_double_click(self, event):
        name = self.object_at(event.x, event.y)
        if name:
            self.edit_object(name)
            return "break"   # don't also select a word
        return None

    def on_right_click(self, event):
        name = self.object_at(event.x, event.y)
        if name:
            menu = tk.Menu(self, tearoff=False)
            if self.objects[name]["type"] == "qr":
                menu.add_command(label="Edit QR code...", command=lambda: self.edit_qr(name))
            else:
                menu.add_command(label="Change picture size...", command=lambda: self.resize_picture(name))
            menu.tk_popup(event.x_root, event.y_root)
            return "break"
        return None

    def insert_tickbox(self):
        tags = self.typing_tags()
        self.text.insert("insert linestart", "[ ] ", " ".join(sorted(tags) + [self.font_tag(tags)]))
        self.edited()
        self.text.focus_set()

    def insert_divider(self):
        self.start_new_line()
        line_tags = self.line_tags_here()
        count = self.printer["width_dots"] // self.printer["char_dots"]["A"]
        tags = " ".join(sorted(line_tags) + [self.font_tag(line_tags)])
        self.text.insert("insert", "\u2500" * count + "\n", tags)
        self.edited()
        self.text.focus_set()

    # ---------- keeping the toolbar and margin up to date ----------

    def schedule_refresh(self):
        if not self._refresh_queued:
            self._refresh_queued = True
            self.after_idle(self.refresh_ui)

    def refresh_ui(self):
        self._refresh_queued = False
        if self.text is None or not self.text.winfo_exists():
            return
        # On paper the black bar only covers the words, so don't let line breaks show it to the edge
        ranges = self.text.tag_ranges("reverse")
        for start, end in zip(ranges[0::2], ranges[1::2]):
            idx = self.text.search("\n", start, stopindex=end)
            while idx:
                self.text.tag_remove("reverse", idx)
                idx = self.text.search("\n", f"{idx}+1c", stopindex=end)
        tags = self.typing_tags()
        for tag, v in self.vars.items():
            v.set(tag in tags)
        if "underline" in self.printer["features"]:
            self.ul_var.set("ul2" if "ul2" in tags else "ul1" if "ul1" in tags else "off")
            self.ul_button.configure(text={"off": "Underline", "ul1": "Underline: thin",
                                           "ul2": "Underline: thick"}[self.ul_var.get()])
        w, h = size_of(tags)
        self.size_box.set(next((n for n, pw, ph in SIZE_PRESETS if (pw, ph) == (w, h)), f"{w} x {h}"))
        self.align_var.set(align_of(tags))
        self.draw_gutter()

    def draw_gutter(self):
        """Labels in the left margin for lines that print upside-down or sideways."""
        g = self.gutter
        g.delete("all")
        index = self.text.index("@0,0 linestart")
        while True:
            info = self.text.dlineinfo(index)
            if info is None:
                break
            tags = self.text.tag_names(index)
            y = info[1] + info[3] // 2
            if "upside" in tags:
                g.create_text(36, y, text="180\u00b0", anchor="e", fill="#c26a00", font=("Segoe UI", 8, "bold"))
            elif "sideways" in tags:
                g.create_text(36, y, text="90\u00b0", anchor="e", fill="#6a4bb0", font=("Segoe UI", 8, "bold"))
            nxt = self.text.index(f"{index}+1line")
            if nxt == index:
                break
            index = nxt

    # ---------- documents ----------

    def document_lines(self):
        """The document as lines of styled text and objects, for printing."""
        lines = [{"items": [], "nl_tags": frozenset()}]
        active = set()
        for key, value, index in self.text.dump("1.0", "end-1c", text=True, tag=True, image=True):
            if key == "tagon" and is_style(value):
                active.add(value)
            elif key == "tagoff":
                active.discard(value)
            elif key == "text":
                parts = value.split("\n")
                for k, part in enumerate(parts):
                    if part:
                        lines[-1]["items"].append(("text", part, frozenset(active)))
                    if k < len(parts) - 1:
                        lines[-1]["nl_tags"] = frozenset(active)
                        lines.append({"items": [], "nl_tags": frozenset()})
            elif key == "image":
                lines[-1]["items"].append(("object", self.objects.get(value), frozenset(active)))
        if not lines[-1]["items"] and len(lines) > 1:
            lines.pop()   # the empty line after the last line break doesn't print
        return lines

    def serialize(self):
        content = []
        active = set()
        for key, value, index in self.text.dump("1.0", "end-1c", text=True, tag=True, image=True):
            if key == "tagon" and is_style(value):
                active.add(value)
            elif key == "tagoff":
                active.discard(value)
            elif key == "text":
                content.append({"text": value, "tags": sorted(active)})
            elif key == "image" and value in self.objects:
                obj = dict(self.objects[value])
                if obj["type"] == "picture":   # save the full-size copy; the chosen size is rebuilt on open
                    gray = obj.pop("gray")
                    buf = io.BytesIO()
                    obj.pop("source", gray).save(buf, format="PNG")
                    obj["png"] = base64.b64encode(buf.getvalue()).decode("ascii")
                    obj.setdefault("size", "Full width")
                content.append({"object": obj, "tags": sorted(active)})
        return {"app": "Thermal Word Processor", "version": 1, "content": content}

    def load_content(self, doc):
        self.text.delete("1.0", "end")
        for item in doc.get("content", []):
            tags = {t for t in item.get("tags", []) if is_style(t)}
            if "text" in item:
                self.text.insert("end-1c", item["text"], " ".join(sorted(tags) + [self.font_tag(tags)]))
            elif "object" in item:
                obj = dict(item["object"])
                if obj.get("type") == "picture" and "png" in obj:
                    obj["source"] = Image.open(io.BytesIO(base64.b64decode(obj.pop("png")))).convert("L")
                    if obj.get("size") not in PICTURE_SIZES:
                        obj["size"] = "Full width"   # files saved before resizing existed
                    obj["gray"] = scale_picture(obj["source"], obj["size"], self.printer["width_dots"])
                elif obj.get("type") != "qr":
                    continue
                try:
                    photo = self.preview_for(obj)
                except ValueError:
                    continue   # e.g. a QR code too big for this paper
                index = self.text.index("end-1c")
                name = self.text.image_create(index, image=photo)
                self.photos[name] = photo
                self.objects[name] = obj
                for t in tags:
                    self.text.tag_add(t, index)
        self.text.mark_set("insert", "1.0")

    def confirm_discard(self):
        if not self.text.edit_modified():
            return True
        answer = messagebox.askyesnocancel("Save changes?", "Save the current receipt before continuing?", parent=self)
        if answer is None:
            return False
        return self.save_doc() if answer else True

    def new_doc(self):
        if self.confirm_discard():
            self.file_path = None
            self.build_window()

    def open_doc(self):
        if not self.confirm_discard():
            return
        path = filedialog.askopenfilename(parent=self, title="Open a receipt", initialdir=self.settings.get("folder") or None,
                                          filetypes=[("Receipts", "*.receipt"), ("All files", "*.*")])
        if not path:
            return
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            self.set_status(f"Couldn't open that file. ({err})", "red")
            return
        self.file_path = path
        self.remember_folder(path)
        self.build_window(doc)
        self.set_status(f"Opened {Path(path).name}.")

    def save_doc(self, ask=False):
        path = self.file_path
        if ask or not path:
            path = filedialog.asksaveasfilename(parent=self, title="Save receipt", defaultextension=".receipt",
                                                initialdir=self.settings.get("folder") or None,
                                                filetypes=[("Receipts", "*.receipt")])
            if not path:
                return False
        try:
            Path(path).write_text(json.dumps(self.serialize()), encoding="utf-8")
        except OSError as err:
            self.set_status(f"Couldn't save. ({err})", "red")
            return False
        self.file_path = path
        self.remember_folder(path)
        self.text.edit_modified(False)
        self.update_title()
        self.set_status(f"Saved {Path(path).name}.")
        return True

    def remember_folder(self, path):
        self.settings["folder"] = str(Path(path).parent)
        save_settings(self.settings)

    def quit_app(self):
        if self.confirm_discard():
            self.destroy()

    # ---------- printers ----------

    def switch_printer(self):
        key = self.printer_var.get()
        if self.busy:   # don't swap the window out from under a print that's still sending
            self.printer_var.set(self.printer_key)
            return
        if key == self.printer_key:
            return
        modified = self.text.edit_modified()
        doc = self.serialize()
        self.settings["printer"] = key
        save_settings(self.settings)
        self.build_window(doc)
        self.text.edit_modified(modified)
        self.update_title()

    def change_darkness(self):
        self.settings["ink_limit"] = self.ink_var.get()
        save_settings(self.settings)
        for name, obj in self.objects.items():
            if obj["type"] == "picture":
                photo = self.preview_for(obj)
                self.photos[name] = photo
                self.text.image_configure(name, image=photo)

    def set_status(self, text, color="gray"):
        self.status.config(text=text, foreground=color)

    def print_receipt(self):
        if self.busy:
            return
        lines = self.document_lines()
        if not any(line["items"] for line in lines):
            self.set_status("Nothing to print yet. Type something first.", "darkorange")
            return
        try:
            feed = max(0, int(self.feed_var.get()))
        except (tk.TclError, ValueError):
            feed = self.printer["default_feed"]
        try:
            job, skipped = build_job(self.printer, lines, feed, self.ink_var.get())
        except ValueError as err:
            self.set_status(str(err), "red")
            return
        address = self.address_var.get().strip() or self.printer["default_address"]

        self.busy = True
        self.print_button.state(["disabled"])
        self.set_status(f"Sending to the {self.printer['name']} at {address}...")
        # Connecting can take a few seconds, so do it off the UI thread
        threading.Thread(target=self._send_in_background, args=(self.printer_key, address, job, skipped),
                         daemon=True).start()

    def _send_in_background(self, key, address, job, skipped):
        printer = PRINTERS[key]
        ok, color = False, "red"
        try:
            if printer["connection"] == "network":
                warning = send_network(address, job, check="status" in printer["features"])
            else:
                try:
                    warning = send_bluetooth(address, job)
                except OSError:
                    # Maybe the saved port is wrong (new computer, re-paired printer): look it up and retry once
                    found = find_bluetooth_port(printer["bluetooth_name"])
                    if not found or found.upper() == address.upper():
                        raise
                    address = found
                    warning = send_bluetooth(address, job)
            ok, color = True, "green"
            text = "Printed!"
            if skipped:
                text += f" (Skipped {' and '.join(sorted(s + 's' for s in skipped))}: the {printer['name']} can't print those.)"
            if warning:
                text += f" {warning}"
                color = "darkorange"
        except PrinterProblem as err:
            text = str(err)
        except OSError as err:
            reason = getattr(err, "strerror", None) or err
            if printer["connection"] == "network":
                text = (f"Couldn't reach the {printer['name']} at {address}. Is it on and plugged into the router? "
                        f"If its address changed, it prints a slip with the new one. ({reason})")
            else:
                text = (f"Couldn't reach the {printer['name']} on {address}. Is it switched on, and paired with "
                        f"this computer in Settings > Bluetooth? ({reason})")
        self.call_soon(self._finish, key, address, ok, text, color)

    def _finish(self, key, address, ok, text, color):
        self.busy = False
        if ok:   # remember the address that worked for next time
            self.settings["addresses"][key] = address
            save_settings(self.settings)
            if key == self.printer_key:
                self.address_var.set(address)
        if self.print_button.winfo_exists():
            self.print_button.state(["!disabled"])
            self.set_status(text, color)

    def find_port(self):
        port = find_bluetooth_port(self.printer["bluetooth_name"])
        if port:
            self.address_var.set(port)
            self.set_status(f"Found the {self.printer['name']} on {port}.", "green")
        else:
            self.set_status(f"Couldn't find a paired \"{self.printer['bluetooth_name']}\". Turn the printer on and "
                            f"pair it in Settings > Bluetooth & devices > Add device, then click Find again.",
                            "darkorange")

    # ---------- talking to the window from background threads ----------

    def call_soon(self, func, *args):
        """Run func(*args) on the window's own thread. Safe to call from any thread."""
        self.ui_queue.put((func, args))

    def poll_ui_queue(self):
        while not self.ui_queue.empty():
            func, args = self.ui_queue.get()
            func(*args)
        self.after(100, self.poll_ui_queue)

    # ---------- updates ----------

    def check_for_updates(self, manual=False):
        if not FROZEN:
            if manual:
                messagebox.showinfo("Updates", f"This is version {APP_VERSION}, running as a Python script.\n\n"
                                    "Automatic updates are for the ThermalWordProcessor.exe version.", parent=self)
            return
        if manual:
            self.set_status("Checking for updates...")

        def work():
            try:
                release = updater.check(UPDATE_REPO, APP_VERSION)
                self.call_soon(self.offer_update, release, manual)
            except updater.UpdateError as err:
                if manual:
                    self.call_soon(self.set_status, f"Couldn't check for updates. {err}", "darkorange")
        threading.Thread(target=work, daemon=True).start()

    def offer_update(self, release, manual):
        if release is None:
            if manual:
                self.set_status(f"You have the latest version ({APP_VERSION}).", "green")
            return
        notes = (release["notes"] or "").strip()
        notes = f"\n\nWhat's new:\n{notes[:600]}" if notes else ""
        if not messagebox.askyesno("Update available",
                                   f"Version {release['version']} is available (you have {APP_VERSION}).{notes}"
                                   "\n\nUpdate now? The app will restart.", parent=self):
            self.set_status("Update skipped. Help > Check for updates whenever you're ready.")
            return
        if not self.confirm_discard():
            return
        self.set_status(f"Downloading version {release['version']}...")

        def work():
            try:
                new_exe = updater.download(release)
                self.call_soon(self.finish_update, new_exe)
            except updater.UpdateError as err:
                self.call_soon(self.set_status, f"The update didn't work, nothing was changed. {err}", "red")
        threading.Thread(target=work, daemon=True).start()

    def finish_update(self, new_exe):
        try:
            updater.install_and_restart(new_exe)
        except updater.UpdateError as err:
            self.set_status(f"The update didn't work, nothing was changed. {err}", "red")
            return
        self.destroy()   # the new version is starting up

    def toggle_auto_updates(self):
        self.settings["check_updates"] = self.auto_update_var.get()
        save_settings(self.settings)

    def show_about(self):
        messagebox.showinfo(f"About {APP_NAME}",
                            f"{APP_NAME}\nVersion {APP_VERSION}{'' if FROZEN else ' (Python script)'}\n\n"
                            f"Updates come from github.com/{UPDATE_REPO}", parent=self)


if __name__ == "__main__":
    ThermalWordProcessor().mainloop()
