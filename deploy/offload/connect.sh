#!/usr/bin/env bash
# Run this yourself, once, on your Mac: deploy/offload/connect.sh
# It lets this Mac reach offload. It makes a dedicated SSH key here, and on the server it adds that key and adds
# `offload` from this Mac's addresses to the SSH rules. All the key can do is run offload-gate.
#
# The SSH change undoes itself after 10 minutes unless a fresh login works first, so a mistake cannot lock you out.
set -euo pipefail
here=$(cd "$(dirname "$0")/../.." && pwd)
host=$(cat "$here/deploy/.host")
key="$HOME/.ssh/offload_ed25519"

# No passphrase: it has to work unattended. It only works from this Mac's addresses, and only runs the gate.
[[ -f $key ]] || ssh-keygen -q -t ed25519 -N "" -C offload -f "$key"
"$here/client/install.sh" >/dev/null
public=$(cat "$key.pub")
[[ $public == ssh-ed25519\ * ]] || { echo "unexpected key" >&2; exit 1; }

echo "==> server: adding the key and the SSH rule, with an automatic undo armed"
ssh -t "$host" sudo bash -s -- "'$public'" "'${host%@*}'" <<'SERVER'
set -euo pipefail
public=$1
admin=$2
conf=/etc/ssh/sshd_config.d/10-only-mac.conf
addresses=$(sed -nE 's/^AllowUsers //p' "$conf" | tr ' ' '\n' | sed -nE "s/^$admin@//p")
[[ -n $addresses ]] || { echo "no addresses in $conf" >&2; exit 1; }
install -d -o offload -g offload -m 700 /home/offload/.ssh
printf 'restrict,from="%s",command="/usr/local/libexec/offload-gate" %s\n' "$(paste -sd, - <<<"$addresses")" "$public" >/home/offload/.ssh/authorized_keys
chown offload:offload /home/offload/.ssh/authorized_keys
chmod 600 /home/offload/.ssh/authorized_keys
restorecon -R /home/offload
if ! grep -q 'offload@' "$conf"; then
  cp -a "$conf" /etc/ssh/10-only-mac.conf.before-offload
  systemd-run --quiet --on-active=10min --unit=ssh-allow-revert /bin/sh -c "cp -a /etc/ssh/10-only-mac.conf.before-offload $conf; systemctl reload sshd"
  extra=$(sed 's/^/offload@/' <<<"$addresses" | paste -sd' ' -)
  sed -i -E "s/^(AllowUsers .*)$/\\1 $extra/" "$conf"
  restorecon "$conf"
  sshd -t
  systemctl reload sshd
fi
SERVER

echo "==> proving it from a fresh connection"
ssh -o ConnectTimeout=10 "$host" true || { echo "your login FAILED. Leave it: the change undoes itself within 10 minutes." >&2; exit 1; }
echo "  your own login still works"
target="offload@${host#*@}"
try() { ssh -i "$key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 "$target" "$@" </dev/null; }
try state >/dev/null || { echo "offload login FAILED. Leave it: the change undoes itself within 10 minutes." >&2; exit 1; }
echo "  offload works"
if try 'cat /etc/passwd' >/dev/null 2>&1; then echo "  PROBLEM: the key ran a command it should not" >&2; exit 1; fi
if try 'bash -c id' >/dev/null 2>&1; then echo "  PROBLEM: the key got a shell" >&2; exit 1; fi
echo "  the key can run nothing else"

ssh -t "$host" 'sudo systemctl stop ssh-allow-revert.timer 2>/dev/null; sudo rm -f /etc/ssh/10-only-mac.conf.before-offload; true'
echo "==> done. The automatic undo is cancelled and offload is connected."
