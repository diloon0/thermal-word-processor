# Thermal Word Processor

A small word processor for thermal receipt printers on Windows. The page is exactly as wide as the paper, so what you see is where lines will break on the receipt.

- **Printers:** Epson TM-m30 (80mm, over the network or a USB cable, cuts the paper itself) and Sunydog / MPT-II (58mm, over Bluetooth). Pick one from the **Printer** menu.
- **Styles:** titles, bold, underline, reverse, small text, sizes up to 8x8, alignment, and upside-down and sideways lines.
- **Extras:** pictures (double-click to resize), QR codes (double-click to edit), tick boxes and dividers.
- **Files:** saves receipts as `.receipt` files.
- **Phone printing:** phones on the same Wi-Fi can print a message or photo from a private web page while the app is open. See below.
- **Finds a moved printer:** if a network printer gets a new address (a new router, another house), the app finds it by itself.

## Download

Get **ThermalWordProcessor.exe** from the [latest release](../../releases/latest). It needs nothing installed: put it anywhere (the Desktop is fine) and double-click it.

The first time, Windows may say *"Windows protected your PC"* because the app isn't signed. Click **More info → Run anyway**.

For the Sunydog, pair the printer first: switch it on, then go to **Settings → Bluetooth & devices → Add device**. The app finds its port by itself (or click **Find**).

For the TM-m30 on a USB cable (where it can't join the network), plug its square USB-B port into the computer and pick **Printer → Obelix by USB cable**. No driver is needed: the app finds the printer by itself, and **Check** shows whether it's plugged in.

The app checks for new versions when it starts and asks before updating. You can turn this off under **Help**.

## Printing from a phone

1. In the app, open **Phone → Phone link and setup slip...** and click **Turn on**. The first time, Windows asks whether to let the app use the network: allow it on **private networks**.
2. Click **Print setup slip**. Scan its QR code with the phone's camera, then add the page to the home screen.
3. Type a message (and pick a photo, on printers that can print pictures) and tap **Print**.

Phones print on the printer picked in the **Printer** menu. The page's link has a secret code in it, so only people you give it to can print. Phone printing stays on while the app is open (minimizing is fine) and comes back on the next time you open it, until you turn it off.

## For the maintainer

- `python thermal_word_processor.py` runs from source (needs Python and Pillow). Add `--phone` to switch phone printing on and show its window.
- `python release.py build` builds `dist\ThermalWordProcessor.exe`.
- `python release.py publish 1.0.1 "What changed"` bumps the version, builds, and (after confirming) pushes and creates the GitHub release that installed copies update from.

Also here: `tm_m30_sampler.py` (a feature sampler receipt).
