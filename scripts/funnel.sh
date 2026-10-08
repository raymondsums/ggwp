#!/bin/sh
# Open or close the public link for game day.
#
#   scripts/funnel.sh on     friends can reach the game at the printed URL
#   scripts/funnel.sh off    back to private
#
# Only this game's port goes public. LifeOS keeps 443 and stays on the tailnet.
# Funnel allows public ports 443, 8443 and 10000; 8443 is free here.
PORT=${PARKRACE_PORT:-5057}
case "$1" in
  on)  tailscale funnel --bg --https=8443 "$PORT" && tailscale funnel status ;;
  off) tailscale funnel --https=8443 off && echo "Public link closed" ;;
  *)   echo "usage: $0 on|off"; exit 1 ;;
esac
