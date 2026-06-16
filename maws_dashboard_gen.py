# -*- coding: utf-8 -*-
"""
Generator statycznego dashboardu MAWS.

Czyta dane z PostgreSQL (tabele maws_wind, maws_ptu), downsampluje do jednego
punktu na minute (najswiezszy odczyt z kazdej minuty) i zapisuje JEDEN
samowystarczalny plik HTML z danymi wbudowanymi w stronie.

Bez serwera. Uruchamiany np. z crona co minute:
  * * * * * /usr/bin/python3 /home/test/maws_dashboard_gen.py >> /home/test/dashboard_gen.log 2>&1

Strona sama przeladowuje sie co RELOAD_SECONDS, by pokazac swiezo wygenerowany plik.

Uzycie:
  python3 maws_dashboard_gen.py \
      --pg "dbname=meteo user=postgres host=mrozowiska.pl sslmode=require" \
      --out /var/www/html/meteo.html

Haslo: najlepiej przez ~/.pgpass lub PGPASSWORD (wtedy pomijasz password= w DSN).
"""

import argparse
import json
import os
import sys
from datetime import datetime

import psycopg2
import psycopg2.extras

# --- Parametry (mozna nadpisac flagami) ---
WINDOW_DAYS = 7        # ile dni historii wciagnac (najdluzszy zakres na dashboardzie = tydzien)
BUCKET_SECONDS = 60    # rozdzielczosc downsamplingu (1 punkt na te liczbe sekund)
TERMINAL_LINES = 50    # ile ostatnich odczytow w panelu "strumien danych"
RELOAD_SECONDS = 60    # co ile sekund strona sama sie przeladuje

# Mapowanie kolumn PTU z bazy -> nazwy pol oczekiwane przez frontend.
# (zmien 'sr'->solar jesli Twoj kanal slonca jest inny - patrz uwagi do parsera)
PTU_MAP = {
    "ta": "temp", "ta_max": "temp_max", "ta_min": "temp_min",
    "rh": "humidity", "rh_max": "humidity_max", "rh_min": "humidity_min",
    "pa": "pressure", "pa_max": "pressure_max", "pa_min": "pressure_min",
    "sr": "solar", "sr_max": "solar_max", "sr_min": "solar_min",
    "rain": "rain",
}


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S")


def fetch_series(cur, table, cols, bucket, window_days):
    """Zwraca po jednym (najswiezszym) wierszu na kazde okno 'bucket' sekund."""
    collist = ", ".join(cols)
    cur.execute(
        f"""
        SELECT DISTINCT ON (floor(extract(epoch from ts) / %(b)s)) {collist}
        FROM {table}
        WHERE ts >= now() - (%(d)s || ' days')::interval
        ORDER BY floor(extract(epoch from ts) / %(b)s), ts DESC
        """,
        {"b": bucket, "d": window_days},
    )
    return cur.fetchall()


def build_payload(conn):
    series = []
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # --- WIND ---
        wind_rows = fetch_series(cur, "maws_wind", ["ts", "ws", "wd"],
                                 BUCKET_SECONDS, WINDOW_DAYS)
        for r in wind_rows:
            rec = {"type": "WIND", "timestamp": _iso(r["ts"])}
            if r["ws"] is not None:
                rec["wind_speed"] = r["ws"]
            if r["wd"] is not None:
                rec["wind_dir"] = r["wd"]
            parts = [f"{r['ws']:.1f}" if r["ws"] is not None else "///",
                     f"{r['wd']:.0f}" if r["wd"] is not None else "///"]
            rec["sensor_reading"] = "WIND   " + "   ".join(parts)
            series.append(rec)

        # --- PTU ---
        ptu_cols = ["ts"] + list(PTU_MAP.keys())
        ptu_rows = fetch_series(cur, "maws_ptu", ptu_cols, BUCKET_SECONDS, WINDOW_DAYS)
        for r in ptu_rows:
            rec = {"type": "PTU", "timestamp": _iso(r["ts"])}
            for db_col, js_field in PTU_MAP.items():
                if r[db_col] is not None:
                    rec[js_field] = r[db_col]
            bits = []
            if r["ta"] is not None:   bits.append(f"T:{r['ta']:.1f}")
            if r["rh"] is not None:   bits.append(f"RH:{r['rh']:.0f}")
            if r["pa"] is not None:   bits.append(f"P:{r['pa']:.1f}")
            if r["sr"] is not None:   bits.append(f"SR:{r['sr']:.0f}")
            if r["rain"] is not None: bits.append(f"R:{r['rain']:.1f}")
            rec["sensor_reading"] = "PTU   " + "  ".join(bits)
            series.append(rec)

    # sortuj rosnaco po czasie (frontend oczekuje chronologii)
    series.sort(key=lambda d: d["timestamp"])
    return series


