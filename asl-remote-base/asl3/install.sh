#!/bin/sh
# Install tkx90-catd on an AllStarLink 3 node (Debian 12, run as root).
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
install -d /usr/local/lib/tkx90 /etc/tkx90
install -m 0755 "$HERE/host/tkx90_catd.py" /usr/local/lib/tkx90/tkx90_catd.py
install -m 0755 "$HERE/host/vch.py" /usr/local/lib/tkx90/vch.py
install -m 0644 "$HERE/host/vch_ui.html" /usr/local/lib/tkx90/vch_ui.html
[ -f /etc/tkx90/tkx90.toml ] || install -m 0644 "$HERE/host/tkx90.toml" /etc/tkx90/tkx90.toml
install -m 0644 "$HERE/asl3/99-tkx90.rules" /etc/udev/rules.d/99-tkx90.rules
install -m 0644 "$HERE/asl3/tkx90-catd.service" /etc/systemd/system/tkx90-catd.service
udevadm control --reload-rules && udevadm trigger
systemctl daemon-reload
systemctl enable --now tkx90-catd
echo "Edit /etc/tkx90/tkx90.toml, then: systemctl restart tkx90-catd asterisk"
