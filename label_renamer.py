"""LabelRenamer - benennt Fotos nach der Labelnummer um.

Erkennung: zuerst QR-Code auf dem Label, sonst Texterkennung (PP-OCR über RapidOCR).
Start:  Doppelklick -> Fenster, Ordner wählen, Start.
        Ordner auf die exe ziehen -> Ordner ist vorausgewählt.
Test:   LabelRenamer.exe <Ordner> --headless [--report datei.txt]
"""

import argparse
import multiprocessing
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from PIL import Image, ImageFilter, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # HEIC optional
    pass

import zxingcpp

APP = "LabelRenamer"
EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"}
# Labelnummer: nur Großbuchstaben und Ziffern, mindestens ein Buchstabe, z. B. F08874ID0109
LABEL_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{6,40}$")
# typisches Format der Labels: 1 Buchstabe, 5 Ziffern, 2 Buchstaben, 4 Ziffern
SCALES = (2400, 4000, None)  # längste Kante in px; None = volle Auflösung
OCR_HINT = "per Text erkannt (kein QR-Code) – bitte prüfen"
PREFIX_HINT = "Präfix + handschriftliche Ziffern – bitte prüfen"
PREFIX_RE = re.compile(r"^[A-Z0-9]{1,30}$")
Image.MAX_IMAGE_PIXELS = None


# --------------------------------------------------------------------------- QR-Code

def clean(text):
    """Text -> Labelnummer in Großbuchstaben, oder None."""
    t = re.sub(r"\s+", "", text or "").upper()
    return t if LABEL_RE.match(t) else None


def load_gray(path, max_side):
    im = Image.open(path)
    if max_side and im.format == "JPEG":
        im.draft("L", (max_side, max_side))  # JPEG verkleinert dekodieren: schnell, wenig RAM
    im = im.convert("L")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def find_qr(path):
    last_error = ""
    for max_side in SCALES:
        try:
            im = load_gray(path, max_side)
        except Exception as e:  # noqa: BLE001
            return None, f"Bild nicht lesbar: {e}", None
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
                    labels.append((0 if "QR" in str(r.format).upper() else 1, lab))
            if labels:
                labels.sort()
                found = sorted({l for _, l in labels})
                info = "" if len(found) == 1 else "mehrere Labels: " + ", ".join(found)
                return labels[0][1], info, None
    return None, last_error, True


# --------------------------------------------------------------------------- Text (OCR)

TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "Z": "2", "S": "5", "B": "8",
            "G": "6", "T": "7"}
TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}
# handschriftliche Kleinbuchstaben, die eigentlich Ziffern sind
HAND = {"g": "9", "q": "9", "b": "6", "o": "0", "l": "1", "s": "5", "z": "2", "i": "1"}
FORMATS = ("LDDDDDLLDDDD", "LDDDDDLLDDDDD")  # z. B. F08874ID0109, F08482ID00540
OCR_MAX_SIDE = 2400
_ENGINE = None


def to_kind(ch, kind):
    if kind == "D":
        return ch if ch.isdigit() else TO_DIGIT.get(ch)
    return ch if ch.isalpha() else TO_LETTER.get(ch)


def fit_format(tok):
    """Korrigiert typische OCR-Verwechslungen (O/0, I/1 …) passend zum Labelformat."""
    for fmt in FORMATS:
        if len(tok) == len(fmt):
            out = [to_kind(c, k) for c, k in zip(tok, fmt)]
            if all(out):
                return "".join(out)
    return None


def digits_only(s):
    out = "".join(c if c.isdigit() else TO_DIGIT.get(c, "?") for c in s)
    return None if "?" in out else out


def fit_prefix(tok, prefix):
    """Ganze Nummer mit (leicht verlesenem) Präfix: FO8482IDOO540 -> F08482ID00540."""
    n = len(prefix)
    if len(tok) <= n or n < 4:
        return None
    head = [to_kind(c, "D" if p.isdigit() else "L") for c, p in zip(tok[:n], prefix)]
    if sum(h != p for h, p in zip(head, prefix)) > 1:
        return None
    rest = digits_only(tok[n:])
    return prefix + rest if rest and len(rest) <= 6 else None


def join_prefix(prefix, tok):
    """Präfix + erkannte Ziffern. Überlappung wird erkannt: F08482ID00 + 00550 -> F08482ID00550."""
    if tok.startswith(prefix):
        rest = tok[len(prefix):]
    else:
        rest = tok
        for k in range(min(len(prefix), len(tok)), 0, -1):
            if prefix.endswith(tok[:k]):
                rest = tok[k:]
                break
    rest = digits_only(rest)
    if not rest or len(rest) > 6:
        return None
    return prefix + rest


