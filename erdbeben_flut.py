#!/usr/bin/env python3
"""
Zusatz-Warner: Erdbeben (USGS) und Hochwasser im Río de la Plata (SHN Argentinien).
Schickt Meldungen auf denselben ntfy-Kanal wie der Inumet-Warner.

Aufruf:
  python3 erdbeben_flut.py           -> prüfen und bei Bedarf senden
  python3 erdbeben_flut.py --probe   -> nur anzeigen, nichts senden
"""

import csv
import io
import json
import math
import os
import sys
import time
import urllib.request

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = os.environ.get(
    "STATE_FILE_EXTRA",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemeldet_extra.json"),
)

HEIM_LAT, HEIM_LNG = -34.4626, -57.8400  # Colonia del Sacramento

# ---------------------------------------------------------------- Erdbeben
USGS = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
# Region "bei uns": Südamerika + Südatlantik bis zu den Süd-Sandwich-Inseln
REGION = {"lat_min": -62.0, "lat_max": 5.0, "lng_min": -82.0, "lng_max": -10.0}
MAG_REGION_INFO = 6.0     # in der Region: Meldung ab dieser Stärke
MAG_REGION_ALARM = 7.0    # in der Region: lauter Alarm ab dieser Stärke
MAG_WELT_INFO = 7.5       # weltweit: leise Info ab dieser Stärke
NAH_KM = 500              # sehr nahe Beben (spürbar): Meldung schon ab Stärke 4,5

# ---------------------------------------------------------------- Hochwasser
SHN_CSV = "https://www.hidro.gob.ar/oceanografia/AlturasHorarias.asp?export=csv"
# Pegel in Metern (SHN-Bezug). Ab ca. 2,50 m steht in Buenos Aires Wasser auf
# ufernahen Straßen; Martín García liegt nur ca. 40 km vor Colonia.
PEGEL = ["Martín García", "Buenos Aires"]
STUFE_HINWEIS = 2.30
STUFE_ALARM = 2.70
STUFE_ENTWARNUNG = 2.00


