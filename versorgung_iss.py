#!/usr/bin/env python3
"""
Versorgung und ISS – Meldungen auf denselben ntfy-Kanal.

  python3 versorgung_iss.py --ose    Wasserabstellungen (OSE) in Colonia del Sacramento
  python3 versorgung_iss.py --iss    sichtbare ISS-Überflüge heute Abend/Nacht über Colonia
  zusätzlich --probe                 nur anzeigen, nichts senden
"""

import html
import json
import os
import re
import sys
import time
import urllib.request

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemeldet_versorgung.json")

HEIM_LAT, HEIM_LNG = -34.4626, -57.8400   # Colonia del Sacramento
UY_OFFSET = -3                            # Uruguay: UTC-3, keine Sommerzeit

# OSE: nur diese Orte melden (Großbuchstaben, Teilstring genügt)
OSE_ORTE = ["COLONIA DEL SACRAMENTO", "SACRAMENTO"]
OSE_ORT_GENAU = ["COLONIA"]               # Ortsname exakt "COLONIA"

# ISS: nur gut sichtbare Überflüge melden
ISS_MIN_HOEHE = 30      # Grad über dem Horizont (höchster Punkt)


def hole(url, versuche=3):
    for i in range(versuche):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (privater Abruf)"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except Exception:
            if i == versuche - 1:
                raise
            time.sleep(5)


def sende(titel, text, prio=3, tags="", link=None):
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
        return {"ose": [], "iss_tag": ""}


def speichere_status(st):
    st["ose"] = st["ose"][-200:]
    st["geprueft_am"] = time.strftime("%Y-%m-%d")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ================================================================ OSE (Wasser)
MOTIVE = {
    "MEJORA DE RED": "Verbesserung des Leitungsnetzes",
    "ROTURA": "Rohrbruch",
    "LIMPIEZA DE TANQUES": "Tankreinigung",
    "CONEXIÓN": "Neuer Anschluss",
    "MANTENIMIENTO": "Wartung",
}


def ose_motiv(t):
    t_up = t.upper()
    for k, v in MOTIVE.items():
        if k in t_up:
            return f"{v} ({t.strip().capitalize()})"
    return t.strip().capitalize()


