# maws

## systemd:

Usługi działają na zasadzie daemonów uruchamianych przez `systemd`:

1. /home/test/meteo.py - pobiera dane ze stacji poprzez TTY

2. /home/test/maws_watcher.py - parsuje dane z pliku meteo_data.txt i wysyła do postgresql (mrozowiska.pl)


### tworzenie HTMLa

python3 maws_dashboard_gen.py     --pg 'dbname=meteo user=postgres host=mrozowiska.pl sslmode=require password=...'     --out meteo.html

wpis do crona:

```
* * * * * cd /home/test/ ; curl -v --insecure --ftp-ssl --ftp-ssl-reqd -T meteo.html ftp://ftp.web.amu.edu.pl/klimat/meteo/index.html --user "klimat:.."
```

### usuwanie duplikatów z postgresa:

Bez dodania `--apply` tylko pokaże liczbę duplikatów, zatem:
```
python3 remove_duplicates.py --pg 'dbname=meteo user=postgres host=mrozowiska.pl password=!!!!' --apply 
```


### problem ze zrywaniem polaczenia:

wylacz oszczedzenie energii:

```
sudo nmcli connection modify MAWS_link connection.autoconnect yes
sudo nmcli connection modify MAWS_link connection.autoconnect-retries 0   # 0 = próbuj w nieskończoność
sudo nmcli connection modify MAWS_link 802-11-wireless.powersave 2        # 2 = wyłącz oszczędzanie energii
sudo nmcli connection up MAWS_link
```

oraz ustaw watchdoga `/usr/local/bin/wifi-watchdog.sh`:

```
#!/bin/bash
SSID="MAWS_link"
GW=$(ip route | awk '/default/ {print $3; exit}')
if [ -z "$GW" ] || ! ping -c 3 -W 2 "$GW" >/dev/null 2>&1; then
    logger "wifi-watchdog: brak laczności, podnoszę $SSID"
    nmcli connection up "$SSID"
fi
```

Nadaj prawa: sudo chmod +x /usr/local/bin/wifi-watchdog.sh
Usługa — /etc/systemd/system/wifi-watchdog.service:


```
[Unit]
Description=WiFi watchdog
After=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/wifi-watchdog.sh

```

Timer - /etc/systemd/system/wifi-watchdog.timer:
```
[Unit]
Description=Uruchamiaj WiFi watchdog cyklicznie

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
```

Włącz:
```
sudo systemctl daemon-reload
sudo systemctl enable --now wifi-watchdog.timer
```

Zaletą tego watchdoga jest to, że używa zapisanego profilu (nmcli connection up), więc hasło nigdzie w skrypcie nie ląduje — zostaje wyłącznie w profilu NetworkManagera.