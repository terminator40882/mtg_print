# MTG Print Studio

Web-Tool zum Erstellen, Kalibrieren und Drucken von Magic-the-Gathering-Proxy-Bögen
auf **DIN A4** (3 × 3 Karten pro Seite) – optimiert für den **Canon PIXMA G650**
mit **Photo Paper Plus Glossy II** und hoher Druckqualität.

- Karten in **Originalgröße 63 × 88 mm**, lückenlos aneinander: **8 Schnitte pro Bogen**
  (4 senkrecht, 4 waagerecht) ergeben 9 fertige Karten.
- Das Padding (Bleed) der MPC-Fill-Bilder wird zwischen den Karten exakt an der
  Schnittlinie abgeschnitten und **nur an den Außenkanten** stehen gelassen – kleine
  Ungenauigkeiten beim äußeren Schnitt erzeugen so keinen weißen Blitzer.
- Druck **treiberlos** (IPP Everywhere / AirPrint) direkt aus der Web-Oberfläche,
  Drucker-IP kommt aus der `.env`.
- Eingebaute **Kalibrierung** (Versatz + Maßstab je Achse) mit Messseite und Probeschnitt.

## Schnellstart

```bash
cp .env.example .env        # PRINTER_IP eintragen (am besten feste IP im Router vergeben)
docker compose up -d --build
```

Danach **http://localhost:8080** öffnen (bzw. `http://<server-ip>:8080`).

Alle Daten (Kartenbilder, Projekte, Kalibrierung, erzeugte PDFs) liegen in `./data`.

## Aufbau

```
docker-compose.yml     # startet alles, baut ./app
.env.example           # Drucker-IP & Druckoptionen
app/
  Dockerfile           # Python + CUPS + cups-filters (PDF -> Raster für den Drucker)
  entrypoint.sh        # startet CUPS und die Web-App
  mtgprint/            # FastAPI-Backend + Web-Oberfläche (static/)
  tests/               # pytest
```

Im Container läuft ein lokales CUPS. Beim Start wird automatisch eine treiberlose
Queue angelegt (`lpadmin -m everywhere` gegen `ipp://PRINTER_IP:631/ipp/print`).
CUPS liest dabei alle Fähigkeiten des Druckers aus (Papiertypen, Qualität, Zufuhr).
Ist der Drucker beim Start aus, versucht die App es im Hintergrund regelmäßig erneut
(oder Button *Drucker → Queue neu einrichten*).

## Konfiguration (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PRINTER_IP` | – | IP des G650 |
| `PRINTER_URI` | – | optional komplette URI, überschreibt `PRINTER_IP` |
| `APP_PORT` | `8080` | Port der Web-Oberfläche |
| `PRINT_MEDIA_TYPE` | `auto` | `auto` wählt Glossy II bzw. „Photographic Glossy“ aus den vom Drucker gemeldeten Typen; alternativ exakter Wert (z.B. `photographic-high-gloss`) |
| `PRINT_QUALITY` | `high` | `high` / `normal` / `draft` |
| `PRINT_INPUT_SLOT` | `auto` | Papierzufuhr, `auto` = Druckerstandard |
| `PRINTER_QUEUE` | `Canon_G650` | Name der CUPS-Queue |

Alle Druckoptionen lassen sich zusätzlich in der Oberfläche unter **Drucker** überschreiben;
dort werden auch die tatsächlich vom Drucker gemeldeten Papiertypen angezeigt.

> **Wichtig:** Gedruckt wird immer auf **A4 (nicht randlos)** und **ohne Skalierung**.
> Randlosdruck vergrößert das Bild beim Canon um einige Prozent und würde die Kartengröße
> verfälschen. Der Rand des Bogens wird ohnehin abgeschnitten.

## Bleed / Padding

MPC-Fill-Bilder folgen der MakePlayingCards-Vorlage: 822 × 1122 px bei 300 DPI =
2,74″ × 3,74″ für eine 2,5″ × 3,5″-Karte → **0,12″ ≈ 3,05 mm Bleed pro Seite**
(umgangssprachlich „1/8 Zoll“ = 0,125″ = 3,175 mm). Das wird pro Achse relativ zur
Bildgröße berechnet, funktioniert also mit jeder Auflösung.

Beim Hochladen wählbar (Standard: MPC):

| Option | Wann |
|---|---|
| MPC-Fill / MPC (0,12″) | Standard für MPC-Fill-Downloads |
| 1/8 Zoll (3,175 mm) | Vorlagen mit exakt 1/8″ |
| Automatisch | Bleed wird aus dem Seitenverhältnis geschätzt |
| Eigene Breite (mm) | beliebig |
| Kein Padding | z.B. Scryfall-Bilder – die App erzeugt dann einen künstlichen Bleed (Randpixel verlängern oder spiegeln) und füllt transparente runde Ecken mit der Randfarbe |

