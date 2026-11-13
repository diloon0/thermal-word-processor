#!/usr/bin/env python3
"""Obelix bridge: print on a USB receipt printer over Bluetooth.

Runs on a Raspberry Pi (designed for a Pi Zero W with Raspberry Pi OS Lite) that is plugged into
the printer's USB port. A paired computer opens a Bluetooth serial connection to the Pi. Every
byte the computer sends goes to the printer, and every byte the printer answers goes back, so
the computer can ask about paper and the cover just as it does over a network or a USB cable.

The bridge doesn't understand print jobs. It only passes bytes along, one computer at a time.

Settings come from the first of these files that exists (see bridge.conf for what they mean):
    /boot/firmware/obelix-bridge.conf   (on the SD card's small partition, so it can also be
                                         edited by putting the card in a laptop)
    /boot/obelix-bridge.conf
    /etc/obelix-bridge.conf

Needs (install.sh sets these up): bluez, python3-dbus, python3-gi.
Run by hand to watch it work:  sudo python3 obelix_bridge.py -v
"""

import argparse
import configparser
import glob
import logging
import os
import select
import socket
import sys
import threading
import time

log = logging.getLogger("obelix-bridge")

CONFIG_PATHS = ["/boot/firmware/obelix-bridge.conf", "/boot/obelix-bridge.conf", "/etc/obelix-bridge.conf"]
DEFAULTS = {
    "name": "Obelix",          # the Bluetooth name computers see
    "pairing": "window",       # window = new computers can pair for a few minutes after power-on
                               # open = any time; closed = never (only already-paired computers)
    "pairing_minutes": "5",
    "allowed": "",             # optional: Bluetooth addresses allowed to print, separated by commas
    "printer": "auto",         # auto, a device like /dev/usb/lp0, "pyusb", or "pretend:/tmp/capture.bin"
    "usb_vendor": "04b8",      # Epson; only used by printer = pyusb
    "channel": "1",            # Bluetooth serial channel; the word processor expects 1
    "tcp_port": "0",           # e.g. 9100 to also accept jobs over the network (for testing at home)
    "idle_seconds": "120",     # hang up on a computer that connects and then sends nothing
    "led": "yes",              # show the state on the Pi's green light
}
SERIAL_PORT_UUID = "00001101-0000-1000-8000-00805f9b34fb"   # Bluetooth "serial port" profile


# ---------------------------------------------------------------------------------------------
# The printer end
# ---------------------------------------------------------------------------------------------

class PrinterGone(OSError):
    """The printer isn't there (switched off or unplugged)."""


class LpPrinter:
    """The printer as Linux's own USB printer device (/dev/usb/lp0). Needs nothing extra installed."""

    def __init__(self, path="auto"):
        self.path = path
        self.fd = None
        self.can_read = True

    def open(self):
        path = self.path
        if path == "auto":
            found = sorted(glob.glob("/dev/usb/lp*"))
            if not found:
                raise PrinterGone("no USB printer is plugged in and switched on")
            path = found[0]
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as err:
            raise PrinterGone(f"couldn't open {path}: {err.strerror or err}") from err
        self.can_read = True

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def write(self, data, timeout=60):
        """Send bytes to the printer, waiting while it's busy (it prints slower than data arrives)."""
        view = memoryview(data)
        deadline = time.monotonic() + timeout
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("the printer stopped taking data (out of paper, or cover open?)")
            _, ready, _ = select.select([], [self.fd], [], min(left, 1.0))
            if not ready:
                continue
            try:
                sent = os.write(self.fd, view[:4096])
            except BlockingIOError:
                time.sleep(0.01)
                continue
            view = view[sent:]

    def read(self, timeout):
        """Whatever the printer has said within `timeout` seconds (b"" if nothing)."""
        if not self.can_read:
            time.sleep(timeout)
            return b""
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        try:
            return os.read(self.fd, 256)
        except BlockingIOError:
            return b""
        except OSError as err:
            if err.errno == 22:   # EINVAL: this printer or cable can't send answers back
                log.warning("The printer can't be read from; status checks won't work.")
                self.can_read = False
                return b""
            raise


