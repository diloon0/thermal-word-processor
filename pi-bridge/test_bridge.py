"""Tests for the bridge's byte-passing, run on any computer:  python test_bridge.py

Nothing here touches a real printer or Bluetooth. A pretend printer stands in for Obelix, and a
network connection on this computer stands in for the Bluetooth link.
"""

import os
import socket
import tempfile
import threading
import time
import unittest

import obelix_bridge as ob


def start(bridge):
    """Run the bridge's network listener on a free port. Returns (port, server)."""
    box = {}
    ready = threading.Event()

    def got(server):
        box["server"] = server
        ready.set()

    threading.Thread(target=ob.serve_tcp, args=(bridge, 0, got), daemon=True).start()
    ready.wait(5)
    return box["server"].getsockname()[1], box["server"]


def ask(conn, n):
    conn.sendall(b"\x10\x04" + bytes([n]))
    return conn.recv(1)


def hung_up(conn):
    """True if the other end closes the connection within a few seconds."""
    conn.settimeout(5)
    try:
        return conn.recv(1) == b""
    except socket.timeout:
        return False
    except OSError:
        return True


class BrokenPrinter:
    """A printer that fails part-way: `good` bytes go through, then it's 'unplugged'."""

    def __init__(self, good):
        self.good = good

    def open(self):
        pass

    def close(self):
        pass

    def write(self, data, timeout=60):
        self.good -= len(data)
        if self.good < 0:
            raise OSError("unplugged")

    def read(self, timeout):
        time.sleep(timeout)
        return b""


class MissingPrinter(BrokenPrinter):
    def open(self):
        raise ob.PrinterGone("no USB printer is plugged in and switched on")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        handle, self.capture = tempfile.mkstemp(suffix=".bin")
        os.close(handle)
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        time.sleep(0.2)
        try:
            os.remove(self.capture)
        except OSError:
            pass

    def bridge(self, make_printer=None, **kw):
        bridge = ob.Bridge(make_printer or (lambda: ob.PretendPrinter(self.capture)), **kw)
        port, server = start(bridge)
        self.servers.append(server)
        return bridge, port

    def captured(self):
        with open(self.capture, "rb") as f:
            return f.read()

    def test_status_then_job_then_confirmation(self):
        states = []
        bridge, port = self.bridge(on_state=states.append)
        job = b"\x1b@Hello Obelix\n" + bytes(range(256)) * 200   # about 51 KB, like a small picture
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            self.assertEqual([ask(conn, n) for n in (2, 3, 4)], [b"\x12"] * 3)
            conn.sendall(job)
            self.assertEqual(ask(conn, 4), b"\x12")   # the confirmation the word processor waits for
        time.sleep(0.5)
        sent = b"\x10\x04\x02\x10\x04\x03\x10\x04\x04" + job + b"\x10\x04\x04"
        self.assertEqual(self.captured(), sent)
        self.assertEqual(states, ["busy", "ready"])

    def test_hangs_up_when_the_printer_is_missing(self):
        bridge, port = self.bridge(lambda: MissingPrinter(0))
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            self.assertTrue(hung_up(conn))

    def test_hangs_up_when_the_printer_fails_mid_job(self):
        bridge, port = self.bridge(lambda: BrokenPrinter(1000))
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            try:
                for _ in range(50):
                    conn.sendall(b"x" * 500)
                    time.sleep(0.01)
            except OSError:
                return   # the send itself noticed the hang-up
            self.assertTrue(hung_up(conn))

    def test_hangs_up_on_a_silent_computer(self):
        bridge, port = self.bridge(idle_seconds=1)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            started = time.monotonic()
            self.assertTrue(hung_up(conn))
            self.assertLess(time.monotonic() - started, 4.5)

    def test_second_computer_waits_its_turn(self):
        bridge, port = self.bridge(busy_wait=5)
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.assertEqual(ask(first, 4), b"\x12")
        second = socket.create_connection(("127.0.0.1", port), timeout=5)
        second.sendall(b"\x10\x04\x04")
        second.settimeout(0.7)
        with self.assertRaises(socket.timeout):   # no answer while the first is still connected
            second.recv(1)
        first.close()
        second.settimeout(5)
        self.assertEqual(second.recv(1), b"\x12")
        second.close()

    def test_second_computer_is_turned_away_if_the_wait_is_too_long(self):
        bridge, port = self.bridge(busy_wait=0.5)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as first:
            self.assertEqual(ask(first, 4), b"\x12")
            with socket.create_connection(("127.0.0.1", port), timeout=5) as second:
                self.assertTrue(hung_up(second))

    def test_bridge_recovers_for_the_next_job(self):
        bridge, port = self.bridge()
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                self.assertEqual(ask(conn, 2), b"\x12")
            time.sleep(0.3)


class PretendPrinterTests(unittest.TestCase):
    def setUp(self):
        handle, self.capture = tempfile.mkstemp(suffix=".bin")
        os.close(handle)
        self.printer = ob.PretendPrinter(self.capture)
        self.printer.open()

    def tearDown(self):
        self.printer.close()
        os.remove(self.capture)

    def answers(self):
        got = b""
        while True:
            more = self.printer.read(0.05)
            if not more:
                return got
            got += more

    def test_one_answer_per_question(self):
        self.printer.write(b"abc\x10\x04\x02def\x10\x04\x04")
        self.assertEqual(self.answers(), b"\x12\x12")

    def test_question_split_across_writes(self):
        for piece in (b"\x10", b"\x04", b"\x04"):
            self.printer.write(piece)
        self.assertEqual(self.answers(), b"\x12")
        self.printer.write(b"zz\x10\x04")
        self.assertEqual(self.answers(), b"")
        self.printer.write(b"\x03")
        self.assertEqual(self.answers(), b"\x12")

    def test_plain_text_gets_no_answer(self):
        self.printer.write(b"Just some text\n\x10 not a question \x04")
        self.assertEqual(self.answers(), b"")


class ConfigTests(unittest.TestCase):
    def test_defaults_and_file(self):
        self.assertEqual(ob.load_config("does-not-exist.conf")["name"], "Obelix")
        here = os.path.dirname(os.path.abspath(__file__))
        config = ob.load_config(os.path.join(here, "bridge.conf"))
        self.assertEqual(config["pairing"], "window")
        self.assertEqual(int(config["channel"]), 1)
        self.assertEqual(int(config["tcp_port"]), 0)
        self.assertIsInstance(ob.printer_maker(config)(), ob.LpPrinter)
        config["printer"] = "pretend:x.bin"
        self.assertIsInstance(ob.printer_maker(config)(), ob.PretendPrinter)


if __name__ == "__main__":
    unittest.main(verbosity=2)
