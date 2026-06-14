# maws

## systemd:

Usługi działają na zasadzie daemonów uruchamianych przez `systemd`:

1. /home/test/meteo.py - pobiera dane ze stacji poprzez TTY i wysyła na serwer Adama

2. /home/test/maws_watcher.py - parsuje dane z pliku meteo_data.txt i wysyła do postgresql (mrozowiska.pl)


### tworzenie HTMLa

python3 maws_dashboard_gen.py     --pg 'dbname=meteo user=postgres host=mrozowiska.pl sslmode=require password=...'     --out meteo.html

wpis do crona:

```
* * * * * cd /home/test/ ; curl -v --insecure --ftp-ssl --ftp-ssl-reqd -T meteo.html ftp://ftp.web.amu.edu.pl/klimat/meteo.html --user "klimat:.."
```

### usuwanie duplikatów z postgresa:

Bez dodania `--apply` tylko pokaże liczbę duplikatów, zatem:
```
python3 remove_duplicates.py --pg 'dbname=meteo user=postgres host=mrozowiska.pl password=!!!!' --apply 
```