def text_von(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def ose_lesen(art):
    url = f"https://www.ose.com.uy/interrupciones/{art}?field_avisodepartamentonombre_value=COLONIA"
    seite = hole(url).decode("utf-8", errors="replace")
    eintraege = []
    for li in re.findall(r"<li>(.*?)</li>", seite, flags=re.S):
        if "views-field-title" not in li:
            continue
        link = re.search(r'href="(/interrupciones/[^"]+)"', li)
        ort = re.search(r"Localidad:</span>\s*([^<]*)", li)
        dep = re.search(r"Departamento:</span>\s*([^<]*?)\s*-\s*<", li)
        zona = re.search(r"field-zonaafectada.*?field-content\">(.*?)</div>", li, flags=re.S)
        motivo = re.search(r"field-motivo.*?field-content\">(.*?)</div>", li, flags=re.S)
        zeiten = re.findall(r'<time datetime="[^"]*"[^>]*>([^<]*)</time>', li)
        eintraege.append({
            "id": link.group(1) if link else li[:80],
            "link": "https://www.ose.com.uy" + link.group(1) if link else url,
            "dep": text_von(dep.group(1)) if dep else "",
            "ort": text_von(ort.group(1)) if ort else "",
            "zona": text_von(zona.group(1)) if zona else "",
            "motivo": text_von(motivo.group(1)) if motivo else "",
            "von": zeiten[1].strip() if len(zeiten) > 1 else "?",
            "bis": zeiten[2].strip() if len(zeiten) > 2 else "?",
        })
    return eintraege


def ose_vorbei(e):
    """True, wenn die Abstellung schon vorbei ist ("dd/mm/yyyy - HH:MM", Uruguay-Zeit)."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2}):(\d{2})", e["bis"])
    if not m:
        return False
    d, mo, y, h, mi = map(int, m.groups())
    import calendar
    ende_utc = calendar.timegm((y, mo, d, h - UY_OFFSET, mi, 0))
    return ende_utc < time.time()


def ose_betrifft_uns(e):
    ort = e["ort"].upper().strip()
    return ort in OSE_ORT_GENAU or any(o in ort for o in OSE_ORTE)


def ose(st, probe):
    for art, wort in (("programados", "Geplante"), ("imprevistos", "Unvorhergesehene")):
        try:
            liste = ose_lesen(art)
        except Exception as e:
            print(f"OSE {art}: nicht erreichbar ({e})", file=sys.stderr)
            continue
        for e in liste:
            if probe:
                print(f"[OSE {art}] {e['ort']} | {e['von']} – {e['bis']} | {e['zona'][:80]}"
                      f" | betrifft uns: {ose_betrifft_uns(e)} | vorbei: {ose_vorbei(e)}")
                continue
            if not ose_betrifft_uns(e) or ose_vorbei(e) or e["id"] in st["ose"]:
                continue
            titel = f"Wasser: {wort} Abstellung in Colonia"
            text = (f"Von: {e['von']}\nBis: {e['bis']}\n\n"
                    f"Betroffene Zone (Original): {e['zona']}\n\n"
                    f"Grund: {ose_motiv(e['motivo'])}\n\n"
                    "Tipp: Vorher Wasser in Kanister und Töpfe füllen. Danach kann das Wasser "
                    "kurz trüb sein – etwas laufen lassen.")
            if sende(titel, text, prio=4 if art == "imprevistos" else 3, tags="droplet", link=e["link"]):
                st["ose"].append(e["id"])
                print("Gesendet:", titel)


# ================================================================ ISS
def iss(st, probe):
    from skyfield.api import load, wgs84, EarthSatellite
    heute = time.strftime("%Y-%m-%d", time.gmtime(time.time() + UY_OFFSET * 3600))
    if st.get("iss_tag") == heute and not probe:
        return

    tle = hole("https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE").decode().strip().splitlines()
    ts = load.timescale()
    sat = EarthSatellite(tle[1], tle[2], "ISS", ts)
    eph = load("de421.bsp")
    sonne, erde = eph["sun"], eph["earth"]
    ort = wgs84.latlon(HEIM_LAT, HEIM_LNG)

    # Zeitraum: heute 17:00 bis morgen 07:00 Uruguay-Zeit
    t_uy = time.time() + UY_OFFSET * 3600
    tag = time.gmtime(t_uy)
    start = ts.utc(tag.tm_year, tag.tm_mon, tag.tm_mday, 17 - UY_OFFSET)
    ende = ts.utc(tag.tm_year, tag.tm_mon, tag.tm_mday, 31 - UY_OFFSET)

    zeiten, ereignisse = sat.find_events(ort, start, ende, altitude_degrees=10.0)
    ueberfluege, akt = [], {}
    for t, ev in zip(zeiten, ereignisse):
        if ev == 0:
            akt = {"auf": t}
        elif ev == 1 and akt:
            akt["hoch"] = t
        elif ev == 2 and "hoch" in akt:
            akt["ab"] = t
            ueberfluege.append(akt)
            akt = {}

    richtungen = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]

    def richtung(t):
        alt, az, _ = (sat - ort).at(t).altaz()
        return richtungen[int((az.degrees + 22.5) // 45) % 8], alt.degrees

    gute = []
    for u in ueberfluege:
        t = u["hoch"]
        hoehe = richtung(t)[1]
        sonne_alt = (erde + ort).at(t).observe(sonne).apparent().altaz()[0].degrees
        beleuchtet = sat.at(t).is_sunlit(eph)
        if hoehe >= ISS_MIN_HOEHE and sonne_alt < -6 and beleuchtet:
            def uhr(x):
                return time.strftime("%H:%M", time.gmtime(x.utc_datetime().timestamp() + UY_OFFSET * 3600))
            gute.append(f"• {uhr(u['auf'])} Uhr aus {richtung(u['auf'])[0]}, höchster Punkt {uhr(t)} Uhr "
                        f"({hoehe:.0f}° hoch), verschwindet {uhr(u['ab'])} Uhr im {richtung(u['ab'])[0]}")

    if probe:
        print(f"ISS: {len(ueberfluege)} Überflüge, davon gut sichtbar: {len(gute)}")
        print("\n".join(gute))
        return
    if gute:
        sende("ISS diese Nacht sichtbar!",
              "Die Raumstation zieht als heller, ruhiger Lichtpunkt über den Himmel "
              "(blinkt nicht – das unterscheidet sie vom Flugzeug):\n\n" + "\n".join(gute)
              + "\n\n90° wäre genau über euch.",
              prio=3, tags="artificial_satellite")
    st["iss_tag"] = heute


def main():
    probe = "--probe" in sys.argv
    st = lade_status()
    if "--ose" in sys.argv:
        ose(st, probe)
    if "--iss" in sys.argv:
        try:
            iss(st, probe)
        except Exception as e:
            print("ISS-Fehler:", e, file=sys.stderr)
    if not probe:
        speichere_status(st)


if __name__ == "__main__":
    main()
