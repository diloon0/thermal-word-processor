"""Phone Printing Helper: print to the Epson TM-m30 from a phone, with the laptop as the go-between.

While this window is open, any phone on the same Wi-Fi can open a private web page and send
a message (and an optional photo) to the printer. The page's address has a secret code in it,
so only people you give the link to can print. Close the window to switch phone printing off.

Double-click this file (or the "Phone Printing Helper" shortcut) to start it.
Click "Print setup slip" to print a QR code that opens the page on a phone.
"""

import base64
import io
import json
import queue
import secrets
import socket
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import ttk

from PIL import Image

import thermal_word_processor as twp

PORT = 8580
HELPER_SETTINGS = Path(__file__).with_name("phone_print_settings.json")
MAX_UPLOAD = 15 * 1024 * 1024   # bytes; photos are shrunk on the phone first, so this is generous
MAX_MESSAGE = 3000              # characters
PRINTER = twp.PRINTERS["epson"]

# Phones type "smart" punctuation the printer's character set doesn't have
PLAIN = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                       "—": "-", "…": "...", " ": " ", "•": "*"})


def load_token():
    """The secret part of the page address. Made once, then kept so saved links keep working."""
    try:
        token = json.loads(HELPER_SETTINGS.read_text(encoding="utf-8"))["token"]
        if isinstance(token, str) and len(token) >= 8:
            return token
    except (OSError, ValueError, KeyError, TypeError):
        pass
    token = secrets.token_urlsafe(9)
    HELPER_SETTINGS.write_text(json.dumps({"token": token}, indent=2), encoding="utf-8")
    return token


def printer_settings():
    s = twp.load_settings()   # shared with the word processor: printer address and picture darkness
    return s["addresses"].get("epson", PRINTER["default_address"]), s["ink_limit"]


