"""Print a sampler receipt that shows off what the Epson TM-m30 can do.

Run `python tm_m30_sampler.py` (the printer must be on and on the network).
Each section is labelled, so you can point at what you like and use it elsewhere.
"""

import socket
import sys

PRINTER = sys.argv[1] if len(sys.argv) > 1 else "10.0.0.114"
WIDTH_DOTS = 576

ESC, GS, DLE = b"\x1b", b"\x1d", b"\x10"
INIT = ESC + b"@"
LEFT, CENTER, RIGHT = ESC + b"a\x00", ESC + b"a\x01", ESC + b"a\x02"


def t(text):
    return text.encode("cp437", errors="replace")


def size(w, h):        # character size, 1-8 wide by 1-8 tall
    return GS + b"!" + bytes([((w - 1) << 4) | (h - 1)])


def on_off(cmd, on):
    return cmd + (b"\x01" if on else b"\x00")


BOLD = lambda on: on_off(ESC + b"E", on)
REVERSE = lambda on: on_off(GS + b"B", on)
UPSIDE_DOWN = lambda on: on_off(ESC + b"{", on)
ROTATE_90 = lambda on: on_off(ESC + b"V", on)
FONT_B = lambda on: on_off(ESC + b"M", on)


def underline(n):      # 0 off, 1 thin, 2 thick
    return ESC + b"-" + bytes([n])


def section(name):
    """A reverse-video label bar across the full width."""
    return (b"\n" + LEFT + REVERSE(True) + BOLD(True) + t(f" {name} ".ljust(48)) + b"\n"
            + BOLD(False) + REVERSE(False) + b"\n")


def code2d(cn, fn, payload):     # GS ( k: the printer's built-in 2D codes
    n = len(payload) + 2
    return GS + b"(k" + bytes([n & 0xFF, n >> 8, cn, fn]) + payload


def qr(data, dots=8):
    d = data.encode()
    return (CENTER + code2d(49, 65, b"\x32\x00") + code2d(49, 67, bytes([dots]))
            + code2d(49, 69, b"\x31") + code2d(49, 80, b"0" + d) + code2d(49, 81, b"0") + b"\n")


def pdf417(data):
    d = data.encode()
    return (CENTER + code2d(48, 65, b"\x00") + code2d(48, 66, b"\x00")     # automatic columns/rows
            + code2d(48, 67, b"\x03") + code2d(48, 68, b"\x03")             # module width / row height
            + code2d(48, 69, b"\x30\x01") + code2d(48, 80, b"0" + d) + code2d(48, 81, b"0") + b"\n")


def barcode(kind, data, hri=True):
    """1D barcode. kind 73 = CODE128, 65 = UPC-A."""
    d = data.encode()
    return (CENTER + GS + b"h\x50" + GS + b"w\x02" + GS + b"H" + (b"\x02" if hri else b"\x00")
            + GS + b"k" + bytes([kind, len(d)]) + d + b"\n")


# Thermal dots spread into each other, so about half the dots already looks solid black.
# INK_LIMIT scales every grey down to that share of dots; only the darkest end ramps up to true black.
INK_LIMIT = 0.55


def ink(level, limit=INK_LIMIT):
    """How much of an area to fill with dots to *look* like `level` (0 white .. 1 black) on paper."""
    return limit * level + (1 - limit) * level ** 8


