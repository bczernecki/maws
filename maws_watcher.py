#!/usr/bin/env python3
"""
Demon sledzacy rosnacy plik logu Vaisala MAWS (tryb append)
i wysylajacy rekordy na biezaco do PostgreSQL - do dwoch tabel:
  maws_wind (ts, ws, wd)
  maws_ptu  (ts, ta..., rh..., td..., pa..., sr..., rain...)

Cechy:
- zapamietuje pozycje w pliku (plik .offset), wiec po restarcie nie czyta od zera
- wykrywa rotacje/obciecie pliku (gdy plik zmaleje - zaczyna od poczatku)
- ON CONFLICT DO NOTHING => odporny na duplikaty
- buforuje wpisy i wysyla paczkami (mniej transakcji)

Uzycie:
  pip install psycopg2-binary
  python3 maws_watcher.py /home/test/meteo_data.txt \
      --pg "dbname=meteo user=pi password=... host=localhost"

Opcjonalnie:
  --interval 1.0     co ile sekund sprawdzac plik
  --batch 50         co ile rekordow wymuszac zapis do bazy
  --from-start       przy pierwszym uruchomieniu przetworz caly istniejacy plik
                     (domyslnie: zacznij od konca, tylko nowe dane)
"""

import argparse
import os
import re
import signal
import sys
import time
from datetime import datetime
from typing import Optional

import psycopg2
from psycopg2.extras import execute_values

# Znacznik czasu w nawiasach + reszta linii (payload).
LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)$")
# Znaki sterujace ramki telegramu Vaisali (SOH/STX/ETX itp.) do usuniecia.
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")

# --- Kanaly PTU: 9 trojek (biezaca / max / min) ---------------------------
# Zweryfikuj 'sr' i 'ch7' z konfiguracja stacji (Lizard Setup) i ew. zmien nazwy.
PTU_CHANNELS = ["ta", "rh", "td", "pa", "sr", "ch6", "ch7", "ch8", "rain"]
SUFFIXES = ["", "_max", "_min"]
PTU_COLUMNS = [f"{ch}{sfx}" for ch in PTU_CHANNELS for sfx in SUFFIXES]

DDL = f"""
CREATE TABLE IF NOT EXISTS maws_wind (
    ts TIMESTAMP NOT NULL,
    ws DOUBLE PRECISION,
    wd DOUBLE PRECISION,
    PRIMARY KEY (ts)
);
CREATE TABLE IF NOT EXISTS maws_ptu (
    ts TIMESTAMP NOT NULL,
    {", ".join(f"{c} DOUBLE PRECISION" for c in PTU_COLUMNS)},
    PRIMARY KEY (ts)
);
"""

SQL_WIND = "INSERT INTO maws_wind (ts, ws, wd) VALUES %s ON CONFLICT (ts) DO NOTHING"
SQL_PTU = (
    f"INSERT INTO maws_ptu (ts, {', '.join(PTU_COLUMNS)}) VALUES %s "
    f"ON CONFLICT (ts) DO NOTHING"
)


def _num(token: str) -> Optional[float]:
    token = token.strip()
    if not token or set(token) == {"/"}:
        return None
    return float(token)


def parse_line(raw: str):
    """Zwraca ('WIND', tuple) lub ('PTU', tuple) lub None.

    Odporne na:
    - ramke telegramu (\\x01 TYP \\x02 ... \\x03) - znaki sterujace sa usuwane,
    - pola rozdzielone spacjami LUB tabulatorami,
    - linie inne niz pomiar (np. 'test2') - zwraca None.
    """
    m = LINE_RE.match(raw.rstrip("\r\n"))
    if not m:
        return None
    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    # usun znaki sterujace ramki, potem podziel po dowolnych bialych znakach
    payload = CTRL_RE.sub(" ", m.group(2))
    tokens = payload.split()
    if not tokens:
        return None

    msg_type, raw_vals = tokens[0], tokens[1:]
    values = [_num(t) for t in raw_vals]

    if msg_type == "WIND" and len(values) >= 2:
        return "WIND", (ts, values[0], values[1])
    if msg_type == "PTU" and len(values) >= len(PTU_COLUMNS):
        return "PTU", (ts, *values[: len(PTU_COLUMNS)])
    return None


class OffsetStore:
    """Trwale przechowuje pozycje odczytu, by po restarcie nie czytac od zera."""

    def __init__(self, log_path: str):
        self.path = log_path + ".offset"

    def load(self) -> int:
        try:
            with open(self.path) as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return -1  # brak zapisanego offsetu

    def save(self, offset: int) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(offset))
        os.replace(tmp, self.path)