MIN_MARKER_DIGITS = 3  # so viele Ziffern muss die Marker-Zahl mindestens haben (z. B. 548, 00539)


def candidates(text, conf, prefix="", marker_ok=True):
    """Mögliche Labelnummern in einem OCR-Textstück, mit Gewicht. Gibt {Label: (Gewicht, Art)}."""
    out = {}

    def add(label, weight, kind):
        if label and weight > out.get(label, (0, ""))[0]:
            out[label] = (weight, kind)

    raw = text.strip()
    short = re.sub(r"[\s.,'`´·_~-]", "", raw)
    if short and len(short) <= 8 and sum(c.isdigit() for c in short) >= len(short) / 2:
        short = "".join(HAND.get(c, c) for c in short)  # handschriftlich "0053g" -> "00539"
    variants = {raw.upper(), raw.upper().replace(" ", ""), short.upper()}
    for chunk in variants:
        for tok in re.findall(r"[A-Z0-9|]+", chunk.replace("|", "1")):
            fixed = fit_format(tok)
            if fixed:
                add(fixed, conf * (2.0 if fixed == tok else 1.6), "text")
            if prefix:
                add(fit_prefix(tok, prefix), conf * 1.8, "text")
                if marker_ok and MIN_MARKER_DIGITS <= len(tok) <= len(prefix) + 6:
                    lab = join_prefix(prefix, tok)
                    if lab:
                        pure = tok.isdigit() or tok.startswith(prefix) or any(
                            prefix.endswith(tok[:k]) and tok[k:].isdigit() for k in range(1, len(tok)))
                        add(lab, conf * (1.0 if pure else 0.4), "marker")
            elif not fixed and 8 <= len(tok) <= 20:
                d = sum(c.isdigit() for c in tok)
                if d >= 3 and len(tok) - d >= 1:
                    add(tok, conf * 0.5, "text")
    return out


def ocr_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


def run_ocr(img):
    """Texterkennung (PP-OCR über RapidOCR). Gibt [(Text, Konfidenz)] zurück."""
    import numpy as np

    arr = np.ascontiguousarray(np.asarray(img.convert("RGB"))[:, :, ::-1])  # BGR
    res, _ = ocr_engine()(arr)
    return [(r[1], float(r[2])) for r in (res or [])]


def find_stickers(gray, max_count=4, rgb=None):
    """Findet helle, rechteckige, unbunte Flächen (Etiketten/Klebeband). Gibt Boxen (x0, y0, x1, y1)
    relativ zur Bildgröße (0..1) zurück, größte zuerst."""
    import numpy as np

    sat = None
    if rgb is not None:  # Sättigung: gelbe Pads, grüne Platine usw. aussortieren
        hsv = rgb.copy()
        hsv.thumbnail((320, 320))
        sat = np.asarray(hsv.convert("HSV"), dtype=np.float32)[:, :, 1]
    small = gray.copy()
    small.thumbnail((320, 320))
    small = small.filter(ImageFilter.GaussianBlur(2))
    a = np.asarray(small, dtype=np.float32)
    h, w = a.shape
    thr = max(np.percentile(a, 75), a.mean() + 0.6 * a.std())
    mask = a >= thr
    seen = np.zeros_like(mask)
    boxes = []
    for y0 in range(h):
        for x0 in range(w):
            if not mask[y0, x0] or seen[y0, x0]:
                continue
            stack = [(y0, x0)]
            seen[y0, x0] = True
            ys, xs = [], []
            while stack:
                y, x = stack.pop()
                ys.append(y)
                xs.append(x)
                for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            area = len(ys)
            bx0, bx1, by0, by1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
            bw, bh = bx1 - bx0, by1 - by0
            if area < 0.004 * w * h or area > 0.45 * w * h:
                continue
            if bx0 == 0 or by0 == 0 or bx1 == w or by1 == h:
                continue  # berührt den Bildrand -> eher Hintergrund
            if area / (bw * bh) < 0.7 or max(bw, bh) / min(bw, bh) > 8:
                continue  # nicht rechteckig genug
            if sat is not None and sat.shape == mask.shape and np.median(sat[ys, xs]) > 70:
                continue  # zu bunt für ein Etikett
            boxes.append((area, (bx0 / w, by0 / h, bx1 / w, by1 / h)))
    boxes.sort(reverse=True)
    return [b for _, b in boxes[:max_count]]


