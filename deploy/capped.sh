#!/usr/bin/env bash
# Runs a test on the server without being able to hurt it: half a core and 1 GB at most. Everything else there
# comes first. An uncapped simulation once took the machine's last 7 GB and stopped the model.
exec systemd-run --user --scope --quiet -p MemoryMax=1G -p MemorySwapMax=0 -p CPUQuota=50% -p CPUWeight=10 nice -n 19 "$@"
