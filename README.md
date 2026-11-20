# Outlet46 Monitor

Überwacht den kompletten Katalog von **outlet46.de** (rund 4.400 Artikel) **alle 30 Minuten** und meldet:

| Ereignis | Bedeutung | Push standardmäßig |
|---|---|---|
| 🆕 Neu | Artikel neu im Shop | ✅ |
| 📉 Preis gesenkt | Verkaufspreis gesunken | ✅ |
| ✅ Wieder da | Artikel war weg und ist wieder gelistet (mit Preisvergleich) | ✅ |
| 📈 Preis erhöht | Verkaufspreis gestiegen | – (nur Dashboard) |
| 🗑️ Nicht mehr gelistet | Ausverkauft oder entfernt | – (nur Dashboard) |
| 🏷️ UVP geändert | Streichpreis geändert, Preis gleich | – (nur Dashboard) |

**So funktioniert es:** Eine GitHub Action ruft alle 30 Minuten die 10 Hauptkategorien des Shops ab
(100 Artikel pro Seite, also etwa 95 Seitenaufrufe mit Pausen dazwischen; das ist schonend für den Shop).
Danach vergleicht sie mit dem letzten Stand und speichert die Ergebnisse in `docs/data/`.
GitHub Pages zeigt das Dashboard (`docs/index.html`) an.

Ein Lauf dauert ca. 3 Minuten (bei langsamem Shop bis zu 10). Der Monitor läuft
alle 30 Minuten.

---

## Einrichtung (wie beim SirPlus-Monitor)

1. **Repository anlegen:** Auf GitHub oben rechts „+“ → „New repository“ wählen.
   Als Namen `outlet46-monitor` eintragen, **Public** auswählen und auf „Create repository“ klicken.
2. **Dateien hochladen:** Auf den Link „uploading an existing file“ klicken.
   Im Finder **Cmd + Shift + .** drücken, damit `.github` sichtbar wird.
   Dann `.github`, `docs`, `scraper` und `README.md` ins Browserfenster ziehen und auf „Commit changes“ klicken.
3. **Dashboard einschalten:** Settings → Pages → Branch `main`, Ordner `/docs` → Save.
   Danach ist das Dashboard unter `https://DEINNAME.github.io/outlet46-monitor/` erreichbar.
4. **Push-Benachrichtigung:** Settings → Secrets and variables → Actions → New repository secret anlegen:
   `NTFY_TOPIC` = dein ntfy-Kanal.
   - Derselbe Kanal wie bei SirPlus: Alles landet in einem Kanal. Die Überschrift zeigt, von welchem Shop die Meldung kommt.
   - Ein neuer Kanal (z. B. `outlet46-…`): Du kannst die Meldungen in der ntfy-App getrennt stummschalten.
5. **Starten:** Actions → „Outlet46 Monitor“ → „Run workflow“. Der erste Lauf legt nur den Ausgangsstand an.

### Optionale Einstellungen (Reiter *Variables*)

| Variable | Beispiel | Wirkung |
|---|---|---|
| `MAX_PUSH_PER_RUN` | `5` | Höchstens so viele Einzel-Push-Nachrichten pro Lauf (Standard 10), der Rest kommt gesammelt in einer Nachricht |
| `NOTIFY_TYPES` | `new,price_drop` | Nur diese Ereignisse pushen (Standard: `new,price_drop,back_in_stock`) |
| `MIN_PRICE_CHANGE_PCT` | `10` | Preissenkungen unter 10 % nicht pushen |
| `MIN_DISCOUNT_PCT` | `70` | Neue Artikel nur pushen, wenn sie mindestens 70 % unter UVP liegen |

Bei Outlet46 kommen täglich viele neue Artikel dazu. Wenn dir das zu viele Push-Nachrichten sind,
helfen `MIN_DISCOUNT_PCT` und `MIN_PRICE_CHANGE_PCT`. Im Dashboard erscheint trotzdem alles.

---

**Push-Nachrichten:** Jede Änderung kommt als eigene Nachricht mit Produktbild. Ein Tipp darauf öffnet direkt die Produktseite im Shop. Die wichtigsten Änderungen (größte Preissenkung, höchster Rabatt) kommen zuerst.

## Dashboard
- **Neuigkeiten:** Alle Änderungen chronologisch, filterbar. Was seit deinem letzten Besuch neu ist, ist blau markiert.
- **Neu im Shop:** Artikel, die in den letzten 14 Tagen neu erkannt wurden.
- **Top-Rabatte:** Sortiert nach Ersparnis gegenüber UVP, mit Kategorie-Filter.
- **Günstigste:** Der billigste Artikel steht oben, ebenfalls mit Kategorie-Filter.
- **Alle Produkte:** Sortierbare Tabelle mit Suche nach Artikel, Marke oder Größe.
- Ein Klick auf einen Artikel zeigt verfügbare Größen, Preisverlauf und den Link zum Shop.

## Technische Hinweise
- Der Monitor hält sich an die robots.txt des Shops. Er liest nur normale Kategorieseiten,
  nutzt keine interne Schnittstelle und keine Suche oder Filter.
- **Schutz vor Fehlalarmen:** Der Monitor vergleicht pro Kategorie die geladene Artikelzahl
  mit der Zahl, die der Shop selbst anzeigt. Fehlen über 15 %, bricht er ab, ohne etwas zu melden.
  Dasselbe passiert, wenn insgesamt plötzlich über 20 % fehlen.
- **Flacker-Schutz:** Ein Artikel gilt erst als „nicht mehr gelistet“, wenn er zwei Läufe hintereinander fehlt.
  So lösen kurz reservierte Artikel oder eine verschobene Seitenaufteilung keine falschen „Wieder da“-Meldungen aus.
- Artikel mit 0,00 € (Datenfehler im Shop) werden ignoriert.
- Interne No-Name-Marken des Shops (z. B. „O46BPX (MB1)“) heißen im Dashboard „Outlet46 No-Name“.
- Ändert der Shop sein Design, kann das Auslesen fehlschlagen. Dann bricht der Lauf ab, statt
  Unsinn zu melden, und in GitHub unter „Actions“ erscheint ein rotes ✗. In diesem Fall muss der
  Parser in `scraper/monitor.py` angepasst werden.