def crop_box(img, box, margin=0.03):
    W, H = img.size
    x0, y0, x1, y1 = box
    mx, my = (x1 - x0) * margin, (y1 - y0) * margin
    return img.crop((int(max(0, x0 - mx) * W), int(max(0, y0 - my) * H),
                     int(min(1, x1 + mx) * W), int(min(1, y1 + my) * H)))


def fit_size(img, target=640):
    s = target / max(img.size)
    if s > 1 or max(img.size) > 1600:
        s = min(s, 1600 / max(img.size)) if s <= 1 else s
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    return img


def enhance_crop(base, box):
    """Schneidet ein Etikett aus, entfernt Raster/Rauschen und verstärkt blasse Schrift."""
    import numpy as np

    crop = crop_box(base, box)
    crop = crop.filter(ImageFilter.GaussianBlur(max(1.0, max(crop.size) / 350)))  # Moiré weg
    a = np.asarray(crop, dtype=np.float32)
    bg = np.asarray(crop.filter(ImageFilter.GaussianBlur(max(crop.size) / 15)), dtype=np.float32)
    norm = a / np.maximum(bg, 1.0)  # ungleichmäßige Helligkeit ausgleichen
    lo, hi = np.percentile(norm, 0.5), np.percentile(norm, 50)
    norm = np.clip((norm - lo) / max(hi - lo, 1e-3), 0, 1) ** 1.6  # blasse Striche abdunkeln
    return fit_size(Image.fromarray((norm * 255).astype(np.uint8)))


def sticker_variants(rgb, gray):
    """Ausschnitte aller gefundenen Etiketten: Blaukanal (farbiger Marker) und entrastert/verstärkt."""
    for i, box in enumerate(find_stickers(gray, rgb=rgb)):
        blue = ImageOps.autocontrast(crop_box(rgb, box).split()[2], cutoff=1)
        yield f"etikett{i + 1}/blau", fit_size(blue)
        yield f"etikett{i + 1}/verstärkt", enhance_crop(gray, box)


def find_text(path, prefix="", ocr=None, debug=False):
    ocr = ocr or run_ocr
    try:
        rgb = Image.open(path)
        if rgb.format == "JPEG":
            rgb.draft("RGB", (OCR_MAX_SIDE, OCR_MAX_SIDE))
        rgb = rgb.convert("RGB")
        rgb.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), Image.LANCZOS)
        gray = rgb.convert("L")
    except Exception as e:  # noqa: BLE001
        return None, f"Bild nicht lesbar: {e}"

    votes, kinds, seen = {}, {}, []

    def read(name, img):
        items = ocr(img)
        if debug and items:
            seen.append(f"{name}: " + " | ".join(t for t, _ in items))
        # Marker-Zahlen nur, wenn das Textstück allein steht (nicht "S3(2.4mm)" o. ä.)
        for text, conf in items:
            alone = bool(re.fullmatch(r"[\sA-Za-z0-9.,'`´·_~|-]{2,16}", text.strip())) and not re.search(
                r"[()（）]|MM|mm", text)
            for label, (weight, kind) in candidates(text, conf, prefix, marker_ok=alone).items():
                votes[label] = votes.get(label, 0) + weight
                kinds[label] = kind

    def best():
        if not votes:
            return None, 0
        # Nummern im bekannten Format (12/13 Zeichen) bevorzugen: "005411" -> zu lang -> verworfen
        scored = {k: v * (1.5 if fit_format(k) == k else 0.5) for k, v in votes.items()}
        label = max(scored, key=scored.get)
        return label, scored[label]

    try:
        read("ganz", rgb)
        label, score = best()
        if not (label and kinds[label] == "text" and score >= 2.4):
            for name, img in sticker_variants(rgb, gray):  # Etiketten einzeln, aufbereitet
                read(name, img)
            if not votes:
                read("ganz/90°", rgb.rotate(90, expand=True))
        label, score = best()
    except Exception as e:  # noqa: BLE001
        return None, f"Texterkennung fehlgeschlagen: {e}"
    note = (" [" + " || ".join(seen) + f" → Stimmen: {dict(sorted(votes.items(), key=lambda kv: -kv[1])[:4])}]"
            if debug else "")
    if label and score >= 0.4:
        return label, (PREFIX_HINT if kinds[label] == "marker" else OCR_HINT) + note
    msg = "weder QR-Code, Labeltext noch Marker-Ziffern gefunden" if prefix else "weder QR-Code noch Labeltext gefunden"
    return None, msg + note


