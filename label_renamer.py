"""LabelRenamer - benennt Fotos nach der Labelnummer im QR-Code um.

Aufruf:
  LabelRenamer.exe                -> Ordner-Auswahldialog
  LabelRenamer.exe <Ordner>       -> diesen Ordner verarbeiten (auch per Drag & Drop)
  LabelRenamer.exe <Ordner> -n    -> Testlauf, nichts wird umbenannt
"""

import argparse
import multiprocessing
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # HEIC optional
    pass

import zxingcpp

EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"}
# Labelnummer: nur Großbuchstaben und Ziffern, mindestens ein Buchstabe, z. B. F08874ID0109
LABEL_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{6,40}$")
SCALES = (2400, 4000, None)  # längste Kante in px; None = volle Auflösung
Image.MAX_IMAGE_PIXELS = None


def clean(text):
    """Text -> Labelnummer in Großbuchstaben, oder None."""
    t = re.sub(r"\s+", "", text or "").upper()
    return t if LABEL_RE.match(t) else None


def load_gray(path, max_side):
    im = Image.open(path)
    if max_side and im.format == "JPEG":
        # JPEG direkt verkleinert dekodieren: schnell und wenig RAM
        im.draft("L", (max_side, max_side))
    im = im.convert("L")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def find_label(path):
    """Gibt (Labelnummer oder None, Info) zurück."""
    last_error = ""
    for max_side in SCALES:
        try:
            im = load_gray(path, max_side)
        except Exception as e:  # noqa: BLE001
            return None, f"Bild nicht lesbar: {e}"
        for variant in (im, ImageOps.autocontrast(im)):
            try:
                results = zxingcpp.read_barcodes(variant, try_rotate=True, try_downscale=True)
            except Exception as e:  # noqa: BLE001
                last_error = str(e)
                continue
            labels = []
            for r in results:
                lab = clean(r.text)
                if lab:
                    is_qr = "QR" in str(r.format).upper()
                    labels.append((0 if is_qr else 1, lab))
            if labels:
                labels.sort()
                found = sorted({l for _, l in labels})
                info = "" if len(found) == 1 else "mehrere Labels: " + ", ".join(found)
                return labels[0][1], info
    return None, last_error or "kein Label-Code gefunden"


def unique_target(folder, label, ext, src, taken):
    name = f"{label}{ext}"
    n = 2
    while True:
        target = os.path.join(folder, name)
        key = os.path.normcase(os.path.abspath(target))
        if key == os.path.normcase(os.path.abspath(src)) and key not in taken:
            return target
        if not os.path.exists(target) and key not in taken:
            return target
        name = f"{label}_{n}{ext}"
        n += 1


def pause():
    if getattr(sys, "frozen", False):
        try:
            input("\nFertig. Enter drücken zum Schließen ...")
        except EOFError:
            pass


def pick_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Ordner mit Fotos auswählen")
        root.destroy()
        return folder or None
    except Exception:  # noqa: BLE001
        return input("Ordnerpfad eingeben: ").strip().strip('"') or None


def main():
    ap = argparse.ArgumentParser(description="Fotos nach Labelnummer (QR-Code) umbenennen")
    ap.add_argument("folder", nargs="?", help="Ordner mit den Fotos")
    ap.add_argument("-n", "--dry-run", action="store_true", help="nur anzeigen, nichts umbenennen")
    ap.add_argument("-j", "--jobs", type=int, default=0, help="parallele Prozesse (Standard: auto)")
    args = ap.parse_args()

    folder = args.folder or pick_folder()
    if not folder or not os.path.isdir(folder):
        print("Kein gültiger Ordner gewählt.")
        pause()
        return 1

    files = sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in EXTENSIONS and os.path.isfile(os.path.join(folder, f))
    )
    if not files:
        print(f"Keine Bilder in {folder}")
        pause()
        return 1

    # große Bilder brauchen RAM -> höchstens 6 Prozesse
    jobs = args.jobs or max(1, min(6, (os.cpu_count() or 2) - 1, len(files)))
    print(f"{len(files)} Bilder in {folder}, {jobs} parallel{' (Testlauf)' if args.dry_run else ''}\n")

    t0 = time.time()
    results = {}
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(find_label, f): f for f in files}
        for i, fut in enumerate(as_completed(futs), 1):
            f = futs[fut]
            try:
                results[f] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[f] = (None, str(e))
            lab, _ = results[f]
            print(f"[{i}/{len(files)}] {os.path.basename(f)} -> {lab or 'NICHT ERKANNT'}")

    print()
    log = []
    renamed = failed = 0
    taken = set()
    for f in files:  # nacheinander umbenennen, damit Duplikate sauber nummeriert werden
        lab, info = results[f]
        base = os.path.basename(f)
        if not lab:
            failed += 1
            log.append(f"FEHLER\t{base}\t{info}")
            continue
        ext = os.path.splitext(f)[1].lower()
        target = unique_target(folder, lab, ext, f, taken)
        taken.add(os.path.normcase(os.path.abspath(target)))
        new = os.path.basename(target)
        note = f"\t{info}" if info else ""
        if os.path.abspath(target) == os.path.abspath(f):
            log.append(f"OK\t{base}\t(schon richtig){note}")
            continue
        if not args.dry_run:
            try:
                os.rename(f, target)
            except OSError as e:
                failed += 1
                log.append(f"FEHLER\t{base}\t{e}")
                continue
        renamed += 1
        log.append(f"UMBENANNT\t{base}\t-> {new}{note}")

    for line in log:
        print(line.replace("\t", "  "))
    print(f"\n{renamed} umbenannt, {failed} nicht erkannt, {time.time() - t0:.1f} s")

    if not args.dry_run:
        try:
            with open(os.path.join(folder, "umbenennen_log.txt"), "a", encoding="utf-8") as fh:
                fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n" + "\n".join(log) + "\n")
        except OSError:
            pass
    pause()
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
