#!/usr/bin/env python3
"""
Inumet-Warner – holt die amtlichen Wetterwarnungen von Inumet (Uruguay),
übersetzt sie ins Deutsche und schickt eine Push-Nachricht aufs Handy (ntfy).

Keine Oberfläche, nur Python 3 ohne Zusatzpakete.

Aufruf:
  python3 inumet_warner.py            -> prüfen, bei neuer Warnung Push senden
  python3 inumet_warner.py --probe    -> nur anzeigen, nichts senden
  python3 inumet_warner.py --test     -> Test-Push senden (prüft die Handy-Verbindung)

Einstellungen über Umgebungsvariablen (oder direkt unten ändern):
  NTFY_TOPIC   Name deines geheimen Kanals in der ntfy-App (Pflicht zum Senden)
  NTFY_SERVER  Standard: https://ntfy.sh
  NUR_COLONIA  "1" = nur Alarm, wenn Colonia betroffen ist (Standard)
               "0" = alle Warnungen in Uruguay melden (andere leiser)
  DEEPL_KEY    optional: kostenloser DeepL-API-Schlüssel für bessere Übersetzung
  STATE_FILE   Datei, in der gemerkt wird, was schon gemeldet wurde
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- Einstellungen
QUELLE = "https://www.inumet.gub.uy/alerta"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NUR_COLONIA = os.environ.get("NUR_COLONIA", "1") != "0"
DEEPL_KEY = os.environ.get("DEEPL_KEY", "")
STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemeldet.json")
)

# Euer Zuhause: Colonia del Sacramento
HEIM_LAT, HEIM_LNG = -34.4626, -57.8400
HEIM_DEPARTAMENTO = "COLONIA"
HEIM_ORT = "Colonia del Sacramento"
# Alarm auch, wenn das Warngebiet näher als so viele km an Colonia heranreicht
UMKREIS_KM = float(os.environ.get("UMKREIS_KM", "100"))

# ---------------------------------------------------------------- Übersetzungen
STUFE = {1: "Grün", 2: "Gelb", 3: "Orange", 4: "Rot"}
STUFE_EMOJI = {2: "yellow_circle", 3: "orange_circle", 4: "red_circle"}
# ntfy-Priorität: 3 normal, 4 hoch, 5 dringend (durchbricht je nach Handy "Nicht stören")
STUFE_PRIO = {2: 4, 3: 5, 4: 5}

ART = {
    "riesgoViento": "Wind",
    "riesgoLluvia": "Regen",
    "riesgoTormenta": "Gewitter",
    "riesgoVisibilidad": "Sicht/Nebel",
    "riesgoCalor": "Hitze",
    "riesgoFrio": "Kälte",
}

# Standardsätze von Inumet -> Deutsch (wird bei Bedarf ergänzt)
SAETZE = {
    "tormentas puntualmente fuertes": "Örtlich starke Gewitter",
    "tormentas fuertes": "Starke Gewitter",
    "tormentas fuertes y puntualmente severas": "Starke, örtlich schwere Gewitter",
    "tormentas severas": "Schwere Gewitter",
    "tormentas puntualmente fuertes y lluvias abundantes": "Örtlich starke Gewitter und ergiebiger Regen",
    "lluvias abundantes": "Ergiebiger Regen",
    "persistencia de lluvias abundantes": "Anhaltender ergiebiger Regen",
    "vientos fuertes": "Starker Wind",
    "vientos fuertes y persistentes": "Starker, anhaltender Wind",
    "niebla": "Nebel",
    "niebla densa": "Dichter Nebel",
    "temperaturas extremas": "Extreme Temperaturen",
    "ola de calor": "Hitzewelle",
    "ola de frío": "Kältewelle",
    "el área será afectada por tormentas (puntualmente) fuertes.":
        "Das Gebiet wird von (örtlich) starken Gewittern betroffen sein.",
    "el área será afectada por tormentas fuertes.":
        "Das Gebiet wird von starken Gewittern betroffen sein.",
    "podrán estar acompañadas por precipitaciones abundantes en cortos períodos, granizo, "
    "rachas de vientos muy fuertes e intensa actividad eléctrica.":
        "Sie können von starkem Regen in kurzer Zeit, Hagel, sehr starken Sturmböen "
        "und intensiver Blitzaktivität begleitet sein.",
    "las condiciones atmosféricas han mejorado, por lo que se determina el cese de la advertencia vigente.":
        "Die Wetterlage hat sich gebessert, die bestehende Warnung wird aufgehoben.",
}


def _norm(t):
    return re.sub(r"\s+", " ", t.strip().lower())


def deepl(text):
    if not DEEPL_KEY or not text.strip():
        return None
    host = "api-free.deepl.com" if DEEPL_KEY.endswith(":fx") else "api.deepl.com"
    daten = urllib.parse.urlencode(
        {"text": text, "source_lang": "ES", "target_lang": "DE"}
    ).encode()
    req = urllib.request.Request(
        f"https://{host}/v2/translate",
        data=daten,
        headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)["translations"][0]["text"]
    except Exception as e:
        print("DeepL-Fehler:", e, file=sys.stderr)
        return None


def uebersetze(text):
    """Bekannte Standardsätze -> Deutsch; sonst DeepL; sonst Spanisch lassen."""
    if not text:
        return ""
    ganz = SAETZE.get(_norm(text))
    if ganz:
        return ganz
    # Satzweise versuchen
    saetze = [s for s in re.split(r"(?<=\.)\s+|\n+", text.strip()) if s.strip()]
    deutsch = [SAETZE.get(_norm(s)) for s in saetze]
    if all(deutsch):
        return " ".join(deutsch)
    d = deepl(text)
    if d:
        return d
    # Teilweise bekannt: Bekanntes übersetzen, Rest im Original markieren
    return " ".join(de if de else f"[ES] {s.strip()}" for de, s in zip(deutsch, saetze))


# ---------------------------------------------------------------- Inumet lesen
def hole_seite():
    req = urllib.request.Request(QUELLE, headers={"User-Agent": "Mozilla/5.0 (privater Warn-Abruf)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def lese_variable(html, name):
    """Liest 'var name = {...};' aus dem Seitenquelltext."""
    m = re.search(r"\b" + name + r"\s*=\s*", html)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.end():])
        return obj
    except ValueError:
        return None


def im_polygon(lat, lng, punkte):
    drin = False
    j = len(punkte) - 1
    for i in range(len(punkte)):
        yi, xi = punkte[i]["lat"], punkte[i]["lng"]
        yj, xj = punkte[j]["lat"], punkte[j]["lng"]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            drin = not drin
        j = i
    return drin


def abstand_km(lat, lng, punkte):
    """Kürzester Abstand (km) vom Punkt zum Rand eines Polygons (0, wenn drin)."""
    import math
    if im_polygon(lat, lng, punkte):
        return 0.0
    kx = 111.32 * math.cos(math.radians(lat))
    ky = 110.57
    def xy(p):
        return ((p["lng"] - lng) * kx, (p["lat"] - lat) * ky)
    best = float("inf")
    for i in range(len(punkte)):
        (x1, y1), (x2, y2) = xy(punkte[i - 1]), xy(punkte[i])
        dx, dy = x2 - x1, y2 - y1
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def naechster_abstand(adv):
    polys = [p for p in adv.get("coordsPoligonos") or [] if p]
    return min((abstand_km(HEIM_LAT, HEIM_LNG, p) for p in polys), default=None)


def betrifft_uns(adv):
    for z in adv.get("zonasArray") or []:
        if str(z.get("id", "")).upper() == HEIM_DEPARTAMENTO:
            orte = [o.lower() for o in z.get("localidades") or []]
            if not orte or HEIM_ORT.lower() in orte or "colonia" in orte:
                return True
    d = naechster_abstand(adv)
    return d is not None and d <= UMKREIS_KM


def datum(s):
    # "2026-11-18 16:00" -> "18.11. 16:00"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})", s or "")
    return f"{m.group(3)}.{m.group(2)}. {m.group(4)} Uhr" if m else (s or "?")


def baue_meldung(adv, alerta):
    risiken = adv.get("riesgoFenomeno") or {}
    stufe = max([int(v) for v in risiken.values() if v] + [1])
    arten = [ART.get(k, k) for k, v in risiken.items() if v and int(v) > 1]
    uns = betrifft_uns(adv)
    d = naechster_abstand(adv)

    departamentos = ", ".join(z.get("label", z.get("id", "")) for z in adv.get("zonasArray") or [])
    titel = f"Inumet: Warnstufe {STUFE.get(stufe, stufe)} – {', '.join(arten) or 'Wetter'}"
    if uns:
        titel = ("⚠ COLONIA BETROFFEN · " if d == 0 or d is None else "⚠ NAHE COLONIA · ") + titel

    zeilen = [
        uebersetze(adv.get("fenomeno", "")),
        f"Beginn: {datum(adv.get('comienzo'))} · nächste Aktualisierung/Ende: {datum(adv.get('finalizacion'))}",
        f"Wahrscheinlichkeit: {adv.get('probabilidad', '?')}",
        ("Colonia: liegt IM Warngebiet" if d == 0 or (uns and d is None)
         else f"Colonia: Warngebiet ca. {d:.0f} km entfernt" + (" – in der Nähe!" if uns else "")
         if d is not None else "Colonia: nicht betroffen"),
        "",
        uebersetze(adv.get("descripcion", "")),
        "",
        f"Gebiete: {departamentos}",
    ]
    if adv.get("finAct") == "act":
        zeilen.append("(Aktualisierung einer bestehenden Warnung)")
    zeilen.append(f"Stand Inumet: {datum(alerta.get('fechaActualizacion'))}")

    # Kennung: ändert sich, sobald Inumet die Warnung neu herausgibt/ändert
    kennung = "|".join(
        str(x) for x in (alerta.get("pdf"), adv.get("indice"), stufe,
                         adv.get("comienzo"), adv.get("finalizacion"))
    )
    pdf = alerta.get("pdf")
    link = f"https://www.inumet.gub.uy/reportes/riesgo/pdf/{pdf}" if pdf else QUELLE
    return {
        "kennung": kennung,
        "titel": titel,
        "text": "\n".join(zeilen).strip(),
        "stufe": stufe,
        "uns": uns,
        "link": link,
    }


# ---------------------------------------------------------------- Senden
def sende(titel, text, prio=4, tags="warning", link=None):
    if not NTFY_TOPIC:
        print("Kein NTFY_TOPIC gesetzt – nichts gesendet.", file=sys.stderr)
        return False
    headers = {
        "Title": titel.encode("utf-8"),
        "Priority": str(prio),
        "Tags": tags,
    }
    if link:
        headers["Click"] = link
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}", data=text.encode("utf-8"), headers=headers
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200


# ---------------------------------------------------------------- Gedächtnis
def lade_status():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"gemeldet": [], "aktiv": False, "cese": ""}


def speichere_status(st):
    st["gemeldet"] = st["gemeldet"][-200:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- Ablauf
def main():
    probe = "--probe" in sys.argv

    if "--test" in sys.argv:
        ok = sende("Inumet-Warner: Test", "Wenn du das liest, kommt der Alarm an. ✔",
                   prio=4, tags="white_check_mark")
        print("Test gesendet." if ok else "Test fehlgeschlagen.")
        return

    html = hole_seite()
    alerta = lese_variable(html, "alerta")
    cese = lese_variable(html, "cese")
    st = lade_status()

    advs = []
    if alerta and not alerta.get("inactivo") and not alerta.get("modoTest"):
        advs = alerta.get("advertencias") or []

    meldungen = [baue_meldung(a, alerta) for a in advs]
    relevante = [m for m in meldungen if m["uns"] or not NUR_COLONIA]

    if probe:
        print(f"{len(meldungen)} Warnung(en) aktiv, davon Colonia betroffen: "
              f"{sum(m['uns'] for m in meldungen)}\n")
        for m in meldungen:
            print("TITEL:", m["titel"]); print(m["text"]); print("LINK:", m["link"]); print("-" * 60)
        if not meldungen and cese:
            print("Keine Warnung aktiv. Letzte Entwarnung:", datum(cese.get("fechaActualizacion")))
        return

    for m in relevante:
        if m["kennung"] in st["gemeldet"]:
            continue
        prio = STUFE_PRIO.get(m["stufe"], 3) if m["uns"] else 2
        tags = STUFE_EMOJI.get(m["stufe"], "warning")
        if sende(m["titel"], m["text"], prio=prio, tags=tags, link=m["link"]):
            st["gemeldet"].append(m["kennung"])
            print("Gesendet:", m["titel"])

    # Entwarnung, wenn vorher etwas Relevantes aktiv war und jetzt nichts mehr
    jetzt_aktiv = bool(relevante)
    if st.get("aktiv") and not jetzt_aktiv:
        text = uebersetze((cese or {}).get("cese", "").split(":", 1)[-1]) or "Keine Warnung mehr aktiv."
        sende("Inumet: Entwarnung", text, prio=3, tags="green_circle")
        print("Entwarnung gesendet.")
    st["aktiv"] = jetzt_aktiv
    # Datum (einmal pro Tag neu) hält den GitHub-Zeitplan dauerhaft aktiv
    import datetime
    st["geprueft_am"] = datetime.date.today().isoformat()
    speichere_status(st)


if __name__ == "__main__":
    main()
