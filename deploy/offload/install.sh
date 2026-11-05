#!/usr/bin/env bash
# Installs the job side of offload, as root. Safe to re-run. It gives nobody a way in: connect.sh does that.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

# A real shell is needed for sshd to run the forced command. It offers nothing more: `restrict` and the gate see to that.
id offload >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/offload --shell /bin/bash offload
passwd -l offload >/dev/null 2>&1 || true

install -m 755 "$here/offload_gate.py" /usr/local/libexec/offload-gate
install -m 755 "$here/offload-run" /usr/local/libexec/offload-run

install -d -o offload -g offload -m 750 /var/lib/offload
install -d -o offload -g offload -m 750 /var/lib/offload/jobs /var/lib/offload/queue /var/lib/offload/home
install -d -m 755 /opt/offload-tools /opt/offload-tools/android-sdk /opt/offload-tools/jbr /opt/offload-tools/bin
touch /opt/offload-tools/bin/uv

no_devices='PrivateDevices=yes
DevicePolicy=closed'
nvidia='DevicePolicy=closed
DeviceAllow=/dev/nvidia0 rw
DeviceAllow=/dev/nvidiactl rw
DeviceAllow=/dev/nvidia-uvm rw
DeviceAllow=/dev/nvidia-uvm-tools rw
DeviceAllow=/dev/null rw
DeviceAllow=/dev/zero rw
DeviceAllow=/dev/full rw
DeviceAllow=/dev/random r
DeviceAllow=/dev/urandom r
DeviceAllow=/dev/tty rw'
# What only this machine knows, both optional: OFFLOAD_EXTRA_HIDDEN, more paths to keep from jobs, and OFFLOAD_TOOL_BINDS,
# tools to lend them, as `-/real/path:/opt/offload-tools/name` entries.
OFFLOAD_EXTRA_HIDDEN='' OFFLOAD_TOOL_BINDS=''
# shellcheck disable=SC1091
[[ -f /etc/aziz/offload.env ]] && . /etc/aziz/offload.env
export OFFLOAD_EXTRA_HIDDEN OFFLOAD_TOOL_BINDS
render() { python3 - "$here/job.service.in" "$1" "$2" <<'PY'
import os
import sys
template, kind, devices = sys.argv[1:]
binds = os.environ["OFFLOAD_TOOL_BINDS"]
text = open(template).read().replace("@KIND@", kind).replace("@DEVICES@", devices)
text = text.replace("@EXTRA_HIDDEN@", os.environ["OFFLOAD_EXTRA_HIDDEN"])
# A line of its own, and none at all without tools: an empty assignment would clear the binds before it.
text = text.replace("@TOOL_BINDS@", f"BindReadOnlyPaths={binds}" if binds else "")
print(text, end="")
PY
}
render CPU "$no_devices" >/etc/systemd/system/offload-job@.service
render GPU "$nvidia" >/etc/systemd/system/offload-gpu-job@.service
chmod 644 /etc/systemd/system/offload-job@.service /etc/systemd/system/offload-gpu-job@.service

command -v restorecon >/dev/null && restorecon -R /home/offload /usr/local/libexec/offload-gate /usr/local/libexec/offload-run /etc/systemd/system/offload-job@.service /etc/systemd/system/offload-gpu-job@.service /var/lib/offload /opt/offload-tools
systemctl daemon-reload
echo "==> offload job side installed"
