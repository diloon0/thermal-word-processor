#!/bin/bash
# Sets up the Obelix bridge on a Raspberry Pi running Raspberry Pi OS Lite.
# Run it on the Pi, from the folder holding these files:   sudo bash install.sh
# Safe to run again after changing the files (it keeps your settings).
#
# Other uses:
#   sudo bash install.sh lock     make the SD card read-only, so pulling the plug can't corrupt it
#   sudo bash install.sh unlock   make it writable again (needed before pairing a new computer
#                                 or changing anything that should survive a restart)
#   sudo bash install.sh remove   stop and remove the bridge

set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run this with sudo:  sudo bash install.sh"
    exit 1
fi

if [ -d /boot/firmware ]; then CONF=/boot/firmware/obelix-bridge.conf; else CONF=/boot/obelix-bridge.conf; fi

case "${1:-install}" in
lock)
    # Pairings are stored on the card, so pair your computer BEFORE locking.
    raspi-config nonint do_overlayfs 0
    echo "The card will be read-only after the next restart:  sudo reboot"
    echo "Changes made after that are forgotten on restart (including new pairings)."
    exit 0
    ;;
unlock)
    raspi-config nonint do_overlayfs 1
    echo "The card will be writable after the next restart:  sudo reboot"
    exit 0
    ;;
remove)
    systemctl disable --now obelix-bridge 2>/dev/null || true
    rm -f /etc/systemd/system/obelix-bridge.service /usr/local/bin/obelix-bridge
    systemctl daemon-reload
    echo "Removed. Your settings are still in $CONF."
    exit 0
    ;;
install)
    ;;
*)
    echo "Unknown option: $1   (use: install, lock, unlock or remove)"
    exit 1
    ;;
esac

echo "== Installing what the bridge needs =="
apt-get update
apt-get install -y bluez python3-dbus python3-gi

echo "== Installing the bridge =="
install -m 755 obelix_bridge.py /usr/local/bin/obelix-bridge
install -m 644 obelix-bridge.service /etc/systemd/system/obelix-bridge.service
if [ -f "$CONF" ]; then
    echo "Keeping your existing settings in $CONF"
else
    install -m 644 bridge.conf "$CONF"
    echo "Settings are in $CONF"
fi

echo "== Switching Bluetooth on =="
rfkill unblock bluetooth || true
# Start Bluetooth powered on after every boot
if [ -f /etc/bluetooth/main.conf ] && ! grep -q "^AutoEnable=true" /etc/bluetooth/main.conf; then
    if grep -q "^\[Policy\]" /etc/bluetooth/main.conf; then
        sed -i '/^\[Policy\]/a AutoEnable=true' /etc/bluetooth/main.conf
    else
        printf '\n[Policy]\nAutoEnable=true\n' >> /etc/bluetooth/main.conf
    fi
fi
systemctl enable bluetooth
systemctl restart bluetooth

echo "== Starting the bridge =="
systemctl daemon-reload
systemctl enable obelix-bridge
systemctl restart obelix-bridge
sleep 3
systemctl --no-pager --lines=8 status obelix-bridge || true

echo
echo "Done. Watch it work with:   journalctl -u obelix-bridge -f"
echo "New computers can pair for the first few minutes after it starts."
echo "To open pairing again:      sudo systemctl restart obelix-bridge"
