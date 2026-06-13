#!/usr/bin/env python3
"""
Czyszczenie duplikatow z tabel maws_wind i maws_ptu.

Duplikat = wiele wierszy o tym samym znaczniku czasu (ts).
Skrypt zostawia DOKLADNIE JEDEN wiersz na kazdy ts (ten wstawiony jako pierwszy,
czyli o najmniejszym ctid), a pozostale usuwa.

BEZPIECZENSTWO:
- domyslnie DRY-RUN: tylko raportuje, ile duplikatow znajdzie, nic nie kasuje
- usuwanie nastepuje tylko z flaga --apply
- caly proces w jednej transakcji na tabele (albo wszystko, albo nic)
- ZATRZYMAJ demona (systemctl stop maws) przed uruchomieniem z --apply,
  zeby nikt nie pisal do tabel w trakcie

Uzycie:
  python3 remove_duplicates.py --pg "dbname=meteo user=... host=... sslmode=require"            # podglad
  python3 remove_duplicates.py --pg "..." --apply                                                # kasowanie
  python3 remove_duplicates.py --pg "..." --apply --add-constraint                               # + zabezpieczenie na przyszlosc
"""

import argparse
import sys

import psycopg2

TABLES = ["maws_wind", "maws_ptu"]


def count_duplicates(cur, table: str) -> int:
    """Liczba wierszy do usuniecia = wiersze ponad jeden na kazdy ts."""
    cur.execute(
        f"SELECT COALESCE(SUM(cnt - 1), 0) "
        f"FROM (SELECT ts, COUNT(*) AS cnt FROM {table} GROUP BY ts HAVING COUNT(*) > 1) s"
    )
    return int(cur.fetchone()[0])


def total_rows(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])


def delete_duplicates(cur, table: str) -> int:
    """Usuwa duplikaty po ts, zostawiajac wiersz o najmniejszym ctid. Zwraca liczbe usunietych."""
    cur.execute(
        f"DELETE FROM {table} a USING {table} b "
        f"WHERE a.ts = b.ts AND a.ctid > b.ctid"
    )
    return cur.rowcount


def add_unique_constraint(cur, table: str) -> bool:
    """Dodaje UNIQUE(ts), o ile jeszcze nie istnieje. Wymaga braku duplikatow."""
    idx = f"uq_{table}_ts"
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE tablename = %s AND indexname = %s",
        (table, idx),
    )
    if cur.fetchone():
        return False
    cur.execute(f"CREATE UNIQUE INDEX {idx} ON {table} (ts)")
    return True


def main():
    ap = argparse.ArgumentParser(description="Czyszczenie duplikatow w tabelach MAWS")
    ap.add_argument("--pg", required=True, help="DSN PostgreSQL")
    ap.add_argument("--apply", action="store_true",
                    help="faktycznie usun duplikaty (bez tego: tylko podglad)")
    ap.add_argument("--add-constraint", action="store_true",
                    help="po czyszczeniu zaloz UNIQUE(ts), by zapobiec duplikatom na przyszlosc")
    ap.add_argument("--tables", nargs="+", default=TABLES,
                    help=f"ktore tabele czyscic (domyslnie: {' '.join(TABLES)})")
    args = ap.parse_args()

    conn = psycopg2.connect(args.pg)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            for table in args.tables:
                before = total_rows(cur, table)
                dupes = count_duplicates(cur, table)
                print(f"[{table}] wierszy: {before}, duplikatow do usuniecia: {dupes}")

                if not args.apply:
                    continue

                if dupes:
                    removed = delete_duplicates(cur, table)
                    print(f"   usunieto {removed} wierszy, zostalo {before - removed}")
                else:
                    print("   brak duplikatow - nic nie usunieto")

                if args.add_constraint:
                    if add_unique_constraint(cur, table):
                        print(f"   zalozono UNIQUE(ts) na {table}")
                    else:
                        print(f"   UNIQUE(ts) na {table} juz istnieje")

        if args.apply:
            conn.commit()
            print("Zatwierdzono zmiany (COMMIT).")
        else:
            conn.rollback()
            print("\nTryb podgladu (dry-run) - nic nie zmieniono. "
                  "Dodaj --apply, aby usunac duplikaty.")
    except psycopg2.Error as e:
        conn.rollback()
        print(f"Blad - wycofano transakcje (ROLLBACK): {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
