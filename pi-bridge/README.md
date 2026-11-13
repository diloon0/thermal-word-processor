# Obelix bridge: the TM-m30 over Bluetooth

A Raspberry Pi plugged into the printer's USB port, so a laptop can print over Bluetooth.
Useful where the printer and the laptop can't share a network or a cable.

```
laptop  ~~ Bluetooth ~~  Raspberry Pi  == USB cable ==  printer
(Thermal Word Processor)  (obelix_bridge.py)            (Epson TM-m30)
```

The bridge only passes bytes along, in both directions, for one computer at a time. The word
processor still asks the printer about paper and its cover before each job, and waits for the
printer to confirm that the whole job arrived.

**Status: drafted and tested against a pretend printer only.** The byte-passing and the word
processor's side are covered by `test_bridge.py`. The Bluetooth and USB parts have not run on a
real Pi yet; the steps below are the plan for doing that.

## Files

| File | What it's for |
|---|---|
| `obelix_bridge.py` | The bridge program. Runs on the Pi. |
| `bridge.conf` | Its settings, with explanations. Becomes `/boot/firmware/obelix-bridge.conf` on the Pi. |
| `install.sh` | Sets everything up on the Pi. Also `lock`, `unlock` and `remove`. |
| `obelix-bridge.service` | Starts the bridge when the Pi powers on. |
| `try_bridge.py` | Run on the laptop to test the bridge. Prints nothing unless you add `--print`. |
| `test_bridge.py` | Tests that need no Pi and no printer: `python test_bridge.py`. |

## What you need

- A Raspberry Pi with Bluetooth (built for a Pi Zero W) and its power supply
- A microSD card, 8 GB or more
- The printer's USB cable (square USB-B end into the printer), plus a micro-USB "OTG" adapter
  for a Pi Zero, which has no full-size USB socket. Use the Pi's socket marked **USB**, not **PWR**.

## Setting up

### 1. Prepare the SD card (on the laptop)
1. Install **Raspberry Pi Imager** and choose your Pi model, then
   **Raspberry Pi OS (other) → Raspberry Pi OS Lite (32-bit)**.
2. In the Imager's settings: set a hostname (for example `obelix-bridge`), a username and
   password, your home Wi-Fi, and switch on **SSH**.
3. Write the card. Then take it out and put it back in, so the laptop shows its `bootfs` drive.
4. Copy this whole `pi-bridge` folder onto the `bootfs` drive.

### 2. Install (on the Pi)
1. Put the card in the Pi and power it on. The first start takes a few minutes.
2. From the laptop: `ssh yourname@obelix-bridge.local`
3. On the Pi: `sudo bash /boot/firmware/pi-bridge/install.sh`

The last lines should show the service as **active (running)**.

### 3. Test in stages
Each stage adds one thing, so a failure points at one cause.

| Stage | What it proves | How |
|---|---|---|
| A | The Pi sees the printer | Plug the printer in and switch it on. On the Pi: `ls /dev/usb/` shows `lp0`. |
| B | The bridge starts | `journalctl -u obelix-bridge -n 20` ends with `Ready as "Obelix"`. |
| C | Bluetooth pairing | Restart the bridge (`sudo systemctl restart obelix-bridge`) to open pairing. On the laptop: Settings → Bluetooth & devices → Add device → Bluetooth → **Obelix**. If it isn't listed, set **Bluetooth devices discovery** to **Advanced** on the same page. |
| D | The link, with no printer | On the Pi, set `printer = pretend:/tmp/capture.bin` in the settings and restart the bridge. On the laptop: `python try_bridge.py --bluetooth`. All three answers should be `0x12 (fine)`. |
| E | The printer's answers | Set `printer = auto` again and restart. Run `python try_bridge.py --bluetooth` again. Still no paper used. Open the printer's cover and run it again: it should say the cover is open. |
| F | A real print | `python try_bridge.py --bluetooth --print` prints a three-line slip. |
| G | The word processor | **Printer → Obelix by Bluetooth**, click **Check**, then print something. |

Stage D can also be done over Wi-Fi before any pairing: set `tcp_port = 9100`, restart, and run
`python try_bridge.py --tcp <the Pi's address>`. Set it back to `0` afterwards.

### 4. Lock it down
1. In the settings, change `pairing = window` to `pairing = closed`, so nobody new can pair.
2. `sudo bash /boot/firmware/pi-bridge/install.sh lock`, then `sudo reboot`. The card is now
   read-only, so unplugging the Pi without shutting it down can't corrupt it.

Pairings are stored on the card. To pair another computer later: `install.sh unlock`, reboot,
set `pairing = window`, pair, then lock again.

## The Pi's green light

| Light | Meaning |
|---|---|
| Two quick flashes, then a pause | Ready |
| Steady blinking | Ready, and new computers can pair |
| Solid | A job is going through |

## If something goes wrong

| What you see | Likely cause |
|---|---|
| Word processor: "Couldn't find a paired Bluetooth device called Obelix" | Not paired yet, or the bridge's `name` setting was changed. |
| "Couldn't reach Obelix's Bluetooth bridge" | The Pi is off, still starting (allow a minute), or too far away. |
| "The Bluetooth bridge answered, but Obelix didn't" | The printer is off, or its cable isn't in the Pi's **USB** socket. |
| Prints, but **Check** says the printer didn't answer | Linux isn't passing the printer's answers back. Try `printer = pyusb` (after `sudo apt install python3-usb`). |
| `No Bluetooth adapter found` in the log | `sudo rfkill unblock bluetooth`, then restart the bridge. |
| `RegisterProfile` fails in the log | Another program on the Pi is using serial channel 1. Change `channel` in the settings and `BRIDGE_CHANNEL` in the word processor to match. |

Watch the bridge live with `journalctl -u obelix-bridge -f`.