def hole(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (privater Warn-Abruf)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def km(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def sende(titel, text, prio=3, tags="warning", link=None):
    if not NTFY_TOPIC:
        print("Kein NTFY_TOPIC gesetzt – nicht gesendet:", titel, file=sys.stderr)
        return False
    headers = {"Title": titel.encode("utf-8"), "Priority": str(prio), "Tags": tags}
    if link:
        headers["Click"] = link
    req = urllib.request.Request(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=text.encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200


def lade_status():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"beben": [], "flut_stufe": 0}


def speichere_status(st):
    st["beben"] = st["beben"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- Erdbeben prüfen
def erdbeben(st, probe):
    daten = json.loads(hole(USGS))
    for f in daten.get("features", []):
        pr = f["properties"]
        lng, lat, tiefe = f["geometry"]["coordinates"]
        mag = pr.get("mag") or 0
        eid = f.get("id")
        if pr.get("type") != "earthquake" or eid in st["beben"]:
            continue
        # nur frische Beben (letzte 6 Stunden) melden
        if time.time() - pr["time"] / 1000 > 6 * 3600:
            continue

        abstand = km(HEIM_LAT, HEIM_LNG, lat, lng)
        in_region = (REGION["lat_min"] <= lat <= REGION["lat_max"]
                     and REGION["lng_min"] <= lng <= REGION["lng_max"])
        tsunami = pr.get("tsunami") == 1

        if abstand <= NAH_KM:
            prio, grund = 5 if mag >= 5.5 else 4, "in unserer Nähe"
        elif in_region and (mag >= MAG_REGION_ALARM or (tsunami and mag >= MAG_REGION_INFO)):
            prio, grund = 5, "Südatlantik/Südamerika"
        elif in_region and mag >= MAG_REGION_INFO:
            prio, grund = 3, "Südatlantik/Südamerika"
        elif mag >= MAG_WELT_INFO:
            prio, grund = 2, "weltweit"
        else:
            continue

        zeit = time.strftime("%d.%m. %H:%M", time.gmtime(pr["time"] / 1000 - 3 * 3600))
        titel = f"Erdbeben M {mag:.1f} – {pr.get('place', '?')}"
        zeilen = [
            f"Stärke: {mag:.1f}  ·  Tiefe: {tiefe:.0f} km",
            f"Zeit: {zeit} Uhr (Uruguay)",
            f"Entfernung zu Colonia: ca. {abstand:,.0f} km".replace(",", "."),
            f"Einordnung: {grund}",
        ]
        if tsunami:
            zeilen.append("⚠ USGS-Tsunami-Kennzeichen gesetzt: Bitte offizielle Meldungen "
                          "(SINAE, Prefectura) prüfen. Das Kennzeichen allein ist noch keine Tsunami-Warnung.")
        if probe:
            print(f"[Prio {prio}] {titel}\n" + "\n".join(zeilen) + "\n")
            continue
        if sende(titel, "\n".join(zeilen), prio=prio, tags="ocean" if tsunami else "globe_with_meridians",
                 link=pr.get("url")):
            st["beben"].append(eid)
            print("Gesendet:", titel)


# ---------------------------------------------------------------- Hochwasser prüfen
def hochwasser(st, probe):
    roh = hole(SHN_CSV).decode("latin-1")
    zeilen = list(csv.reader(io.StringIO(roh), delimiter=";"))
    kopf, daten = zeilen[0], [z for z in zeilen[1:] if z]
    if not daten:
        return
    neu = daten[0]  # oberste Zeile = neueste Messung
    werte = {}
    for name in PEGEL:
        for i, k in enumerate(kopf):
            if k.strip().lower() == name.lower():
                try:
                    werte[name] = float(neu[i].replace(",", "."))
                except (ValueError, IndexError):
                    pass
    if not werte:
        print("Hochwasser: keine Pegelwerte gefunden.", file=sys.stderr)
        return

    hoechst = max(werte.values())
    stufe = 2 if hoechst >= STUFE_ALARM else 1 if hoechst >= STUFE_HINWEIS else 0
    text = (f"Messung {neu[0]} (Argentinien-Zeit = Uruguay-Zeit)\n"
            + "\n".join(f"{n}: {v:.2f} m" for n, v in werte.items())
            + f"\n\nAb ca. {STUFE_ALARM:.2f} m ist mit Überflutung ufernaher Bereiche zu rechnen "
              "(Rambla, Hafen, tiefe Straßen am Wasser).")

    if probe:
        print(f"Pegel jetzt: {werte} -> Stufe {stufe}\n")
        return

    alt = st.get("flut_stufe", 0)
    if stufe > alt:
        if stufe == 2:
            sende("Hochwasser Río de la Plata: ALARM", text, prio=5, tags="ocean,rotating_light",
                  link="https://www.hidro.gob.ar/oceanografia/alturashorarias.asp")
        else:
            sende("Hochwasser Río de la Plata: erhöhter Pegel", text, prio=4, tags="ocean",
                  link="https://www.hidro.gob.ar/oceanografia/alturashorarias.asp")
        st["flut_stufe"] = stufe
    elif alt > 0 and hoechst < STUFE_ENTWARNUNG:
        sende("Hochwasser: Entwarnung", text, prio=3, tags="green_circle")
        st["flut_stufe"] = 0


def main():
    probe = "--probe" in sys.argv
    st = lade_status()
    for teil in (erdbeben, hochwasser):
        try:
            teil(st, probe)
        except Exception as e:  # ein Ausfall soll den anderen Teil nicht stoppen
            print(f"Fehler bei {teil.__name__}: {e}", file=sys.stderr)
    if not probe:
        st["geprueft_am"] = time.strftime("%Y-%m-%d")
        speichere_status(st)


if __name__ == "__main__":
    main()