class Watcher:
    def __init__(self, log_path, dsn, interval, batch, from_start):
        self.log_path = log_path
        self.dsn = dsn
        self.interval = interval
        self.batch = batch
        self.from_start = from_start
        self.offsets = OffsetStore(log_path)
        self.buf_wind, self.buf_ptu = [], []
        self.conn = None
        self.running = True
        self.partial = ""  # niedokonczona linia (zapis w trakcie)

    # --- baza ---
    def connect(self):
        while self.running:
            try:
                self.conn = psycopg2.connect(self.dsn)
                self.conn.autocommit = False
                with self.conn.cursor() as cur:
                    cur.execute(DDL)
                self.conn.commit()
                print("Polaczono z PostgreSQL", flush=True)
                return
            except psycopg2.Error as e:
                print(f"Blad polaczenia z baza: {e}; ponawiam za 5 s", file=sys.stderr, flush=True)
                time.sleep(5)

    def flush(self, offset: int):
        """Wysyla bufory do bazy w jednej transakcji, potem utrwala offset."""
        if not self.buf_wind and not self.buf_ptu:
            self.offsets.save(offset)
            return
        while self.running:
            try:
                with self.conn.cursor() as cur:
                    if self.buf_wind:
                        execute_values(cur, SQL_WIND, self.buf_wind, page_size=1000)
                    if self.buf_ptu:
                        execute_values(cur, SQL_PTU, self.buf_ptu, page_size=1000)
                self.conn.commit()
                n = len(self.buf_wind) + len(self.buf_ptu)
                self.buf_wind.clear()
                self.buf_ptu.clear()
                # offset zapisujemy dopiero PO udanym commicie:
                # w razie awarii dane zostana przeczytane ponownie,
                # a ON CONFLICT zignoruje duplikaty
                self.offsets.save(offset)
                print(f"{datetime.now():%H:%M:%S} zapisano {n} rekordow", flush=True)
                return
            except psycopg2.Error as e:
                print(f"Blad zapisu: {e}; reconnect...", file=sys.stderr, flush=True)
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.connect()

    # --- plik ---
    def initial_offset(self) -> int:
        saved = self.offsets.load()
        size = os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0
        if saved >= 0:
            if saved > size:
                print("Plik zmalal (rotacja?) - zaczynam od poczatku", flush=True)
                return 0
            return saved
        return 0 if self.from_start else size

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.connect()

        offset = self.initial_offset()
        print(f"Start od offsetu {offset} w {self.log_path}", flush=True)

        while self.running:
            try:
                size = os.path.getsize(self.log_path)
            except FileNotFoundError:
                time.sleep(self.interval)
                continue

            if size < offset:  # plik obciety/zrotowany
                print("Wykryto rotacje pliku - czytam od poczatku", flush=True)
                offset, self.partial = 0, ""

            if size > offset:
                with open(self.log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                self.consume(chunk)
                if len(self.buf_wind) + len(self.buf_ptu) >= self.batch:
                    self.flush(offset)

            else:
                # brak nowych danych - oprozij bufor, by nic nie wisialo
                self.flush(offset)

            time.sleep(self.interval)

        self.flush(offset)
        if self.conn:
            self.conn.close()
        print("Zatrzymano.", flush=True)

    def consume(self, chunk: str):
        data = self.partial + chunk
        lines = data.split("\n")
        # ostatni element moze byc niedokonczona linia (stacja w trakcie zapisu)
        self.partial = lines.pop()
        for line in lines:
            if not line.strip():
                continue
            parsed = parse_line(line)
            if parsed is None:
                print(f"Pominieto: {line[:80]}", file=sys.stderr, flush=True)
                continue
            kind, row = parsed
            (self.buf_wind if kind == "WIND" else self.buf_ptu).append(row)

    def stop(self, *_):
        self.running = False


def main():
    ap = argparse.ArgumentParser(description="Demon MAWS -> PostgreSQL")
    ap.add_argument("logfile", help="np. /home/test/meteo_data.txt")
    ap.add_argument("--pg", required=True, help="DSN PostgreSQL")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--from-start", action="store_true",
                    help="przetworz caly istniejacy plik przy pierwszym starcie")
    a = ap.parse_args()
    Watcher(a.logfile, a.pg, a.interval, a.batch, a.from_start).run()


if __name__ == "__main__":
    main()