class PyUsbPrinter:
    """The printer through the pyusb library (apt install python3-usb). A fallback for when
    /dev/usb/lp0 prints fine but never passes the printer's answers back."""

    def __init__(self, vendor="04b8"):
        self.vendor = int(vendor, 16)
        self.dev = self.ep_out = self.ep_in = None

    def open(self):
        import usb.core
        import usb.util
        self.usb = usb
        dev = usb.core.find(idVendor=self.vendor)
        if dev is None:
            raise PrinterGone("no USB printer is plugged in and switched on")
        config = dev.get_active_configuration()
        for interface in config:
            if interface.bInterfaceClass == 7:   # 7 = printer
                break
        else:
            raise PrinterGone("that USB device isn't a printer")
        number = interface.bInterfaceNumber
        if dev.is_kernel_driver_active(number):
            dev.detach_kernel_driver(number)   # take it over from /dev/usb/lp0
        usb.util.claim_interface(dev, number)
        direction = usb.util.endpoint_direction
        for endpoint in interface:
            if usb.util.endpoint_type(endpoint.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                self.ep_out = endpoint
            else:
                self.ep_in = endpoint
        if self.ep_out is None:
            raise PrinterGone("the printer has nowhere to send data to")
        self.dev = dev

    def close(self):
        if self.dev is not None:
            try:
                self.usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None

    def write(self, data, timeout=60):
        view = memoryview(data)
        while view:
            sent = self.ep_out.write(bytes(view[:4096]), int(timeout * 1000))
            view = view[sent:]

    def read(self, timeout):
        if self.ep_in is None:
            time.sleep(timeout)
            return b""
        try:
            return bytes(self.ep_in.read(64, max(1, int(timeout * 1000))))
        except self.usb.core.USBTimeoutError:
            return b""


class PretendPrinter:
    """A stand-in printer for testing the Bluetooth link without using paper.

    Everything "printed" is added to a file instead. Status questions (DLE EOT n) get the answer
    a healthy printer gives (0x12), so the word processor's checks pass.
    """

    def __init__(self, path):
        self.path = path
        self.file = None
        self.answers = []
        self.tail = b""
        self.lock = threading.Lock()

    def open(self):
        self.file = open(self.path, "ab")
        self.answers, self.tail = [], b""

    def close(self):
        if self.file:
            self.file.close()
            self.file = None

    def write(self, data, timeout=60):
        data = bytes(data)
        self.file.write(data)
        self.file.flush()
        stream = self.tail + data
        count = 0
        i = stream.find(b"\x10\x04")
        while i != -1 and i + 2 < len(stream):
            count += 1
            i = stream.find(b"\x10\x04", i + 3)
        with self.lock:
            self.answers.extend([b"\x12"] * count)
        # keep an unfinished question (split across two writes) for next time
        if i != -1:
            self.tail = stream[i:]
        elif stream.endswith(b"\x10"):
            self.tail = b"\x10"
        else:
            self.tail = b""

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                if self.answers:
                    return self.answers.pop(0)
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.01)


def printer_maker(config):
    """A function that makes a fresh printer object, from the settings."""
    choice = config["printer"].strip()
    if choice.startswith("pretend:"):
        return lambda: PretendPrinter(choice[len("pretend:"):])
    if choice == "pyusb":
        return lambda: PyUsbPrinter(config["usb_vendor"])
    return lambda: LpPrinter(choice or "auto")


# ---------------------------------------------------------------------------------------------
# The computer end
# ---------------------------------------------------------------------------------------------
# recv(timeout) returns bytes, b"" once the computer has hung up, or None if nothing came in time.

