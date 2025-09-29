SDAT/ESL Dashboard (Flask)

Eine kleine Flask‑Webanwendung, die lokale XML‑Dateien einliest und zwei Diagramme darstellt:
- Zählerstände (absolute Stände)
- Verbrauch (Differenzen zwischen Zählerständen)

Was die Anwendung macht
- Liest XML aus lokalen Ordnern ein:
  - `data/` (typischerweise ESL‑Dateien mit absoluten Zählerständen)
  - `SDAT-Files/` (SDAT‑Dateien mit 15‑Minuten‑Verbrauchswerten)
- Ordnet die Daten zwei Zählern zu:
  - `ID742` = Bezug vom Netz
  - `ID735` = Einspeisung ins Netz
- Visualisiert:
  - Zählerstände als Zeitreihe (Linie)
  - Verbrauch pro Zeitraum als Zeitreihe (aus Differenzen der Zählerstände)

Datenquellen und Zuordnung
- ESL (absolute Zählerstände):
  - Pro `<TimePeriod end="...">` wird ein Zeitpunkt verwendet.
  - Aus den `<ValueRow>`‑Einträgen werden Werte nach OBIS‑Codes summiert:
    - Bezug (`ID742`): `1.8.1 + 1.8.2`
    - Einspeisung (`ID735`): `2.8.1 + 2.8.2`
  - Ergebnis sind absolute Stände (i. d. R. kWh).
- SDAT (relative 15‑Minuten‑Werte):
  - `DocumentID` muss die Zähler‑ID enthalten (z. B. `...ID742`/`...ID735`).
  - `StartDateTime` + `Resolution` bestimmen die Zeitpunkte je `Observation` (`Sequence`).
  - Aktuell werden SDAT‑Werte eingelesen und pro Zeitstempel gespeichert; die Diagramme auf der Startseite zeigen jedoch die ESL‑Zählerstände bzw. deren Differenzen. (SDAT‑Kurven können bei Bedarf zusätzlich eingeblendet werden.)

Duplikate und Sortierung
- Falls derselbe Zeitstempel mehrfach vorkommt, gewinnt der zuletzt gelesene Eintrag.
- Alle Zeitreihen werden chronologisch sortiert.

Was die beiden Diagramme aussagen
- Zählerstandsdiagramm (oben):
  - Y‑Achse: absoluter Zählerstand (kumulierte Energie, typischerweise kWh).
  - X‑Achse: Zeit.
  - Steigt die Linie, hat sich der Zählerstand erhöht. Je steiler, desto mehr Energie seit dem letzten Punkt.
- Verbrauchsdiagramm (unten):
  - Y‑Achse: Verbrauch pro Zeitraum (kWh), berechnet als Differenz zweier aufeinanderfolgender Zählerstände.
  - X‑Achse: Zeit (Zeitpunkt des „Folge‑Werts“).
  - Negative oder ungültige Differenzen werden als Lücken behandelt.

Projektstruktur
- `app.py`: Flask‑App, XML‑Parsing (ESL/SDAT), Datenaufbereitung, Endpoints
- `templates/index.html`: Seite mit zwei Chart.js‑Diagrammen
- `static/`: einfache Styles
- `data/`: lokale ESL/SDAT‑XMLs (rekursiv eingelesen)
- `SDAT-Files/`: optionale SDAT‑XMLs (rekursiv eingelesen)

Endpoints
- `GET /` – Dashboard mit zwei Diagrammen (Zählerstand oben, Verbrauch unten)
- `GET /api/data` – JSON (Zählerstands‑Zeitreihe)
- `GET /consumption` – Seite mit Verbrauchsdiagramm (gleiches Layout, anderer Titel/Achse)
- `GET /api/consumption` – JSON (Verbrauchs‑Zeitreihe aus ESL‑Differenzen)

Voraussetzungen
- Python 3.10+
- Abhängigkeiten: Flask (Chart.js wird via CDN geladen)

Installation (Windows PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install flask
```

Starten
```powershell
python app.py
```
Dann im Browser `http://127.0.0.1:5000/` öffnen.

Hinweise und Grenzen
- Der Verbrauch wird zurzeit aus ESL‑Differenzen berechnet. Für feinere Auflösung (15‑Minuten‑Takt) können die SDAT‑Werte zusätzlich visualisiert werden.
- Die Zeitachse nutzt, wenn verfügbar, den Chart.js‑Zeitskalen‑Adapter; andernfalls fällt die Seite auf eine Kategorien‑Achse zurück.
- Achte bei eigenen ESL/SDAT‑Dateien auf die genannten Strukturen (ESL: `TimePeriod`/`ValueRow`, SDAT: `DocumentID`, `Interval`, `Resolution`, `Observation`).