def laptop_address(printer_address):
    """This laptop's address on the printer's network (what phones type to reach it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((printer_address, twp.NETWORK_PORT))   # no data is sent; this just picks the right network
        return s.getsockname()[0]


def line(text, *tags):
    tags = frozenset(tags)
    return {"items": [("text", text, tags)] if text else [], "nl_tags": tags}


def build_phone_job(name, title, message, photo_bytes, photo_size, ink_limit):
    """Phone submission -> ESC/POS bytes. Raises ValueError with a friendly message."""
    name, title = name.translate(PLAIN).strip()[:40], title.translate(PLAIN).strip()[:80]
    message = message.translate(PLAIN).replace("\r\n", "\n").rstrip()[:MAX_MESSAGE]
    if not title and not message.strip() and not photo_bytes:
        raise ValueError("Type a message or choose a photo first.")   # a name alone isn't worth paper
    lines = []
    if title:
        lines.append(line(title, "size_2x2", "bold", "align_center"))
    if name:
        lines.append(line(f"From {name}", "small", "align_center"))
    if title or name:
        lines.append(line(""))
    for paragraph in message.split("\n") if message else []:
        lines.append(line(paragraph))
    if photo_bytes:
        try:
            source = twp.prepare_picture(io.BytesIO(photo_bytes), PRINTER["width_dots"])
        except (OSError, ValueError, Image.DecompressionBombError):
            raise ValueError("That photo couldn't be opened. Try a different one.")
        size = photo_size if photo_size in twp.PICTURE_SIZES else "Full width"
        if message:
            lines.append(line(""))
        lines.append({"items": [("object", {"type": "picture", "gray": twp.scale_picture(source, size, PRINTER["width_dots"])},
                                 frozenset({"align_center"}))], "nl_tags": frozenset()})
    lines.append(line(""))
    lines.append(line(time.strftime("%a %b %d, %Y  %I:%M %p").replace(" 0", " "), "small", "align_center"))
    job, _ = twp.build_job(PRINTER, lines, PRINTER["default_feed"], ink_limit)
    return job


def setup_slip_job(url):
    lines = [line("Print to " + PRINTER["name"], "size_2x2", "bold", "align_center"), line(""),
             line("Scan with your phone's camera, then add the page to your home screen.", "align_center"),
             line(""),
             {"items": [("object", {"type": "qr", "data": url, "size": "Medium"}, frozenset({"align_center"}))],
              "nl_tags": frozenset()},
             line(url, "small", "align_center"), line(""),
             line("Works on the same Wi-Fi as the printer, while the laptop helper is running.", "small", "align_center")]
    job, _ = twp.build_job(PRINTER, lines, PRINTER["default_feed"], 0.55)
    return job


PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Obelix">
<meta name="theme-color" content="#1f2937">
<title>Obelix</title>
<style>
  :root { color-scheme: light dark; --accent: #2563eb; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 17px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
         background: Canvas; color: CanvasText; }
  main { max-width: 520px; margin: 0 auto; padding: 18px 16px 40px; }
  h1 { font-size: 1.35rem; margin: 4px 0 14px; }
  label { display: block; font-weight: 600; margin: 14px 0 6px; font-size: .95rem; }
  input[type=text], textarea, select { width: 100%; font: inherit; padding: 10px 12px; border-radius: 10px;
         border: 1px solid #8884; background: Field; color: FieldText; }
  textarea { min-height: 9.5em; resize: vertical; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 16px; }
  .hint { font-size: .82rem; opacity: .7; margin-top: 4px; }
  .photo { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .photo button, .photo label.pick { margin: 0; font-weight: 600; padding: 9px 14px; border-radius: 10px;
         border: 1px solid #8886; background: transparent; color: inherit; font-size: .95rem; cursor: pointer; }
  #preview { display: none; max-width: 100%; margin-top: 10px; border-radius: 6px; filter: grayscale(1); }
  #send { width: 100%; margin-top: 20px; padding: 14px; font: inherit; font-weight: 700; color: white;
          background: var(--accent); border: 0; border-radius: 12px; }
  #send:disabled { opacity: .55; }
  #status { margin-top: 14px; min-height: 1.4em; font-weight: 600; }
  .ok { color: #16a34a; } .err { color: #dc2626; }
</style></head>
<body><main>
  <h1>&#x1F9FE; Obelix</h1>
  <label for="name">Your name <span class="hint">(printed as "From ...")</span></label>
  <input id="name" type="text" maxlength="40" autocomplete="given-name" placeholder="Optional">
  <label for="title">Title</label>
  <input id="title" type="text" maxlength="80" placeholder="Optional, printed big">
  <label for="msg">Message</label>
  <textarea id="msg" placeholder="Type your message..."></textarea>
  <div class="hint" id="count">48 characters per printed line</div>
  <label>Photo</label>
  <div class="photo">
    <label class="pick" for="file">Choose photo</label>
    <input id="file" type="file" accept="image/*" hidden>
    <select id="size" style="width:auto"><option>Full width</option><option>Half width</option><option>Third width</option></select>
    <button id="clearphoto" type="button" hidden>Remove</button>
  </div>
  <img id="preview" alt="">
  <button id="send">Print</button>
  <div id="status"></div>
</main>
<script>
const $ = id => document.getElementById(id);
$("name").value = localStorage.getItem("receipt-name") || "";
let photo = null;   // base64 JPEG, already shrunk to the paper's width

$("file").addEventListener("change", async () => {
  const f = $("file").files[0];
  if (!f) return;
  try {
    const img = await new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok(i); i.onerror = bad;
                                                 i.src = URL.createObjectURL(f); });
    const w = Math.min(576, img.naturalWidth), h = Math.round(img.naturalHeight * w / img.naturalWidth);
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, w, h); g.drawImage(img, 0, 0, w, h);
    const url = c.toDataURL("image/jpeg", 0.92);
    photo = url.split(",")[1];
    $("preview").src = url; $("preview").style.display = "block"; $("clearphoto").hidden = false;
  } catch (e) { show("That photo couldn't be opened.", "err"); }
});
$("clearphoto").onclick = () => { photo = null; $("file").value = ""; $("preview").style.display = "none";
                                  $("clearphoto").hidden = true; };

function show(text, cls) { $("status").textContent = text; $("status").className = cls || ""; }

$("send").onclick = async () => {
  const body = { name: $("name").value, title: $("title").value, message: $("msg").value,
                 photo: photo, photo_size: $("size").value };
  if (!body.title.trim() && !body.message.trim() && !photo) { show("Type a message or choose a photo first.", "err"); return; }
  localStorage.setItem("receipt-name", body.name);
  $("send").disabled = true; show("Sending...");
  try {
    const r = await fetch(location.pathname.replace(/\\/$/, "") + "/print",
                          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    show(j.message, j.ok ? "ok" : "err");
    if (j.ok) { $("title").value = ""; $("msg").value = ""; $("clearphoto").onclick(); }
  } catch (e) {
    show("Couldn't reach the laptop. Is the Phone Printing Helper running, and is this phone on the same Wi-Fi?", "err");
  }
  $("send").disabled = false;
};
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    app = None   # set when the server starts

    def page_path(self):
        return f"/p/{self.app.token}"

    def reply(self, status, body, kind="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def reply_json(self, ok, message):
        self.reply(200 if ok else 400, json.dumps({"ok": ok, "message": message}), "application/json")

    def do_GET(self):
        if self.path.rstrip("/") == self.page_path():
            self.reply(200, PAGE)
        else:
            self.reply(404, "Not found.", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path.rstrip("/") != self.page_path() + "/print":
            self.reply(404, "Not found.", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_UPLOAD:
            self.reply_json(False, "That's too big to print. Try a smaller photo.")
            return
        try:
            data = json.loads(self.rfile.read(length))
            photo = base64.b64decode(data["photo"]) if data.get("photo") else None
            fields = [str(data.get(k) or "") for k in ("name", "title", "message")]
        except (ValueError, TypeError, KeyError):
            self.reply_json(False, "Something went wrong sending that. Try again.")
            return
        ok, message = self.app.print_from_phone(*fields, photo, str(data.get("photo_size") or ""),
                                                self.client_address[0])
        self.reply_json(ok, message)

    def log_message(self, *args):
        pass   # keep quiet; the window shows what matters


class HelperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Phone Printing Helper")
        self.resizable(False, False)
        self.token = load_token()
        self.print_lock = threading.Lock()
        self.log_queue = queue.Queue()
        self.printer_address, _ = printer_settings()

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        self.headline = ttk.Label(outer, text="Starting...", font=("Segoe UI", 13, "bold"))
        self.headline.pack(anchor="w")
        ttk.Label(outer, text="Phones on the same Wi-Fi can print by opening this link:").pack(anchor="w", pady=(8, 2))
        self.url_var = tk.StringVar()
        link = ttk.Entry(outer, textvariable=self.url_var, width=46, state="readonly", font=("Consolas", 10))
        link.pack(fill="x")
        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Print setup slip (QR code)", command=self.print_setup_slip).pack(side="left")
        ttk.Button(row, text="Copy link", command=self.copy_link).pack(side="left", padx=(8, 0))
        ttk.Label(outer, text="Recent activity:").pack(anchor="w", pady=(12, 2))
        self.log = tk.Listbox(outer, height=7, width=60, activestyle="none")
        self.log.pack(fill="both")
        ttk.Label(outer, foreground="gray", wraplength=440,
                  text="Keep this window open (minimizing is fine) and the laptop awake. "
                       "Close it to switch phone printing off.").pack(anchor="w", pady=(10, 0))
        self.start_server()
        self.check_log()

    # ---------- server ----------

    def start_server(self):
        try:
            here = laptop_address(self.printer_address)
        except OSError:
            here = "127.0.0.1"
        self.url = f"http://{here}:{PORT}/p/{self.token}"   # plain string: safe to read from any thread
        self.url_var.set(self.url)
        Handler.app = self
        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        except OSError as err:
            self.headline.config(text="Couldn't start", foreground="red")
            self.add_log(f"Port {PORT} is busy. Is the helper already open? ({err.strerror or err})")
            return
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.headline.config(text="Phone printing is ON", foreground="green")
        self.add_log(f"Ready. {PRINTER['called']} is at {self.printer_address}.")
        self.protocol("WM_DELETE_WINDOW", self.stop)

    def stop(self):
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        self.destroy()

    def add_log(self, text):
        # Called from the web server's threads too, and only the window's own thread may touch the
        # window, so messages wait in a queue that check_log() empties
        self.log_queue.put(f"{time.strftime('%I:%M %p').lstrip('0')}  {text}")

    def check_log(self):
        while not self.log_queue.empty():
            self.log.insert(0, self.log_queue.get())
            self.log.delete(50, "end")
        self.after(200, self.check_log)

    # ---------- printing ----------

    def send(self, job):
        address, _ = printer_settings()   # pick up an address changed in the word processor
        with self.print_lock:             # one job at a time
            return twp.send_network(address, job)

    def print_from_phone(self, name, title, message, photo, photo_size, who):
        _, ink_limit = printer_settings()
        try:
            job = build_phone_job(name, title, message, photo, photo_size, ink_limit)
            warning = self.send(job)
        except ValueError as err:
            return False, str(err)
        except twp.PrinterProblem as err:
            self.add_log(f"Couldn't print for {name or who}: {err}")
            return False, str(err)
        except OSError:
            self.add_log(f"Couldn't reach {PRINTER['called']} for {name or who}.")
            return False, f"The laptop couldn't reach {PRINTER['called']}. Is it on and plugged into the router?"
        what = " + photo" if photo else ""
        self.add_log(f"Printed for {name or who}{what}.")
        return True, "Printed! " + (warning or "")

    def print_setup_slip(self):
        def go():
            try:
                warning = self.send(setup_slip_job(self.url))
                self.add_log("Printed the setup slip." + (f" {warning}" if warning else ""))
            except (twp.PrinterProblem, OSError) as err:
                self.add_log(f"Couldn't print the setup slip: {err}")
        threading.Thread(target=go, daemon=True).start()

    def copy_link(self):
        self.clipboard_clear()
        self.clipboard_append(self.url)
        self.add_log("Link copied.")


if __name__ == "__main__":
    HelperApp().mainloop()
