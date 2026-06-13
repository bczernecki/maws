# maws

## systemd:

Usługi działają na zasadzie daemonów uruchamianych przez `systemd`:

1. /home/test/meteo.py - pobiera dane ze stacji poprzez TTY i wysyła na serwer Adama

2. /home/test/maws_watcher.py - parsuje dane z pliku meteo_data.txt i wysyła do postgresql (mrozowiska.pl)


### usuwanie duplikatów z postgresa:


Bez dodania `--apply` tylko pokaże liczbę duplikatów, zatem:
```
python3 remove_duplicates.py --pg 'dbname=meteo user=postgres host=mrozowiska.pl password=!!!!' --apply 
```