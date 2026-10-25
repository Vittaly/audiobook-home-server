#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing files"
install -m 0755 "$SRC/usb-mount.sh"     /usr/local/bin/usb-mount.sh
install -m 0644 "$SRC/usb-mount@.service" /etc/systemd/system/usb-mount@.service
install -m 0644 "$SRC/99-automount.rules" /etc/udev/rules.d/99-automount.rules
ls 
echo "==> Checking optional dependencies"
for pkg in icu-devtools exfatprogs ntfs-3g; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        echo "    WARN: $pkg not installed (optional)"
    fi
done

echo "==> Reloading systemd and udev"
systemctl daemon-reload
udevadm control --reload-rules
udevadm trigger

echo
echo "OK. Check with:"
echo "  systemctl status 'usb-mount@*'"
echo "  findmnt -t vfat,exfat,ntfs,ntfs3,ext4 /media"