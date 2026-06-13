# -*- coding: utf-8 -*-
import serial
import requests
import time
from datetime import datetime

# --- KONFIGURACJA ---
SERIAL_PORT = '/dev/serial0'
BAUD_RATE = 9600
SERVER_URL = 'http://150.254.126.210:4101/api/meteo'
FILE_NAME = 'meteo_data.txt'

# Interwał wysyłki wiatru na serwer (w sekundach)
WIND_SEND_INTERVAL = 60

def main():
    print("[*] Start odczytu czujnikow (Tryb oszczędny - WIND co 60s)")
    print(f"[*] Nasluch na porcie: {SERIAL_PORT}")
    print(f"[*] Serwer docelowy: {SERVER_URL}")
    print("-" * 50)

    last_wind_send_time = 0

    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except Exception as e:
        print("BLAD KRYTYCZNY: Nie mozna otworzyc portu szeregowego:", e)
        return

    while True:
        try:
            if ser.in_waiting > 0:
                raw_data = ser.readline()
                data_str = raw_data.decode('utf-8', errors='replace').strip()

                if data_str:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    # 1. Zapis na karcie SD (Zapisujemy wszystko bez filtracji)
                    with open(FILE_NAME, 'a', encoding='utf-8') as f:
                        f.write(f"[{timestamp}] {data_str}\n")

                    # 2. Logika filtrowania wysyłki
                    should_send = True

                    # Jeśli w danych jest słowo WIND, sprawdź czy minęło 60 sekund
                    if "WIND" in data_str:
                        current_time = time.time()
                        if current_time - last_wind_send_time < WIND_SEND_INTERVAL:
                            should_send = False
                        else:
                            last_wind_send_time = current_time

                    # Dane PTU (Pressure/Temp) są wysyłane zawsze (should_send zostaje True)

                    # 3. Wysylka na serwer (tylko jeśli dane przeszły filtr)
                    if should_send:
                        payload = [{
                            "device_id": "meteo_rpi",
                            "timestamp": timestamp,
                            "sensor_reading": data_str
                        }]

                        try:
                            response = requests.post(SERVER_URL, json=payload, timeout=3)
                            if response.status_code == 200:
                                status_msg = "[Wyslano OK]"
                            else:
                                status_msg = f"[Blad serwera: {response.status_code}]"
                        except requests.RequestException:
                            status_msg = "[Blad polaczenia]"

                        print(f"[{timestamp}] Odczyt: {data_str} -> {status_msg}")
                    else:
                        # Opcjonalnie: info w konsoli malinki, że pominięto wysyłkę
                        # print(f"[{timestamp}] WIND (pominięto wysyłkę - throttling)")
                        pass

        except Exception as err:
            print("Nieoczekiwany blad w glownej petli:", err)
            time.sleep(1)

if __name__ == '__main__':
    main()
