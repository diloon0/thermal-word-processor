"""Try the Obelix bridge from the laptop, step by step. Prints NOTHING unless you add --print.

    python try_bridge.py --tcp 192.168.1.50        ask the printer's status over the network
    python try_bridge.py --bluetooth               the same over Bluetooth (finds a paired "Obelix")
    python try_bridge.py --bluetooth AA:BB:CC:DD:EE:FF
    python try_bridge.py --bluetooth --print       ...and print a three-line test slip

The status check only asks the printer three questions (paper, cover, errors). No paper is used.
"""

import argparse
import socket
import sys
import time

DLE_EOT = b"\x10\x04"
QUESTIONS = {2: "cover and feed button", 3: "errors", 4: "paper"}
TEST_SLIP = (b"\x1b@" + b"\x1ba\x01" + b"Obelix bridge test\n" + b"\x1ba\x00"
             + b"Sent over the bridge.\n" + time.strftime("%Y-%m-%d %H:%M").encode() + b"\n"
             + b"\n\n" + b"\x1dVB\x00")


def explain(n, value):
    if value is None:
        return "no answer"
    problems = []
    if n == 2 and value & 0x04:
        problems.append("cover is open")
    if n == 3 and value & 0x08:
        problems.append("cutter error")
    if n == 3 and value & 0x60:
        problems.append("printer error")
    if n == 4 and value & 0x60:
        problems.append("out of paper")
    elif n == 4 and value & 0x0C:
        problems.append("paper is low")
    return f"0x{value:02x} " + ("(" + ", ".join(problems) + ")" if problems else "(fine)")


def connect(opts):
    if opts.tcp:
        print(f"Connecting to {opts.tcp} port {opts.port} over the network...")
        return socket.create_connection((opts.tcp, opts.port), timeout=10)
    address = opts.bluetooth
    if address == "find":
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        import thermal_word_processor as twp
        address = twp.find_bluetooth_address(opts.name)
        if not address:
            sys.exit(f'No paired Bluetooth device called "{opts.name}". Pair it in '
                     "Settings > Bluetooth & devices > Add device first.")
    print(f"Connecting to {address} over Bluetooth (channel {opts.channel})...")
    conn = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    conn.settimeout(15)
    conn.connect((address, opts.channel))
    return conn


def main():
    args = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    how = args.add_mutually_exclusive_group(required=True)
    how.add_argument("--tcp", metavar="ADDRESS", help="the Pi's network address (bridge needs tcp_port = 9100)")
    how.add_argument("--bluetooth", nargs="?", const="find", metavar="ADDRESS",
                     help="the Pi's Bluetooth address; leave it out to look up the paired device by name")
    args.add_argument("--port", type=int, default=9100)
    args.add_argument("--channel", type=int, default=1)
    args.add_argument("--name", default="Obelix")
    args.add_argument("--print", action="store_true", dest="print_slip", help="also print a short test slip")
    opts = args.parse_args()

    try:
        conn = connect(opts)
    except OSError as err:
        sys.exit(f"Couldn't connect: {err}")
    with conn:
        print("Connected. Asking the printer how it is...")
        answered = 0
        for n, what in QUESTIONS.items():
            conn.settimeout(3)
            started = time.monotonic()
            try:
                conn.sendall(DLE_EOT + bytes([n]))
                reply = conn.recv(1)
            except socket.timeout:
                reply = None
            except OSError as err:
                sys.exit(f"The bridge hung up ({err}). Is the printer switched on and plugged into the Pi?")
            if reply == b"":
                sys.exit("The bridge hung up. Is the printer switched on and plugged into the Pi?")
            value = reply[0] if reply else None
            answered += value is not None
            print(f"  {what:22} {explain(n, value)}   [{time.monotonic() - started:.2f}s]")
        if not answered:
            print("The bridge is reachable, but the printer didn't answer. Printing may still work;\n"
                  'if it does, try "printer = pyusb" in the bridge settings to get status answers.')
        if opts.print_slip:
            print("Printing the test slip...")
            conn.settimeout(30)
            conn.sendall(TEST_SLIP)
            conn.sendall(DLE_EOT + b"\x04")   # an answer means everything before it arrived
            try:
                conn.settimeout(15)
                print("  The printer confirmed it." if conn.recv(1) else "  The bridge hung up early.")
            except socket.timeout:
                print("  Sent, but the printer didn't confirm.")
        else:
            print("Nothing was printed (add --print for a test slip).")


if __name__ == "__main__":
    main()
