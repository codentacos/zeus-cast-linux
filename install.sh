#!/usr/bin/env bash
# Installs zeuscast-linux for the current user on Fedora.
set -euo pipefail
cd "$(dirname "$0")"

venv="$HOME/.local/share/zeuscast/venv"
bin="$HOME/.local/bin/zeuscast"

echo "==> Installing dependencies (sudo)"
sudo dnf install -y python3 python3-pip python3-pyserial python3-pillow python3-psutil python3-pyside6 fontconfig glib2
if ! command -v ffmpeg >/dev/null; then
    # ffmpeg-free is enough; RPM Fusion's ffmpeg also works (and has libx264).
    sudo dnf install -y ffmpeg-free
fi

echo "==> Installing udev rule (serial port access, keep ModemManager away)"
sudo install -m 0644 packaging/60-zeuscast.rules /etc/udev/rules.d/60-zeuscast.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=tty

echo "==> Installing zeuscast into $venv"
python3 -m venv --system-site-packages "$venv"
"$venv/bin/pip" install --quiet --upgrade .
mkdir -p "$(dirname "$bin")" "$HOME/.local/share/applications" "$HOME/.config/systemd/user"
ln -sf "$venv/bin/zeuscast" "$bin"
sed "s|@BIN@|$bin|" packaging/zeuscast.desktop.in > "$HOME/.local/share/applications/zeuscast.desktop"
install -m 0644 packaging/zeuscast.service "$HOME/.config/systemd/user/zeuscast.service"
install -m 0644 README.md "$HOME/.local/share/zeuscast/README.md"
systemctl --user daemon-reload

cat <<EOF

Done. Unplug and replug the cooler's USB cable (or reboot) so the udev rule applies, then:

  zeuscast list          # should show the cooler
  zeuscast gui           # settings window (also in your app menu)

Or run it headless in the background:

  systemctl --user enable --now zeuscast
EOF
