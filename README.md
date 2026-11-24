# LabelRenamer

Kleines portables Windows-Programm: erkennt auf Fotos die Labelnummer der Platine und benennt jede Datei danach um, z. B. `IMG_4711.jpg` → `F08874ID0109.jpg`.

- Erkennung zuerst über den **QR-Code** auf dem Label. Gibt es keinen, wird der **Text auf dem Label** gelesen (PP-OCR, liest auch Handschrift). Solche Treffer sind in der Ergebnisliste gelb markiert und sollten kurz geprüft werden.
- Mehrere Bilder werden parallel verarbeitet, große Fotos (40–50 MB) inklusive
- Dateinamen nur aus Großbuchstaben und Ziffern
- Formate: JPG, PNG, HEIC, TIFF, BMP, WEBP
- Keine Installation, nur eine `.exe`, keine Logdatei

## Download

Unter **[Releases](../../releases/latest)** `LabelRenamer.exe` herunterladen.

## Benutzung

1. `LabelRenamer.exe` starten (oder einen Ordner auf die exe ziehen)
2. Ordner mit den Fotos wählen
3. **Start** klicken

Das Fenster zeigt den Fortschritt mit Geschwindigkeitskurve, Restzeit und Anzahl erkannter Labels, danach eine Tabelle mit altem und neuem Namen. Mit **Nur Vorschau** wird nichts umbenannt.

## Abgeklebtes Label

Ist das Label abgeklebt und die Nummer nur teilweise mit Marker daraufgeschrieben, unter **Präfix** den festen Anfang eintragen, z. B. `F08482ID00`. Findet das Programm weder QR-Code noch die gedruckte Nummer, liest es die (größte) Ziffernfolge auf dem Bild und hängt sie an: Marker `550` → `F08482ID00550`. Steht auf dem Marker schon ein Teil des Präfixes (z. B. `00550`), wird das erkannt. Marker-Zahlen dürfen 2 oder mehr Ziffern haben; 2-stellige werden nur auf einem Etikett akzeptiert, nicht irgendwo auf der Platine. Solche Treffer sind orange markiert und sollten geprüft werden. Das Präfix wird gespeichert.

## Geteilte Labels und Lernen aus dem Durchgang

- Labels, bei denen Anfang und laufende Nummer getrennt stehen (z. B. `F08482ID0 | 1638`), werden zusammengesetzt, wenn beide Teile auf derselben Zeile liegen – auch ohne Präfix.
- Nach der Analyse vergleicht das Programm alle Fotos eines Durchgangs: Haben die meisten Nummern dieselbe Länge und denselben Anfang, werden Ausreißer durch eine passende zweitbeste Lesung ersetzt und fehlende führende Nullen bei Marker-Zahlen ergänzt (`548` → `F08482ID00548`). Solche Fälle sind markiert: „an die anderen Fotos des Durchgangs angepasst – bitte prüfen“.

## Verhalten

- Dasselbe Label mehrfach → `F08874ID0109.jpg`, `F08874ID0109_2.jpg`, …
- Kein Label gefunden → Datei bleibt unverändert
- Bereits richtig benannte Dateien werden übersprungen
- „Gesperrt“: Windows hat das Umbenennen verweigert, meist wegen des Überwachten Ordnerzugriffs in Bilder/Dokumente/Desktop. Dann die Fotos in einen anderen Ordner legen.

## Selbst bauen

```
pip install -r requirements.txt pyinstaller
pip uninstall -y opencv-python && pip install --no-deps opencv-python-headless
pyinstaller --onefile --windowed --name LabelRenamer --collect-all zxingcpp --collect-all pillow_heif --collect-all sv_ttk --collect-all rapidocr_onnxruntime --collect-binaries onnxruntime label_renamer.py
```

Bei jedem Push auf `main` baut GitHub Actions die exe, testet QR- und Texterkennung unter Windows und veröffentlicht sie als Release.
