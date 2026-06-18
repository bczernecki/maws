# -*- coding: utf-8 -*-
import serial
import time
from datetime import datetime

# --- KONFIGURACJA ---
SERIAL_PORT = '/dev/serial0'
BAUD_RATE = 9600
FILE_NAME = 'meteo_data.txt'


def main():
    print("[*] Start odczytu czujnikow")
    print(f"[*] Nasluch na porcie: {SERIAL_PORT}")
    print(f"[*] Zapis do pliku: {FILE_NAME}")
    print("-" * 50)

    try:
        # timeout=1 sprawia, ze readline() blokuje maks. 1 s czekajac na dane,
        # oddajac w tym czasie CPU. Brak busy-wait => zuzycie CPU bliskie zeru.
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except Exception as e:
        print("BLAD KRYTYCZNY: Nie mozna otworzyc portu szeregowego:", e)
        return

    while True:
        try:
            # readline() blokuje do konca linii albo do uplywu timeoutu.
            # Gdy brak danych, zwraca b'' i po prostu czekamy dalej.
            raw_data = ser.readline()
            if not raw_data:
                continue

            data_str = raw_data.decode('utf-8', errors='replace').strip()
            if not data_str:
                continue

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Zapis na karte SD (wszystko, bez filtracji)
            with open(FILE_NAME, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] {data_str}\n")

            print(f"[{timestamp}] {data_str}")

        except Exception as err:
            print("Nieoczekiwany blad w glownej petli:", err)
            time.sleep(1)


if __name__ == '__main__':
    main()
