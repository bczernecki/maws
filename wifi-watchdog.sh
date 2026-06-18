#!/bin/bash
SSID="MAWS_link"
GW=$(ip route | awk '/default/ {print $3; exit}')
if [ -z "$GW" ] || ! ping -c 3 -W 2 "$GW" >/dev/null 2>&1; then
    logger "wifi-watchdog: brak laczności, podnoszę $SSID"
    nmcli connection up "$SSID"
fi
