# LabelRenamer

Kleines portables Windows-Programm: erkennt auf Fotos die Labelnummer der Platine und benennt jede Datei danach um, z. B. `IMG_4711.jpg` → `F08874ID0109.jpg`.

- Erkennung zuerst über den **QR-Code** auf dem Label. Gibt es keinen, wird der **Text auf dem Label** gelesen (eingebaute Windows-Texterkennung). Solche Treffer sind in der Ergebnisliste gelb markiert und sollten kurz geprüft werden.
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

## Verhalten

- Dasselbe Label mehrfach → `F08874ID0109.jpg`, `F08874ID0109_2.jpg`, …
- Kein Label gefunden → Datei bleibt unverändert
- Bereits richtig benannte Dateien werden übersprungen
- „Gesperrt“: Windows hat das Umbenennen verweigert, meist wegen des Überwachten Ordnerzugriffs in Bilder/Dokumente/Desktop. Dann die Fotos in einen anderen Ordner legen.

## Selbst bauen

```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name LabelRenamer --collect-all zxingcpp --collect-all pillow_heif --collect-all winrt label_renamer.py
```

Bei jedem Push auf `main` baut GitHub Actions die exe, testet QR- und Texterkennung unter Windows und veröffentlicht sie als Release.