class FdClient:
    """A Bluetooth connection, which the Pi's Bluetooth service hands over as a file descriptor."""

    def __init__(self, fd):
        self.fd = fd
        os.set_blocking(fd, True)

    def recv(self, timeout):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self.fd, 4096)

    def send(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class SocketClient:
    """A network connection (only used when tcp_port is set)."""

    def __init__(self, sock):
        self.sock = sock

    def recv(self, timeout):
        self.sock.settimeout(timeout)
        try:
            return self.sock.recv(4096)
        except socket.timeout:
            return None

    def send(self, data):
        self.sock.settimeout(10)
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------
# Passing bytes between the two
# ---------------------------------------------------------------------------------------------

class Bridge:
    def __init__(self, make_printer, idle_seconds=120, on_state=None, busy_wait=15):
        self.make_printer = make_printer
        self.idle_seconds = idle_seconds
        self.busy_wait = busy_wait                         # how long a second computer waits its turn
        self.on_state = on_state or (lambda state: None)   # hears "busy" and "ready"
        self.lock = threading.Lock()                       # one computer at a time

    def serve(self, client, who="a computer"):
        """Look after one connection from start to finish. Always closes it."""
        if not self.lock.acquire(timeout=self.busy_wait):
            log.warning("%s connected while another job was still going; turned away.", who)
            client.close()
            return
        printer = self.make_printer()
        try:
            try:
                printer.open()
            except OSError as err:
                # Hanging up straight away is how the computer learns the printer isn't there
                log.warning("%s connected, but %s.", who, err)
                return
            self.on_state("busy")
            log.info("%s connected.", who)
            total = self._pass_bytes(client, printer)
            log.info("%s finished: %d bytes to the printer.", who, total)
        except OSError as err:
            log.warning("The job from %s stopped early: %s", who, err)
        finally:
            client.close()
            printer.close()
            self.on_state("ready")
            self.lock.release()

    def _pass_bytes(self, client, printer):
        stop = threading.Event()

        def answers():   # printer -> computer
            while not stop.is_set():
                try:
                    data = printer.read(0.1)
                    if data:
                        client.send(data)
                except (OSError, ValueError):
                    return

        back = threading.Thread(target=answers, daemon=True)
        back.start()
        total = 0
        last = time.monotonic()
        try:
            while True:   # computer -> printer
                data = client.recv(1.0)
                if data is None:
                    if time.monotonic() - last > self.idle_seconds:
                        log.info("Nothing for %d seconds; hanging up.", self.idle_seconds)
                        break
                    continue
                if not data:
                    break   # the computer hung up: the job is complete
                printer.write(data)
                total += len(data)
                last = time.monotonic()
        finally:
            stop.set()
            back.join(2)
        return total


def serve_tcp(bridge, port, ready=None):
    server = socket.create_server(("", port))
    log.info("Also accepting jobs over the network on port %d.", server.getsockname()[1])
    if ready:
        ready(server)
    while True:
        try:
            conn, address = server.accept()
        except OSError:
            return   # the listener was closed
        threading.Thread(target=bridge.serve, args=(SocketClient(conn), f"network {address[0]}"),
                         daemon=True).start()


# ---------------------------------------------------------------------------------------------
# The Pi's green light
# ---------------------------------------------------------------------------------------------

class Led:
    """Shows the state on the Pi's activity light: heartbeat = ready, steady blink = pairing
    is open, solid = a job is going through. Does nothing if the light can't be controlled."""

    MODES = {"ready": "heartbeat", "pairing": "timer", "busy": "default-on"}

    def __init__(self, enabled=True):
        self.path = None
        if enabled:
            for name in ("ACT", "led0"):
                if os.path.exists(f"/sys/class/leds/{name}/trigger"):
                    self.path = f"/sys/class/leds/{name}/trigger"
                    break

    def show(self, state):
        if self.path:
            try:
                with open(self.path, "w") as trigger:
                    trigger.write(self.MODES[state])
            except (OSError, KeyError):
                pass


# ---------------------------------------------------------------------------------------------
# Bluetooth (Linux only: talks to the BlueZ service over D-Bus)
# ---------------------------------------------------------------------------------------------

def run_bluetooth(config, bridge, led):
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    name = config["name"]
    pairing = config["pairing"].strip().lower()
    minutes = max(1, int(config["pairing_minutes"]))
    allowed = {a.strip().upper().replace("-", ":") for a in config["allowed"].split(",") if a.strip()}
    state = {"pairing_open": pairing in ("window", "open"), "busy": False}

    def find_adapter():
        manager = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        for path, interfaces in manager.GetManagedObjects().items():
            if "org.bluez.Adapter1" in interfaces:
                return path
        return None

    adapter_path = None
    for _ in range(30):   # the Bluetooth chip can take a while to appear after power-on
        try:
            adapter_path = find_adapter()
        except dbus.DBusException:
            adapter_path = None
        if adapter_path:
            break
        time.sleep(2)
    if not adapter_path:
        log.error("No Bluetooth adapter found. Is Bluetooth blocked? Try: sudo rfkill unblock bluetooth")
        return 1
    adapter = dbus.Interface(bus.get_object("org.bluez", adapter_path), "org.freedesktop.DBus.Properties")

    def set_adapter(prop, value):
        try:
            adapter.Set("org.bluez.Adapter1", prop, value)
        except dbus.DBusException as err:
            log.warning("Couldn't set Bluetooth %s: %s", prop, err.get_dbus_message())

    def show_light():
        led.show("busy" if state["busy"] else "pairing" if state["pairing_open"] else "ready")

    def set_pairing(is_open):
        state["pairing_open"] = is_open
        set_adapter("DiscoverableTimeout", dbus.UInt32(0))
        set_adapter("PairableTimeout", dbus.UInt32(0))
        set_adapter("Pairable", dbus.Boolean(is_open))
        set_adapter("Discoverable", dbus.Boolean(is_open))
        log.info("Pairing is %s.", "open: new computers can pair now" if is_open else "closed")
        show_light()

    def close_pairing():
        set_pairing(False)
        return False   # don't repeat the timer

    def device_address(path):
        return path.rsplit("/dev_", 1)[-1].replace("_", ":").upper()

    class Rejected(dbus.DBusException):
        _dbus_error_name = "org.bluez.Error.Rejected"

    class Agent(dbus.service.Object):
        """Answers pairing requests. The Pi has no screen or keys, so pairing is "just works":
        allowed while pairing is open, refused otherwise."""

        def check(self, device):
            if not state["pairing_open"]:
                log.warning("Refused a pairing request from %s (pairing is closed).", device_address(device))
                raise Rejected("Pairing is closed")
            log.info("Pairing with %s.", device_address(device))

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Release(self):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            raise Rejected("This printer pairs without a PIN")

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            raise Rejected("This printer pairs without a passkey")

        @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
        def DisplayPasskey(self, device, passkey, entered):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def DisplayPinCode(self, device, pincode):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
        def RequestConfirmation(self, device, passkey):
            self.check(device)

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
        def RequestAuthorization(self, device):
            self.check(device)

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            if allowed and device_address(device) not in allowed:
                raise Rejected("Not on the allowed list")

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Cancel(self):
            pass

    class Profile(dbus.service.Object):
        """The serial port computers connect to. Each connection arrives here as a file descriptor."""

        @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
        def NewConnection(self, device, fd, properties):
            address = device_address(device)
            client = FdClient(fd.take())
            if allowed and address not in allowed:
                log.warning("%s isn't on the allowed list; turned away.", address)
                client.close()
                return
            threading.Thread(target=bridge.serve, args=(client, address), daemon=True).start()

        @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
        def RequestDisconnection(self, device):
            pass

        @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
        def Release(self):
            pass

    def on_bridge_state(new):   # called from a job's thread, so hand over to the main loop
        def apply():
            state["busy"] = new == "busy"
            show_light()
            return False
        GLib.idle_add(apply)

    bridge.on_state = on_bridge_state

    set_adapter("Powered", dbus.Boolean(True))
    set_adapter("Alias", dbus.String(name))

    agent_path, profile_path = "/obelix/agent", "/obelix/serial"
    Agent(bus, agent_path)
    Profile(bus, profile_path)
    bluez = bus.get_object("org.bluez", "/org/bluez")
    agents = dbus.Interface(bluez, "org.bluez.AgentManager1")
    agents.RegisterAgent(agent_path, "NoInputNoOutput")
    agents.RequestDefaultAgent(agent_path)
    dbus.Interface(bluez, "org.bluez.ProfileManager1").RegisterProfile(profile_path, SERIAL_PORT_UUID, {
        "Name": dbus.String(name),
        "Role": dbus.String("server"),
        "Channel": dbus.UInt16(int(config["channel"])),
        "RequireAuthentication": dbus.Boolean(True),    # only paired computers
        "RequireAuthorization": dbus.Boolean(False),
        "AutoConnect": dbus.Boolean(False),
    })

    def on_device_change(interface, changed, invalidated, path=None):
        if interface == "org.bluez.Device1" and "Paired" in changed:
            log.info("%s is %s.", device_address(path), "now paired" if changed["Paired"] else "no longer paired")

    bus.add_signal_receiver(on_device_change, dbus_interface="org.freedesktop.DBus.Properties",
                            signal_name="PropertiesChanged", bus_name="org.bluez", path_keyword="path")

    set_pairing(pairing in ("window", "open"))
    if pairing == "window":
        log.info("Pairing closes in %d minutes.", minutes)
        GLib.timeout_add_seconds(minutes * 60, close_pairing)

    log.info('Ready as "%s" (Bluetooth serial channel %s).', name, config["channel"])
    GLib.MainLoop().run()
    return 0


# ---------------------------------------------------------------------------------------------

def load_config(path=None):
    parser = configparser.ConfigParser()
    parser["bridge"] = DEFAULTS
    for candidate in ([path] if path else CONFIG_PATHS):
        if candidate and os.path.exists(candidate):
            parser.read(candidate, encoding="utf-8")
            log.info("Settings from %s", candidate)
            break
    return parser["bridge"]


def main(argv=None):
    args = argparse.ArgumentParser(description="Bluetooth-to-USB bridge for a receipt printer")
    args.add_argument("--config", help="settings file (default: the first found of the usual places)")
    args.add_argument("--tcp", type=int, help="also accept jobs on this network port (e.g. 9100)")
    args.add_argument("--printer", help='override the printer setting, e.g. "pretend:/tmp/capture.bin"')
    args.add_argument("--no-bluetooth", action="store_true", help="network only (needs --tcp)")
    args.add_argument("-v", "--verbose", action="store_true")
    opts = args.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if opts.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    config = load_config(opts.config)
    if opts.printer:
        config["printer"] = opts.printer
    led = Led(config.getboolean("led", fallback=True))
    bridge = Bridge(printer_maker(config), idle_seconds=int(config["idle_seconds"]),
                    on_state=led.show)
    port = opts.tcp if opts.tcp is not None else int(config["tcp_port"])
    if port:
        threading.Thread(target=serve_tcp, args=(bridge, port), daemon=True).start()
    if opts.no_bluetooth:
        if not port:
            args.error("--no-bluetooth needs --tcp PORT")
        led.show("ready")
        threading.Event().wait()   # the network thread does the work
        return 0
    return run_bluetooth(config, bridge, led)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