def find_label(path, prefix="", debug=False):
    """Gibt (Labelnummer oder None, Info) zurück. Läuft in einem eigenen Prozess."""
    label, info, try_text = find_qr(path)
    if label or not try_text:
        return label, info
    return find_text(path, prefix, debug=debug)


# --------------------------------------------------------------------------- Umbenennen

def list_images(folder):
    return sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in EXTENSIONS and os.path.isfile(os.path.join(folder, f))
    )


def default_jobs(n_files):
    # große Bilder + Texterkennung brauchen RAM -> höchstens 4 Prozesse
    return max(1, min(4, (os.cpu_count() or 2) - 1, n_files))


def unique_target(folder, label, ext, src, taken):
    name = f"{label}{ext}"
    n = 2
    while True:
        target = os.path.join(folder, name)
        key = os.path.normcase(os.path.abspath(target))
        is_self = key == os.path.normcase(os.path.abspath(src))
        if key not in taken and (is_self or not os.path.exists(target)):
            return target
        name = f"{label}_{n}{ext}"
        n += 1


def rename_with_retry(src, dst):
    """Kurz erneut versuchen, falls Virenscanner/OneDrive die Datei gerade sperrt."""
    for attempt in range(4):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.5 * (attempt + 1))


RENAMED, PLANNED, UNCHANGED, NOT_FOUND, DENIED = "renamed", "planned", "unchanged", "notfound", "denied"


def run_job(folder, dry_run, emit, cancel_event, jobs=0, prefix="", debug=False):
    """Analysiert und benennt um. emit(event, **daten) meldet den Fortschritt."""
    files = list_images(folder)
    emit("start", total=len(files))
    if not files:
        emit("done", cancelled=False)
        return
    jobs = jobs or default_jobs(len(files))
    results = {}
    ex = ProcessPoolExecutor(max_workers=jobs)
    try:
        futs = {ex.submit(find_label, f, prefix, debug): f for f in files}
        for fut in as_completed(futs):
            if cancel_event.is_set():
                break
            f = futs[fut]
            try:
                results[f] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[f] = (None, str(e))
            emit("analyzed", file=f, label=results[f][0], done=len(results),
                 nbytes=os.path.getsize(f) if os.path.exists(f) else 0)
    finally:
        ex.shutdown(wait=not cancel_event.is_set(), cancel_futures=True)

    if cancel_event.is_set():
        emit("done", cancelled=True)
        return

    emit("phase")
    taken = set()
    for f in files:  # nacheinander, damit Duplikate sauber nummeriert werden
        lab, info = results[f]
        base = os.path.basename(f)
        if not lab:
            emit("result", old=base, new="", status=NOT_FOUND, info=info)
            continue
        target = unique_target(folder, lab, os.path.splitext(f)[1].lower(), f, taken)
        key = os.path.normcase(os.path.abspath(target))
        taken.add(key)
        new = os.path.basename(target)
        if key == os.path.normcase(os.path.abspath(f)):
            emit("result", old=base, new=new, status=UNCHANGED, info=info or "bereits richtig benannt")
        elif dry_run:
            emit("result", old=base, new=new, status=PLANNED, info=info)
        else:
            try:
                rename_with_retry(f, target)
                emit("result", old=base, new=new, status=RENAMED, info=info)
            except PermissionError:
                emit("result", old=base, new=new, status=DENIED,
                     info="Zugriff verweigert (Ordner geschützt oder Datei geöffnet)")
            except OSError as e:
                emit("result", old=base, new=new, status=DENIED, info=str(e))
    emit("done", cancelled=False)


# --------------------------------------------------------------------------- Einstellungen

def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, APP, "settings.json")


