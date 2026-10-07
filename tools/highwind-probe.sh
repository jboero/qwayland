#!/usr/bin/env bash
# Advertise a fake Horizon OS "Highwind" Virtual Display server on the LAN and
# log whatever the headset sends. Research aid for docs/native-virtual-display.md;
# only useful on a headset where Meta has enabled the DISCO wireless transport.
#
# Usage: tools/highwind-probe.sh [port]   (Ctrl-C to stop)
# Watch the headset at the same time with:
#   adb logcat | grep -iE "highwind|crosswind|ServiceDiscovery"
set -euo pipefail
PORT=${1:-47800}
IP=$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
UUID=$(cat /proc/sys/kernel/random/uuid)
LOG=highwind-probe-$(date +%Y%m%d-%H%M%S).log

trap 'kill 0' EXIT
avahi-publish-service "$(hostname)-linux" _highwind_sp_v1._tcp "$PORT" \
    tt=tcp etn=mac etv=1 ein="$(hostname)" eiu="$UUID" ipo="$IP" &
echo "advertising _highwind_sp_v1._tcp on $IP:$PORT, hexdump of traffic in $LOG"
socat -d -d -x TCP-LISTEN:"$PORT",reuseaddr,fork SYSTEM:'cat >/dev/null' 2>&1 | tee "$LOG"
