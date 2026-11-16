# LabelRenamer

Kleines portables Windows-Tool: liest auf Fotos den QR-Code vom Platinen-Label und benennt jede Datei nach der Labelnummer um, z. B. `IMG_4711.jpg` → `F08874ID0109.jpg`.

- Mehrere Bilder werden parallel verarbeitet, große Fotos (40–50 MB) inklusive
- Der Dateiname enthält nur Großbuchstaben und Ziffern
- Formate: JPG, PNG, HEIC, TIFF, BMP, WEBP
- Keine Installation, nur eine `.exe`

## Download

Unter **[Releases](../../releases/latest)** `LabelRenamer.exe` herunterladen.

## Benutzung

- **Doppelklick** auf `LabelRenamer.exe`, dann den Ordner mit den Fotos auswählen
- oder einen **Ordner auf die exe ziehen**
- oder in der Konsole: `LabelRenamer.exe "C:\Pfad\zu\Fotos"`

Optionen:

| Option | Bedeutung |
|---|---|
| `-n` | Testlauf: zeigt nur an, benennt nichts um |
| `-j 4` | Anzahl paralleler Prozesse (Standard: automatisch, max. 6) |

## Verhalten

- Gibt es dasselbe Label mehrfach, heißen die Dateien `F08874ID0109.jpg`, `F08874ID0109_2.jpg`, …
- Wird kein Label gefunden, bleibt die Datei unverändert und steht als `FEHLER` in der Liste
- Bereits richtig benannte Dateien werden übersprungen, das Tool kann also beliebig oft laufen
- Alles wird in `umbenennen_log.txt` im Ordner protokolliert

Erkannt wird der QR-Code auf dem Label (der Data-Matrix-Code mit nur Ziffern wird ignoriert). Das Label muss auf dem Foto scharf und vollständig sichtbar sein.

## Selbst bauen

```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --console --name LabelRenamer --collect-all zxingcpp --collect-all pillow_heif label_renamer.py
```

Die exe wird bei jedem Push auf `main` automatisch über GitHub Actions gebaut, unter Windows getestet und als Release veröffentlicht.