def load_settings():
    import json

    try:
        with open(settings_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    import json

    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


# --------------------------------------------------------------------------- Oberfläche

def fmt_time(s):
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


def fmt_num(x, digits=1):
    return f"{x:.{digits}f}".replace(".", ",")


def fmt_mb(b):
    return f"{b / 1e6:,.0f} MB".replace(",", ".")


class App:
    STATUS_TEXT = {
        RENAMED: "✔ Umbenannt",
        PLANNED: "➜ Vorschau",
        UNCHANGED: "• Unverändert",
        NOT_FOUND: "✖ Kein Label",
        DENIED: "⚠ Gesperrt",
    }
    ACCENT, ACCENT_FILL = "#57c8ff", "#173847"
    BG, CARD, MUTED, GRID = "#1c1c1c", "#202020", "#9a9a9a", "#2e2e2e"

    def __init__(self, root, folder=None):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.queue = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.dry = False

        root.title(APP)
        root.minsize(800, 720)
        root.geometry("960x820")
        try:
            import sv_ttk

            sv_ttk.set_theme("dark")
        except Exception:  # noqa: BLE001
            ttk.Style().theme_use("clam")
        style = ttk.Style()
        root.configure(background=self.BG)
        font = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
        self.font = font
        style.configure("Title.TLabel", font=(font, 18, "bold"))
        style.configure("Big.TLabel", font=(font, 11))
        style.configure("Muted.TLabel", foreground=self.MUTED)
        style.configure("Value.TLabel", font=(font, 12, "bold"))
        style.configure("Accent.TButton", font=(font, 10, "bold"), padding=(26, 7))
        style.configure("Stop.TButton", font=(font, 10, "bold"), padding=(26, 7))
        style.configure("Treeview", rowheight=28)
        style.configure("Treeview.Heading", font=(font, 9, "bold"))

        main = ttk.Frame(root, padding=20)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Fotos nach Labelnummer umbenennen", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, style="Muted.TLabel",
                  text="Liest den QR-Code oder den Text auf dem Label und benennt jedes Foto danach um, "
                       "z. B. F08874ID0109.jpg").pack(anchor="w", pady=(2, 12))

        # 1. Ordner
        box = ttk.LabelFrame(main, text=" 1. Ordner auswählen ", padding=10)
        box.pack(fill="x")
        self.folder_var = tk.StringVar(value=folder or "")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.entry = ttk.Entry(row, textvariable=self.folder_var, font=(font, 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.browse_btn = ttk.Button(row, text="Durchsuchen …", command=self.browse)
        self.browse_btn.pack(side="left", padx=(8, 0))
        self.folder_info = ttk.Label(box, style="Muted.TLabel")
        self.folder_info.pack(anchor="w", pady=(6, 0))
        self.folder_var.trace_add("write", lambda *_: self.update_folder_info())

        # Einstellung: abgeklebtes Label
        opt = ttk.LabelFrame(main, text=" Einstellung: abgeklebtes Label (optional) ", padding=10)
        opt.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(opt)
        row.pack(fill="x")
        ttk.Label(row, text="Präfix:").pack(side="left")
        self.prefix_var = tk.StringVar(value=load_settings().get("prefix", ""))
        self.prefix_entry = ttk.Entry(row, textvariable=self.prefix_var, width=22, font=(font, 10))
        self.prefix_entry.pack(side="left", padx=(8, 12), ipady=2)
        self.prefix_info = ttk.Label(row, style="Muted.TLabel")
        self.prefix_info.pack(side="left", fill="x", expand=True)
        self.prefix_var.trace_add("write", lambda *_: self.on_prefix_change())
        self.on_prefix_change()

        row = ttk.Frame(main)
        row.pack(fill="x", pady=12)
        self.dry_var = tk.BooleanVar(value=False)
        self.dry_chk = ttk.Checkbutton(row, text="Nur Vorschau (nichts wird umbenannt)", variable=self.dry_var)
        self.dry_chk.pack(side="left")
        self.start_btn = ttk.Button(row, text="Start", style="Accent.TButton", command=self.start_or_cancel)
        self.start_btn.pack(side="right")

        # 2. Fortschritt
        prog = ttk.LabelFrame(main, text=" 2. Fortschritt ", padding=10)
        prog.pack(fill="x")
        self.prog_title = ttk.Label(prog, text="Bereit", style="Big.TLabel")
        self.prog_title.pack(anchor="w")
        self.pbar = ttk.Progressbar(prog, mode="determinate", maximum=1)
        self.pbar.pack(fill="x", pady=6)
        self.graph = tk.Canvas(prog, height=100, background=self.BG,
                               highlightthickness=1, highlightbackground=self.GRID)
        self.graph.pack(fill="x")
        self.graph.bind("<Configure>", lambda e: self.draw_graph())
        stats = ttk.Frame(prog)
        stats.pack(fill="x", pady=(8, 0))
        self.stat_vars = {}
        for i, (key, title) in enumerate((("speed", "Geschwindigkeit"), ("eta", "Restzeit"),
                                          ("found", "Erkannt"), ("missing", "Nicht erkannt"))):
            f = ttk.Frame(stats)
            f.grid(row=0, column=i, sticky="w", padx=(0, 36))
            ttk.Label(f, text=title, style="Muted.TLabel").pack(anchor="w")
            v = tk.StringVar(value="–")
            ttk.Label(f, textvariable=v, style="Value.TLabel").pack(anchor="w")
            self.stat_vars[key] = v

        # 3. Ergebnis
        res = ttk.LabelFrame(main, text=" 3. Ergebnis ", padding=10)
        res.pack(fill="both", expand=True, pady=(12, 0))
        tv_frame = ttk.Frame(res)
        tv_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tv_frame, columns=("status", "old", "new", "info"), show="headings", height=5)
        for c, title, w, stretch in (("status", "Status", 120, False), ("old", "Alter Name", 210, True),
                                     ("new", "Neuer Name", 190, True), ("info", "Hinweis", 260, True)):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, stretch=stretch, anchor="w")
        sb = ttk.Scrollbar(tv_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in ((RENAMED, "#6ccb5f"), (PLANNED, "#57c8ff"), (UNCHANGED, "#9a9a9a"),
                           (NOT_FOUND, "#ff99a4"), (DENIED, "#fce100"), ("ocr", "#ffb86b")):
            self.tree.tag_configure(tag, foreground=color)

        bottom = ttk.Frame(res)
        bottom.pack(fill="x", pady=(8, 0))
        self.summary = ttk.Label(bottom, style="Big.TLabel")
        self.summary.pack(side="left")
        self.open_btn = ttk.Button(bottom, text="Ordner öffnen", command=self.open_folder, state="disabled")
        self.open_btn.pack(side="right")

        self.update_folder_info()
        self.reset_progress()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)

    # ---- Ordner
    def folder(self):
        return self.folder_var.get().strip().strip('"')

    def browse(self):
        from tkinter import filedialog

        start = self.folder() if os.path.isdir(self.folder()) else os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="Ordner mit Fotos auswählen", initialdir=start)
        if chosen:
            self.folder_var.set(os.path.normpath(chosen))

    def update_folder_info(self):
        folder = self.folder()
        if not folder:
            text = "Noch kein Ordner gewählt."
        elif not os.path.isdir(folder):
            text = "Ordner nicht gefunden."
        else:
            try:
                files = list_images(folder)
                text = f"{len(files)} Bilder gefunden ({fmt_mb(sum(os.path.getsize(f) for f in files))})"
            except OSError as e:
                text = f"Ordner nicht lesbar: {e}"
        self.folder_info.configure(text=text)

    def open_folder(self):
        if os.path.isdir(self.folder()) and hasattr(os, "startfile"):
            os.startfile(self.folder())  # noqa: S606

    # ---- Ablauf
    def start_or_cancel(self):
        if self.running:
            self.cancel.set()
            self.start_btn.configure(state="disabled", text="Wird abgebrochen …")
            return
        from tkinter import messagebox

        folder = self.folder()
        if not os.path.isdir(folder):
            messagebox.showwarning(APP, "Bitte zuerst einen gültigen Ordner auswählen.")
            return
        self.tree.delete(*self.tree.get_children())
        self.reset_progress()
        self.cancel.clear()
        self.running = True
        self.dry = self.dry_var.get()
        prefix = self.prefix()
        if self.prefix_var.get().strip() and not prefix:
            self.running = False
            messagebox.showwarning(APP, "Das Präfix darf nur Buchstaben und Ziffern enthalten.")
            return
        save_settings({"prefix": prefix})
        for w in (self.entry, self.browse_btn, self.dry_chk, self.open_btn, self.prefix_entry):
            w.configure(state="disabled")
        self.start_btn.configure(text="Abbrechen", style="Stop.TButton")
        self.prog_title.configure(text="Bilder werden gesucht …")

        def emit(event, **d):
            self.queue.put((event, d))

        threading.Thread(target=run_job, args=(folder, self.dry, emit, self.cancel, 0, prefix), daemon=True).start()

    def prefix(self):
        p = re.sub(r"\s+", "", self.prefix_var.get()).upper()
        return p if PREFIX_RE.match(p) else ""

    def on_prefix_change(self):
        raw = self.prefix_var.get()
        up = raw.upper().replace(" ", "")
        if raw != up:  # nur Großbuchstaben (Tk ruft den Trace dabei nicht erneut auf)
            self.prefix_var.set(up)
        p = self.prefix()
        if not up:
            text = "leer = aus. Bei abgeklebtem Label mit Marker-Nummer z. B. F08482ID00 eintragen."
        elif not p:
            text = "Nur Buchstaben und Ziffern erlaubt."
        else:
            text = f"Marker „550“ → {p}550 · Marker „00550“ → {join_prefix(p, '00550') or p + '00550'}"
        self.prefix_info.configure(text=text)

    def reset_progress(self):
        self.total = self.done = self.found = self.missing = 0
        self.t_start = None
        self.samples = []  # (Sekunden seit Start, Bilder/s)
        self.events = []  # (Zeitpunkt, Bytes) fertig analysierter Bilder
        self.counts = {k: 0 for k in self.STATUS_TEXT}
        self.pbar.configure(value=0, maximum=1)
        for v in self.stat_vars.values():
            v.set("–")
        self.summary.configure(text="")
        self.draw_graph()

    def poll(self):
        try:
            while True:
                event, d = self.queue.get_nowait()
                getattr(self, "on_" + event)(**d)
        except queue.Empty:
            pass
        if self.running and self.t_start and self.done < self.total:
            self.sample()
        self.root.after(250, self.poll)

    def on_start(self, total):
        self.total = total
        self.t_start = time.time()
        self.pbar.configure(maximum=max(total, 1))
        self.prog_title.configure(text=f"Analysiere 0 von {total} Bildern" if total else "Keine Bilder in diesem Ordner.")

    def on_analyzed(self, file, label, done, nbytes):
        self.done = done
        self.events.append((time.time(), nbytes))
        if label:
            self.found += 1
        else:
            self.missing += 1
        self.pbar.configure(value=done)
        self.prog_title.configure(
            text=f"Analysiere {done} von {self.total} Bildern – {100 * done / max(self.total, 1):.0f} %")
        self.stat_vars["found"].set(str(self.found))
        self.stat_vars["missing"].set(str(self.missing))

    def sample(self):
        now = time.time()
        elapsed = now - self.t_start
        window = 3.0
        recent = [b for t, b in self.events if now - t <= window]
        span = min(window, max(elapsed, 0.5))
        rate = len(recent) / span
        self.samples.append((elapsed, rate))
        self.stat_vars["speed"].set(f"{fmt_num(rate)} Bilder/s · {fmt_num(sum(recent) / span / 1e6, 0)} MB/s")
        if self.done:
            self.stat_vars["eta"].set("ca. " + fmt_time((self.total - self.done) * elapsed / self.done))
        else:
            self.stat_vars["eta"].set("wird berechnet …")
        self.draw_graph()

    def draw_graph(self):
        c = self.graph
        c.delete("all")
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        for i in range(1, 4):
            c.create_line(0, h * i / 4, w, h * i / 4, fill=self.GRID)
        if not self.samples:
            c.create_text(w / 2, h / 2, text="Geschwindigkeit (Bilder pro Sekunde)", fill="#6a6a6a",
                          font=(self.font, 9))
            return
        t_max = max(self.samples[-1][0], 10.0)
        r_max = max(max(r for _, r in self.samples) * 1.25, 0.5)
        line = [(t / t_max * w, h - r / r_max * (h - 10)) for t, r in self.samples]
        poly = [(0, h), (0, line[0][1])] + line + [(line[-1][0], h)]
        c.create_polygon(*[v for p in poly for v in p], fill=self.ACCENT_FILL, outline="")
        if len(line) > 1:
            c.create_line(*[v for p in line for v in p], fill=self.ACCENT, width=2)
        cur = line[-1][1]
        c.create_line(0, cur, w, cur, fill=self.ACCENT, dash=(3, 3))
        c.create_text(w - 6, max(cur - 9, 10), anchor="e", fill=self.ACCENT, font=(self.font, 9, "bold"),
                      text=f"{fmt_num(self.samples[-1][1])} Bilder/s")

    def on_phase(self):
        self.prog_title.configure(text="Vorschau wird erstellt …" if self.dry else "Dateien werden umbenannt …")

    def on_result(self, old, new, status, info):
        self.counts[status] += 1
        tags = (status,)
        if info in (OCR_HINT, PREFIX_HINT) and status in (RENAMED, PLANNED):
            tags = ("ocr",)
        self.tree.insert("", "end", values=(self.STATUS_TEXT[status], old, new or "–", info), tags=tags)

    def on_done(self, cancelled):
        self.running = False
        elapsed = time.time() - self.t_start if self.t_start else 0
        for w in (self.entry, self.browse_btn, self.dry_chk, self.open_btn, self.prefix_entry):
            w.configure(state="normal")
        self.start_btn.configure(state="normal", text="Start", style="Accent.TButton")
        self.stat_vars["eta"].set("–")
        if self.done and elapsed:
            self.stat_vars["speed"].set(f"Ø {fmt_num(self.done / elapsed)} Bilder/s")
        self.update_folder_info()
        if cancelled:
            self.prog_title.configure(text=f"Abgebrochen nach {self.done} von {self.total} Bildern – nichts umbenannt")
            return
        if not self.total:
            return
        self.pbar.configure(value=self.total)
        c = self.counts
        if self.dry:
            self.prog_title.configure(text=f"Vorschau fertig in {fmt_time(elapsed)} – es wurde nichts umbenannt")
            parts = [f"{c[PLANNED]} würden umbenannt"]
        else:
            self.prog_title.configure(text=f"Fertig in {fmt_time(elapsed)}")
            parts = [f"{c[RENAMED]} umbenannt"]
        for key, word in ((UNCHANGED, "unverändert"), (NOT_FOUND, "ohne Label"), (DENIED, "gesperrt")):
            if c[key]:
                parts.append(f"{c[key]} {word}")
        self.summary.configure(text=" · ".join(parts))
        if c[DENIED]:
            from tkinter import messagebox

            messagebox.showwarning(
                APP,
                f"{c[DENIED]} Dateien konnten nicht umbenannt werden (Zugriff verweigert).\n\n"
                "Häufigste Ursache auf Firmen-PCs: Der 'Überwachte Ordnerzugriff' schützt Bilder, "
                "Dokumente und Desktop. Lege die Fotos in einen anderen Ordner (z. B. Downloads, "
                "C:\\Temp oder das Netzlaufwerk) oder schließe Programme, in denen die Fotos geöffnet sind.",
            )

    def on_close(self):
        if self.running:
            from tkinter import messagebox

            if not messagebox.askyesno(APP, "Die Verarbeitung läuft noch. Wirklich beenden?"):
                return
            self.cancel.set()
        self.root.destroy()