def dither(width, height, shade, limit=INK_LIMIT):
    """Turn shade(x, y) -> 0.0 (white) .. 1.0 (black) into dots with Floyd-Steinberg error diffusion.

    Returns pixel(x, y) -> True for black, ready for raster(). Pixels where shade() is None stay white.
    """
    grid = [[0.0] * width for _ in range(height)]
    mask = [[False] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            s = shade(x, y)
            if s is not None:
                grid[y][x] = ink(max(0.0, min(1.0, s)), limit)
                mask[y][x] = True
    black = [[False] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if not mask[y][x]:
                continue
            old = grid[y][x]
            black[y][x] = old >= 0.5
            err = old - (1.0 if black[y][x] else 0.0)
            for dx, dy, w in ((1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and ny < height and mask[ny][nx]:
                    grid[ny][nx] += err * w / 16
    return lambda x, y: black[y][x]


def raster(width, height, pixel):
    """Black-and-white image: pixel(x, y) -> True for black."""
    row_bytes = (width + 7) // 8
    data = bytearray()
    for y in range(height):
        for bx in range(row_bytes):
            byte = 0
            for bit in range(8):
                x = bx * 8 + bit
                if x < width and pixel(x, y):
                    byte |= 0x80 >> bit
            data.append(byte)
    return (CENTER + GS + b"v0\x00" + bytes([row_bytes & 0xFF, row_bytes >> 8, height & 0xFF, height >> 8])
            + bytes(data) + b"\n")


def gradient(x, y, w=WIDTH_DOTS):
    return x / (w - 1)   # white on the left fading to black on the right


def heart(x, y, w=220, h=200):
    u = (x - w / 2) / (w / 2.6)
    v = -(y - h * 0.42) / (h / 2.6)
    return (u * u + v * v - 1) ** 3 - u * u * v ** 3 <= 0


def shaded_heart(x, y):
    return y / 199 if heart(x, y) else None   # light at the top, dark at the bottom


def read_status():
    """Ask the printer how it's doing before printing (real-time status, DLE EOT)."""
    report = {}
    with socket.create_connection((PRINTER, 9100), timeout=5) as conn:
        for name, n in (("printer", 1), ("cover/feed", 2), ("errors", 3), ("paper", 4)):
            conn.sendall(DLE + b"\x04" + bytes([n]))
            try:
                report[name] = conn.recv(1)[0]
            except (socket.timeout, IndexError):
                report[name] = None
    return report


def describe_status(s):
    lines = []
    if s.get("paper") is not None:
        p = s["paper"]
        lines.append("Paper: " + ("OUT!" if p & 0x60 else "running low" if p & 0x0C else "plenty left"))
    if s.get("cover/feed") is not None:
        lines.append("Cover: " + ("open" if s["cover/feed"] & 0x04 else "closed"))
    if s.get("errors") is not None:
        lines.append("Errors: " + ("cutter problem" if s["errors"] & 0x08 else
                                   "error" if s["errors"] & 0x60 else "none"))
    return lines or ["(the printer didn't answer the status check)"]


def build(status_lines):
    j = INIT
    # Title banner
    j += CENTER + REVERSE(True) + BOLD(True) + size(2, 2) + t(" TM-m30 SAMPLER ") + b"\n" + size(1, 1)
    j += REVERSE(False) + BOLD(False) + t("Everything this printer can do\n")

    j += section("1. TEXT SIZES (width x height)")
    for w, h in ((1, 1), (2, 1), (1, 2), (2, 2), (3, 3)):
        j += size(w, h) + t(f"{w}x{h} Aa") + size(1, 1) + b"\n"
    j += size(6, 6) + t("6x6") + size(1, 1) + b"\n"
    j += size(8, 8) + t("8x8") + size(1, 1) + b"\n"

    j += section("2. TWO FONTS")
    j += t("Font A: 48 characters per line, like so ....!\n")
    j += t("".join(str(i % 10) for i in range(1, 49))) + b"\n"
    j += FONT_B(True) + t("Font B: smaller, 64 characters per line, see how many fit here!!\n")
    j += t("".join(str(i % 10) for i in range(1, 65))) + b"\n" + FONT_B(False)

    j += section("3. STYLES")
    j += t("Normal text\n")
    j += BOLD(True) + t("Bold text\n") + BOLD(False)
    j += underline(1) + t("Thin underline") + underline(0) + b"\n"
    j += underline(2) + t("Thick underline") + underline(0) + b"\n"
    j += REVERSE(True) + t(" Reverse: white on black ") + REVERSE(False) + b"\n"
    # Upside-down only switches on or off at the start of a line, so turn it off after the line ends
    j += UPSIDE_DOWN(True) + t("Upside-down text!") + b"\n" + UPSIDE_DOWN(False)
    j += BOLD(True) + underline(1) + size(2, 1) + t("Mix & match") + size(1, 1) + underline(0) + BOLD(False) + b"\n"

    j += section("4. ALIGNMENT")
    j += LEFT + t("Left\n") + CENTER + t("Center\n") + RIGHT + t("Right\n") + LEFT

    j += section("5. ROTATED 90 DEGREES")
    j += ROTATE_90(True) + size(2, 2) + t("SIDEWAYS") + size(1, 1) + b"\n" + ROTATE_90(False)
    j += t("Back to normal.\n")

    j += section("6. ACCENTS & SYMBOLS (built in)")
    j += t("Café  jalapeño  über  naïve  £5  ½  ±  °F  ¿Qué?\n")
    j += t("Box art: ┌─┬─┐ ├─┼─┤ └─┴─┘  ░▒▓█  α ß π Σ Ω µ  ≈ ≤ ≥ √ ∞\n")
    j += t("[ ] To-do item    [x] Done item\n")

    j += section("7. PICTURES: GRADIENT CALIBRATION")
    j += t("Which bar fades most evenly from white to black?\n")
    for label, limit in (("A", 1.0), ("B", 0.70), ("C", 0.55), ("D", 0.45), ("E", 0.35)):
        note = " (old, too dark)" if limit == 1.0 else " (new default)" if limit == INK_LIMIT else ""
        j += LEFT + t(f"{label}: mid-grey uses {limit / 2:.0%} dots{note}\n")
        j += raster(WIDTH_DOTS, 40, dither(WIDTH_DOTS, 40, gradient, limit))
    j += LEFT
    j += section("8. PICTURES: DRAWN BY THE LAPTOP")
    j += raster(220, 200, dither(220, 200, shaded_heart)) + LEFT

    j += section("9. QR CODE (built in)")
    j += qr("https://en.wikipedia.org/wiki/Thermal_printing")
    j += CENTER + t("Scan me: Wikipedia, Thermal printing\n") + LEFT

    j += section("10. PDF417 (built in, boarding-pass style)")
    j += pdf417("Hello from the TM-m30! This is a PDF417 code.")

    j += section("11. BARCODES (built in)")
    j += t("CODE128:\n") + barcode(73, "{BHELLO-TM-m30") + LEFT
    j += t("UPC-A:\n") + barcode(65, "01234567890") + LEFT

    j += section("12. STATUS REPORT (asked before printing)")
    for line in status_lines:
        j += t(line + "\n")

    j += section("13. AUTO-CUT")
    j += CENTER + t("...and it cuts the paper itself. Bye!\n") + LEFT
    j += b"\n\n" + GS + b"VB\x00"
    return j


if __name__ == "__main__":
    status = read_status()
    print("Status bytes:", status)
    job = build(describe_status(status))
    with socket.create_connection((PRINTER, 9100), timeout=10) as conn:
        conn.sendall(job)
        conn.shutdown(socket.SHUT_WR)
    print(f"Sent {len(job)} bytes to {PRINTER}.")