def render_html(series):
    data_json = json.dumps(series, ensure_ascii=True, separators=(",", ":"))
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = HTML_TEMPLATE
    html = html.replace("__SERIES__", data_json)
    html = html.replace("__RELOAD_MS__", str(RELOAD_SECONDS * 1000))
    html = html.replace("__GENERATED_AT__", generated_at)
    return html


def main():
    global WINDOW_DAYS, BUCKET_SECONDS, TERMINAL_LINES, RELOAD_SECONDS
    ap = argparse.ArgumentParser(description="Generator statycznego dashboardu MAWS")
    ap.add_argument("--pg", required=True, help="DSN PostgreSQL")
    ap.add_argument("--out", required=True, help="sciezka wyjsciowego pliku HTML")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--bucket", type=int, default=BUCKET_SECONDS, help="downsampling [s]")
    ap.add_argument("--reload", type=int, default=RELOAD_SECONDS, help="auto-reload strony [s]")
    a = ap.parse_args()
    WINDOW_DAYS, BUCKET_SECONDS, RELOAD_SECONDS = a.days, a.bucket, a.reload

    try:
        conn = psycopg2.connect(a.pg)
    except psycopg2.Error as e:
        print(f"Blad polaczenia z baza: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        series = build_payload(conn)
    finally:
        conn.close()

    html = render_html(series)

    # zapis atomowy: najpierw .tmp, potem podmiana (przegladarka nigdy nie zlapie polowy pliku)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, a.out)
    print(f"{datetime.now():%H:%M:%S} zapisano {a.out} ({len(series)} punktow, {len(html)//1024} kB)")


# ============================================================================
# SZABLON HTML (na bazie kodu kolegi; usunieto Flask/fetch, dane wbudowane)
# ============================================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MAWS Meteo Pro</title>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/moment"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-moment"></script>

    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800&display=swap');

        :root {
            --bg: #ffffff; --panel-bg: #f7f9fc; --text: #1f2937; --border: #e6eaf0;
            --accent: #0ca678; --accent-glow: rgba(12, 166, 120, 0.25);
            --temp: #e11d48; --temp-glow: rgba(225, 29, 72, 0.18);
            --hum: #2563eb; --hum-glow: rgba(37, 99, 235, 0.18);
            --press: #7c3aed; --press-glow: rgba(124, 58, 237, 0.18);
            --wind: #d97706; --wind-glow: rgba(217, 119, 6, 0.18);
            --solar: #ea580c; --solar-glow: rgba(234, 88, 12, 0.18);
            --rain: #0891b2; --rain-glow: rgba(8, 145, 178, 0.18);
        }
        body { font-family: 'Inter', sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 20px; }
        h1 { text-align: center; color: var(--text); font-weight: 800; letter-spacing: 1px; margin-bottom: 5px; }
        .last-update { text-align: center; color: #64748b; font-size: 0.95em; margin-bottom: 30px; font-weight: 600; }

        .cards-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .card {
            background: var(--panel-bg); border-radius: 16px; padding: 20px;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            box-shadow: 0 4px 14px rgba(15, 23, 42, 0.08); border: 1px solid var(--border); border-top: 5px solid #cbd5e1; position: relative;
        }
        .card-title { font-size: 0.95em; color: #64748b; text-transform: uppercase; font-weight: 800; letter-spacing: 1px; margin-bottom: 15px; text-align: center; }

        .stats-container { display: flex; flex-direction: column; align-items: center; width: 100%; }
        .stat-edge { font-size: 0.9rem; color: #94a3b8; font-weight: 600; line-height: 1.2; display: flex; align-items: center; gap: 5px;}
        .stat-edge.max { margin-bottom: 5px; }
        .stat-edge.min { margin-top: 5px; }
        .stat-edge span { font-weight: 800; color: #475569; }

        .card-value { font-size: 2.8rem; font-weight: 800; line-height: 1; }
        .card-unit { font-size: 1rem; color: #64748b; font-weight: 600; margin-left: 4px; }

        .card.temp { border-top-color: var(--temp); } .card.temp .card-value { color: var(--temp); }
        .card.hum { border-top-color: var(--hum); } .card.hum .card-value { color: var(--hum); }
        .card.press { border-top-color: var(--press); } .card.press .card-value { color: var(--press); }
        .card.wind { border-top-color: var(--wind); } .card.wind .card-value { color: var(--wind); }
        .card.solar { border-top-color: var(--solar); } .card.solar .card-value { color: var(--solar); }
        .card.rain { border-top-color: var(--rain); } .card.rain .card-value { color: var(--rain); }

        .controls { text-align: center; margin-bottom: 30px; display: flex; justify-content: center; flex-wrap: wrap; gap: 10px; }
        button {
            background: #eef2f7; color: #475569; border: 1px solid var(--border); padding: 10px 25px; border-radius: 8px;
            cursor: pointer; font-weight: 600; font-size: 0.95rem; transition: all 0.2s ease;
        }
        button.active, button:hover { background: var(--accent); color: #fff; border-color: var(--accent); box-shadow: 0 4px 12px var(--accent-glow); }

        .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 30px; }
        @media (max-width: 1200px) { .chart-grid { grid-template-columns: 1fr; } }
        .chart-container { background: var(--panel-bg); border-radius: 16px; padding: 20px; box-shadow: 0 4px 14px rgba(15, 23, 42, 0.08); border: 1px solid var(--border); }

        .terminal-header { color: var(--text); font-weight: 800; font-size: 1.2rem; margin-top: 40px; margin-bottom: 10px; letter-spacing: 1px; text-transform: uppercase; }
        .terminal { background: #f4f6f8; border: 1px solid var(--border); border-radius: 12px; padding: 15px; font-family: 'Consolas', monospace; height: 300px; overflow-y: auto; color: #334155; }
        .terminal-line { margin: 4px 0; font-size: 0.9em; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
        .term-wind { color: var(--wind); } .term-ptu { color: var(--accent); }
    </style>
</head>
<body>

    <h1>STACJA POGODOWA MAWS</h1>
    <div class="last-update" id="last-update">Wygenerowano: __GENERATED_AT__</div>

    <div class="cards-container">
        <div class="card temp">
            <div class="card-title">Temperatura</div>
            <div class="stats-container">
                <div class="stat-edge max">&#9650; MAX: <span id="max-temp">--</span>&deg;C</div>
                <div><span class="card-value" id="cur-temp">--</span><span class="card-unit">&deg;C</span></div>
                <div class="stat-edge min">&#9660; MIN: <span id="min-temp">--</span>&deg;C</div>
            </div>
        </div>

        <div class="card hum">
            <div class="card-title">Wilgotno&#347;&#263;</div>
            <div class="stats-container">
                <div class="stat-edge max">&#9650; MAX: <span id="max-hum">--</span>%</div>
                <div><span class="card-value" id="cur-hum">--</span><span class="card-unit">%</span></div>
                <div class="stat-edge min">&#9660; MIN: <span id="min-hum">--</span>%</div>
            </div>
        </div>

        <div class="card press">
            <div class="card-title">Ci&#347;nienie</div>
            <div class="stats-container">
                <div class="stat-edge max">&#9650; MAX: <span id="max-press">--</span></div>
                <div><span class="card-value" id="cur-press">--</span><span class="card-unit">hPa</span></div>
                <div class="stat-edge min">&#9660; MIN: <span id="min-press">--</span></div>
            </div>
        </div>

        <div class="card wind">
            <div class="card-title">Wiatr</div>
            <div class="stats-container">
                <div class="stat-edge max" style="visibility: hidden;">.</div>
                <div><span class="card-value" id="cur-wind">--</span><span class="card-unit">m/s</span></div>
                <div class="stat-edge min">Kierunek: <span id="cur-wind-dir">--</span>&deg;</div>
            </div>
        </div>

        <div class="card solar">
            <div class="card-title">Promieniowanie s&#322;oneczne</div>
            <div class="stats-container">
                <div class="stat-edge max">&#9650; MAX: <span id="max-solar">--</span></div>
                <div><span class="card-value" id="cur-solar">--</span><span class="card-unit">W/m&sup2;</span></div>
                <div class="stat-edge min">&#9660; MIN: <span id="min-solar">--</span></div>
            </div>
        </div>

        <div class="card rain">
            <div class="card-title">Opady</div>
            <div class="stats-container">
                <div class="stat-edge max" style="visibility: hidden;">.</div>
                <div><span class="card-value" id="cur-rain">--</span><span class="card-unit">mm</span></div>
                <div class="stat-edge min" style="visibility: hidden;">.</div>
            </div>
        </div>
    </div>

    <div class="controls">
        <button onclick="setTimeRange(6)" id="btn-6">Ostatnie 6H</button>
        <button onclick="setTimeRange(24)" id="btn-24" class="active">Ostatnie 24H</button>
        <button onclick="setTimeRange(72)" id="btn-72">3 Dni</button>
        <button onclick="setTimeRange(168)" id="btn-168">Tydzie&#324;</button>
    </div>

    <div class="chart-grid">
        <div class="chart-container"><canvas id="ptuChart" height="200"></canvas></div>
        <div class="chart-container"><canvas id="windChart" height="200"></canvas></div>
        <div class="chart-container"><canvas id="pressChart" height="200"></canvas></div>
        <div class="chart-container"><canvas id="solarRainChart" height="200"></canvas></div>
    </div>

    <h3 class="terminal-header">Bie&#380;&#261;cy strumie&#324; danych</h3>
    <div class="terminal" id="terminal"></div>

    <script>
        // Dane wbudowane statycznie przez generator (zamiast fetch z serwera)
        const EMBEDDED_SERIES = __SERIES__;
        const RELOAD_MS = __RELOAD_MS__;
        let allData = EMBEDDED_SERIES;
        let ptuChartInstance, windChartInstance, pressChartInstance, solarRainChartInstance;
        let currentTimeRangeHours = 24;

        Chart.defaults.color = '#475569';
        Chart.defaults.borderColor = '#e6eaf0';
        Chart.defaults.font.family = "'Inter', sans-serif";

        function setTimeRange(hours) {
            currentTimeRangeHours = hours;
            document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
            try { document.getElementById('btn-' + hours).classList.add('active'); } catch(e){}
            try { localStorage.setItem('maws_range', hours); } catch(e){}
            updateCharts();
        }

        function initCharts() {
            const commonOptions = {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } } },
                scales: {
                    x: { type: 'time', time: { tooltipFormat: 'YYYY-MM-DD HH:mm', displayFormats: { hour: 'HH:mm' } } }
                }
            };

            ptuChartInstance = new Chart(document.getElementById('ptuChart'), {
                type: 'line',
                data: { datasets: [
                    { label: 'Temp (\u00B0C)', data: [], borderColor: '#e11d48', backgroundColor: 'rgba(225, 29, 72, 0.10)', fill: true, yAxisID: 'yTemp', tension: 0.4, pointRadius: 0, borderWidth: 2 },
                    { label: 'Wilg (%)', data: [], borderColor: '#2563eb', yAxisID: 'yHum', tension: 0.4, pointRadius: 0, borderWidth: 2 }
                ]},
                options: {
                    ...commonOptions,
                    scales: {
                        x: commonOptions.scales.x,
                        yTemp: { type: 'linear', position: 'left', title: {display: true, text: 'Temperatura (\u00B0C)'} },
                        yHum: { type: 'linear', position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false }, title: {display: true, text: 'Wilgotno\u015B\u0107 (%)'} }
                    }
                }
            });

            windChartInstance = new Chart(document.getElementById('windChart'), {
                type: 'line',
                data: { datasets: [
                    { label: 'Pr\u0119dko\u015B\u0107 Wiatru (m/s)', data: [], borderColor: '#d97706', backgroundColor: 'rgba(217, 119, 6, 0.10)', fill: true, yAxisID: 'ySpeed', tension: 0.3, pointRadius: 1, borderWidth: 2 },
                    { type: 'scatter', label: 'Kierunek (\u00B0)', data: [], backgroundColor: '#0ca678', yAxisID: 'yDir', pointRadius: 3 }
                ]},
                options: {
                    ...commonOptions,
                    scales: {
                        x: commonOptions.scales.x,
                        ySpeed: { type: 'linear', position: 'left', beginAtZero: true, title: {display: true, text: 'Pr\u0119dko\u015B\u0107 (m/s)'} },
                        yDir: { type: 'linear', position: 'right', min: 0, max: 360, ticks:{stepSize:90}, grid: { drawOnChartArea: false }, title: {display: true, text: 'Kierunek (\u00B0)'} }
                    }
                }
            });

            pressChartInstance = new Chart(document.getElementById('pressChart'), {
                type: 'line',
                data: { datasets: [
                    { label: 'Ci\u015Bnienie (hPa)', data: [], borderColor: '#7c3aed', backgroundColor: 'rgba(124, 58, 237, 0.10)', fill: true, tension: 0.4, pointRadius: 0, borderWidth: 2 }
                ]},
                options: {
                    ...commonOptions,
                    scales: {
                        x: commonOptions.scales.x,
                        y: { type: 'linear', position: 'left', title: {display: true, text: 'Ci\u015Bnienie (hPa)'} }
                    }
                }
            });

            solarRainChartInstance = new Chart(document.getElementById('solarRainChart'), {
                type: 'line',
                data: { datasets: [
                    { label: 'Promieniowanie (W/m\u00B2)', data: [], borderColor: '#ea580c', backgroundColor: 'rgba(234, 88, 12, 0.15)', fill: true, yAxisID: 'ySolar', tension: 0.4, pointRadius: 0, borderWidth: 2 },
                    { type: 'bar', label: 'Opady (mm)', data: [], backgroundColor: '#0891b2', yAxisID: 'yRain', borderRadius: 4 }
                ]},
                options: {
                    ...commonOptions,
                    scales: {
                        x: commonOptions.scales.x,
                        ySolar: { type: 'linear', position: 'left', beginAtZero: true, title: {display: true, text: 'Promieniowanie (W/m\u00B2)'} },
                        yRain: { type: 'linear', position: 'right', beginAtZero: true, grid: { drawOnChartArea: false }, title: {display: true, text: 'Opady (mm)'} }
                    }
                }
            });
        }

        function updateCharts() {
            if (!allData.length) return;
            const cutoff = moment().subtract(currentTimeRangeHours, 'hours');
            const filtered = allData.filter(d => moment(d.timestamp).isAfter(cutoff));
            const step = filtered.length > 2000 ? Math.ceil(filtered.length / 1000) : 1;

            const temp = [], hum = [], press = [], solar = [], rain = [];
            const wSpd = [], wDir = [];

            for (let i = 0; i < filtered.length; i += step) {
                const d = filtered[i];
                if (d.type === 'PTU') {
                    if (d.temp !== undefined) temp.push({ x: d.timestamp, y: d.temp });
                    if (d.humidity !== undefined) hum.push({ x: d.timestamp, y: d.humidity });
                    if (d.pressure !== undefined) press.push({ x: d.timestamp, y: d.pressure });
                    if (d.solar !== undefined) solar.push({ x: d.timestamp, y: d.solar });
                    if (d.rain !== undefined) rain.push({ x: d.timestamp, y: d.rain });
                } else if (d.type === 'WIND') {
                    if (d.wind_speed !== undefined) wSpd.push({ x: d.timestamp, y: d.wind_speed });
                    if (d.wind_dir !== undefined) wDir.push({ x: d.timestamp, y: d.wind_dir });
                }
            }

            ptuChartInstance.data.datasets[0].data = temp;
            ptuChartInstance.data.datasets[1].data = hum;
            ptuChartInstance.update('none');

            windChartInstance.data.datasets[0].data = wSpd;
            windChartInstance.data.datasets[1].data = wDir;
            windChartInstance.update('none');

            pressChartInstance.data.datasets[0].data = press;
            pressChartInstance.update('none');

            solarRainChartInstance.data.datasets[0].data = solar;
            solarRainChartInstance.data.datasets[1].data = rain;
            solarRainChartInstance.update('none');
        }

        function updateCards() {
            if (!allData.length) return;
            const lastPTU = [...allData].reverse().find(d => d.type === 'PTU');
            const lastWIND = [...allData].reverse().find(d => d.type === 'WIND');

            if(lastPTU) {
                if(lastPTU.temp !== undefined) document.getElementById('cur-temp').innerText = lastPTU.temp.toFixed(1);
                if(lastPTU.temp_max !== undefined) document.getElementById('max-temp').innerText = lastPTU.temp_max.toFixed(1);
                if(lastPTU.temp_min !== undefined) document.getElementById('min-temp').innerText = lastPTU.temp_min.toFixed(1);

                if(lastPTU.humidity !== undefined) document.getElementById('cur-hum').innerText = Math.round(lastPTU.humidity);
                if(lastPTU.humidity_max !== undefined) document.getElementById('max-hum').innerText = Math.round(lastPTU.humidity_max);
                if(lastPTU.humidity_min !== undefined) document.getElementById('min-hum').innerText = Math.round(lastPTU.humidity_min);

                if(lastPTU.pressure !== undefined) document.getElementById('cur-press').innerText = lastPTU.pressure.toFixed(1);
                if(lastPTU.pressure_max !== undefined) document.getElementById('max-press').innerText = lastPTU.pressure_max.toFixed(1);
                if(lastPTU.pressure_min !== undefined) document.getElementById('min-press').innerText = lastPTU.pressure_min.toFixed(1);

                if(lastPTU.solar !== undefined) document.getElementById('cur-solar').innerText = Math.round(lastPTU.solar);
                if(lastPTU.solar_max !== undefined) document.getElementById('max-solar').innerText = Math.round(lastPTU.solar_max);
                if(lastPTU.solar_min !== undefined) document.getElementById('min-solar').innerText = Math.round(lastPTU.solar_min);

                if(lastPTU.rain !== undefined) document.getElementById('cur-rain').innerText = lastPTU.rain.toFixed(1);
            }

            if(lastWIND) {
                if(lastWIND.wind_speed !== undefined) document.getElementById('cur-wind').innerText = lastWIND.wind_speed.toFixed(1);
                if(lastWIND.wind_dir !== undefined) document.getElementById('cur-wind-dir').innerText = Math.round(lastWIND.wind_dir);
            }
        }

        function updateTerminal() {
            const term = document.getElementById('terminal');
            term.innerHTML = allData.slice(-50).reverse().map(d => {
                const cls = d.type === 'WIND' ? 'term-wind' : 'term-ptu';
                return `<div class="terminal-line ${cls}">[${d.timestamp}] ${d.sensor_reading}</div>`;
            }).join('');
        }

        // Start: dane juz sa wbudowane, wiec nie ma fetch.
        initCharts();
        // przywroc ostatnio wybrany zakres czasu (po przeladowaniu strony)
        try {
            const saved = parseInt(localStorage.getItem('maws_range'));
            if (saved) {
                currentTimeRangeHours = saved;
                document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
                const b = document.getElementById('btn-' + saved); if (b) b.classList.add('active');
            }
        } catch(e){}
        updateCards();
        updateCharts();
        updateTerminal();
        // przeladuj strone, by pobrac swiezo wygenerowany plik
        setTimeout(() => location.reload(), RELOAD_MS);
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