def run_gui(folder=None):
    if sys.platform == "win32":
        try:  # scharfe Schrift auf hochauflösenden Bildschirmen
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    import tkinter as tk

    root = tk.Tk()
    App(root, folder)
    if sys.platform == "win32":
        try:  # dunkle Titelleiste (Windows 10/11)
            import ctypes

            root.update()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:  # noqa: BLE001
            pass
    root.mainloop()


def run_headless(folder, dry_run, report=None, prefix="", debug=False):
    """Ohne Fenster, für automatische Tests."""
    lines, counts = [], {}

    def emit(event, **d):
        if event == "result":
            counts[d["status"]] = counts.get(d["status"], 0) + 1
            lines.append(f"{d['status']}\t{d['old']}\t{d['new']}\t{d['info']}")

    run_job(folder, dry_run, emit, threading.Event(), 0, prefix, debug)
    text = "\n".join(lines) + "\n"
    if report:
        with open(report, "w", encoding="utf-8") as fh:
            fh.write(text)
    if sys.stdout:
        print(text, end="")
    return 0 if not (counts.get(NOT_FOUND) or counts.get(DENIED)) else 2


def main():
    ap = argparse.ArgumentParser(description="Fotos nach Labelnummer umbenennen")
    ap.add_argument("folder", nargs="?", help="Ordner mit den Fotos")
    ap.add_argument("-n", "--dry-run", action="store_true", help="nur Vorschau, nichts umbenennen")
    ap.add_argument("--headless", action="store_true", help="ohne Fenster ausführen")
    ap.add_argument("--report", help="Ergebnisliste in diese Datei schreiben (nur mit --headless)")
    ap.add_argument("--prefix", default="", help="Präfix für abgeklebte Labels, z. B. F08482ID00")
    ap.add_argument("--debug", action="store_true", help="gelesenen OCR-Text bei Fehlschlag ausgeben")
    args = ap.parse_args()
    if args.headless:
        if not args.folder or not os.path.isdir(args.folder):
            return 1
        return run_headless(args.folder, args.dry_run, args.report, args.prefix.upper(), args.debug)
    run_gui(args.folder if args.folder and os.path.isdir(args.folder) else None)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
