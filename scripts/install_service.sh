#!/usr/bin/env bash
set -euo pipefail

repo=/home/aonpi/RaspberryPi5VEX
script_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ "$script_repo" != "$repo" ]]; then
    echo "Install from $repo; the service uses that fixed checkout path." >&2
    exit 1
fi
if [[ ! -x "$repo/build/vexpi" ]]; then
    echo "Build $repo/build/vexpi before installing the service." >&2
    exit 1
fi
systemd-analyze verify "$repo/deploy/vexp.service"
sudo install -m 0644 "$repo/deploy/vexp.service" /etc/systemd/system/vexp.service
sudo systemctl daemon-reload
sudo systemctl enable vexp.service
sudo systemctl restart vexp.service
systemctl --no-pager --full status vexp.service
echo 'Follow logs: journalctl -u vexp.service -f'
