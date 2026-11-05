#!/usr/bin/env bash
# Installs or updates Aziz on this machine, as root. Run by deploy/deploy.sh. Safe to re-run.
set -euo pipefail
root=/opt/aziz
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

id aziz >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aziz
chown -R root:root "$root"
chmod -R go-w "$root"

# Aziz used to run inside the study agent as ai-ders-arbiter. Take over from it, keeping the chosen GPU mode.
if [[ -f /etc/systemd/system/ai-ders-arbiter.service ]]; then
  systemctl disable --now ai-ders-arbiter.service
  rm -f /etc/systemd/system/ai-ders-arbiter.service /etc/polkit-1/rules.d/50-ai-ders.rules
  if [[ -f /var/lib/ai-ders/gpu-mode ]]; then
    install -d -o aziz -g aziz -m 700 /var/lib/aziz
    install -o aziz -g aziz -m 600 /var/lib/ai-ders/gpu-mode /var/lib/aziz/gpu-mode
  fi
  rm -f /var/lib/ai-ders/jobs.json /var/lib/ai-ders/jobs.new
fi

install -m 644 "$root/deploy/aziz.service" /etc/systemd/system/aziz.service
install -m 644 "$root/deploy/50-aziz.rules" /etc/polkit-1/rules.d/50-aziz.rules
command -v restorecon >/dev/null && restorecon -R "$root" /etc/systemd/system/aziz.service /etc/polkit-1/rules.d/50-aziz.rules
systemctl daemon-reload
systemctl enable aziz.service
systemctl restart aziz.service
echo "==> Aziz running: $(systemctl is-active aziz.service)"