Die Einstellung lässt sich pro Bild nachträglich ändern (Doppelklick auf eine Karte).
Die Bibliothek warnt, wenn das Seitenverhältnis nicht zur gewählten Einstellung passt
oder die Auflösung unter 300 DPI liegt.

## Bedienung – die wichtigsten Abläufe

**Deck drucken (MPC-Fill):** Bilder (Dateien, Ordner oder ZIP) einfach ins Browserfenster
ziehen → „MPC-Fill“ gewählt lassen → „Direkt ins Projekt einfügen“ → **Drucken**.
Optional die MPC-Fill-**XML** importieren (*Schnell-Aktionen*): Reihenfolge und Anzahl
werden übernommen, Bilder werden über die Drive-ID im Dateinamen bzw. den Namen
zugeordnet; fehlende Bilder können auf Wunsch direkt von Google Drive geladen werden.

**Layout anpassen:** Karten links anklicken setzt sie in den markierten Slot (die Markierung
springt weiter → sehr schnelles Befüllen). Drag & Drop zwischen Slots tauscht, mit **Alt**
wird kopiert, Slot auf die Kartenliste ziehen entfernt ihn. Seiten lassen sich verschieben,
duplizieren, leeren, löschen. *Lücken schließen*, *Nach Name sortieren*, *Seite füllen*,
Undo/Redo (Strg+Z / Strg+Y), Tastatursteuerung (Pfeile, Entf, Strg+D, Strg+C/V).

**Einzelne Karten nachdrucken:** Neues Projekt oder „Duplizieren“, nur die benötigten Slots
belegen, beim Drucken „Aktuelle Seite“ oder eine Seitenauswahl wie `1-3,5` angeben.

**Layout-Optionen pro Projekt:** Kartengröße, Spalten/Zeilen, Abstand zwischen Karten
(0 = 8 Schnitte; > 0 = Sicherheitsabstand mit Bleed gefüllt), Außen-Bleed, Raster-Versatz,
Schnittmarken (Länge/Stärke/Abstand, „bis zum Blattrand“ für Schneidemaschinen),
Seitenbeschriftung, Bildqualität.

## Kalibrierung (Reiter „Kalibrierung“)

Jeder Drucker druckt etwas versetzt und minimal skaliert. So gleichst du das aus:

1. **Neues Profil** anlegen (z.B. „Glossy II“) – verschiedene Papiere können sich unterschiedlich verhalten.
2. **Kalibrierseite drucken** (gleiches Papier!).
3. Mit einem Stahllineal von der **linken** Papierkante zur Mitte der Linien **A** und **B**,
   von der **oberen** Kante zu **C** und **D** messen und eintragen → *Berechnen* → *Übernehmen*.
   Die App berechnet Versatz und Maßstab je Achse und korrigiert ab dann jede Seite.
4. Erneut drucken und messen verfeinert das Ergebnis (die Messung wird mit der aktiven
   Korrektur verrechnet). Feinjustage per ±-Buttons ist ebenfalls möglich.
5. **Probeschnitt** drucken: farbige 63 × 88-mm-Felder mit Schnittmarken und einem
   Innenrahmen 3 mm vom Rand. Nach 8 Schnitten muss jede Karte exakt so groß wie eine
   echte Karte sein, der Innenrahmen überall 3 mm Abstand haben und kein Streifen der
   Nachbarfarbe sichtbar sein.

## Entwicklung

```bash
cd app
pip install -r requirements.txt pytest httpx
DATA_DIR=./data-dev uvicorn mtgprint.main:app --reload --port 8080
python -m pytest -q
```

Ohne Docker funktioniert alles außer dem Drucken (dafür braucht es CUPS).

## Fehlersuche

- **„Queue konnte nicht angelegt werden“** – Drucker an? IP korrekt? Im G650-Menü muss
  das WLAN/LAN aktiv sein; IPP läuft auf Port 631. Test aus dem Container:
  `docker compose exec mtg-print sh -c 'ipptool -tv ipp://$PRINTER_IP/ipp/print get-printer-attributes.test'`
- **Karten zu groß/klein** – Kalibrierung durchführen; sicherstellen, dass nicht randlos gedruckt wird.
- **Seite nur teilweise gedruckt (bricht mitten auf dem Blatt ab):** Das PDF ist dann
  vollständig, aber die Übertragung zum Drucker ist abgerissen. Unter *Drucker →
  Druckaufträge* zeigt jeder Auftrag seinen echten Status (z.B. „fehlgeschlagen“,
  „abgebrochen“) samt Meldung des Druckers; *Diagnose-Bericht anzeigen* sammelt Status,
  Optionen, Jobs und CUPS-Log zum Kopieren. Für mehr Details `CUPS_LOG_LEVEL=debug` in
  der `.env` setzen und `docker compose up -d` ausführen.
- **Logs:** `docker compose logs -f`, CUPS-Log: `docker compose exec mtg-print cat /var/log/cups/error_log`
