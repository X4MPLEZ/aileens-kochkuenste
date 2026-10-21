"""Verarbeitet neue Fotos im Ordner photos/:
Datum aus EXIF (sonst Dateiname, sonst Upload-Zeitpunkt), Ausrichtung korrigieren,
auf max. 1400 px verkleinern, als JPEG speichern, photos.json fortschreiben.
Läuft automatisch per GitHub Action nach jedem Upload."""
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

TZ = ZoneInfo("Europe/Berlin")
PHOTOS = Path("photos")
MANIFEST = Path("photos.json")
EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}
DONE = re.compile(r"^\d{4}-\d{2}-\d{2}_[0-9a-z]{6}\.jpg$")
MAX = 1400

PHOTOS.mkdir(exist_ok=True)

manifest = {"photos": []}
if MANIFEST.exists():
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest = {"photos": data if isinstance(data, list) else data.get("photos", [])}
    except Exception:
        pass
photos = manifest["photos"]

# Einträge entfernen, deren Datei gelöscht wurde
photos[:] = [p for p in photos if Path(p["file"]).exists()]
by_file = {p["file"] for p in photos}
seen_src = {p.get("src") for p in photos if p.get("src")}
seen_hash = {p.get("hash") for p in photos if p.get("hash")}


def exif_date(img):
    try:
        ex = img.getexif()
        sub = ex.get_ifd(0x8769)
        raw = sub.get(0x9003) or sub.get(0x9004) or ex.get(0x0132)
        if raw:
            return datetime.strptime(str(raw)[:19], "%Y:%m:%d %H:%M:%S"), "exif"
    except Exception:
        pass
    return None, None


def name_date(name):
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", name)  # z. B. IMG-20251014-WA0001
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), 12, 0, 0), "name"
        except ValueError:
            pass
    return None, None


def upload_date(path):
    try:
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%aI", "-1", "--", str(path)],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        if out:
            return datetime.fromisoformat(out).astimezone(TZ).replace(tzinfo=None), "file"
    except Exception:
        pass
    return datetime.now(TZ).replace(tzinfo=None), "file"


changed = 0
for f in sorted(PHOTOS.iterdir()):
    if not f.is_file() or f.suffix.lower() not in EXT:
        continue
    rel = f"photos/{f.name}"
    if rel in by_file and DONE.match(f.name):
        continue  # schon verarbeitet
    raw = f.read_bytes()
    digest = hashlib.sha1(raw).hexdigest()
    if f.name in seen_src or digest in seen_hash:
        f.unlink()  # Duplikat
        changed += 1
        continue
    try:
        img = Image.open(f)
        img.load()
    except Exception as e:
        print(f"Übersprungen {f.name}: {e}")
        continue

    date, source = exif_date(img)
    if not date:
        date, source = name_date(f.name)
    if not date:
        date, source = upload_date(f)

    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    scale = min(1.0, MAX / max(w, h))
    if scale < 1:
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)

    new_name = f"{date:%Y-%m-%d}_{digest[:6]}.jpg"
    out = PHOTOS / new_name
    img.save(out, "JPEG", quality=82, optimize=True)
    if out.name != f.name:
        f.unlink()

    photos.append({
        "file": f"photos/{new_name}",
        "date": date.strftime("%Y-%m-%dT%H:%M:%S"),
        "dateSource": source,
        "caption": "",
        "w": img.size[0],
        "h": img.size[1],
        "crop": {"fx": 0.5, "fy": 0.5, "z": 1},
        "src": f.name,
        "hash": digest,
    })
    seen_src.add(f.name)
    seen_hash.add(digest)
    changed += 1
    print(f"{f.name} -> {new_name} ({source}, {date:%d.%m.%Y %H:%M})")

# ---------- Gericht erkennen (Claude API), nur wenn ein Schlüssel hinterlegt ist ----------
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("MODEL", "").strip() or "claude-haiku-5-5"
MAX_AI = int(os.environ.get("MAX_AI_PER_RUN", "60") or 60)
PROMPT = (
    "Auf dem Foto ist ein selbst gekochtes Gericht. Antworte NUR mit JSON in genau dieser Form: "
    '{"gericht": "<kurzer Name des Gerichts auf Deutsch, höchstens 4 Wörter>", '
    '"beschreibung": "<ein warmer, kurzer Satz auf Deutsch mit höchstens 18 Wörtern, der beschreibt, was zu sehen ist; '
    "ohne Übertreibung, ohne Ausrufezeichen; bei Unsicherheit vorsichtig formulieren, z. B. mit 'vermutlich'>\"}. "
    'Wenn kein Essen zu sehen ist: {"gericht": "", "beschreibung": ""}.'
)


def describe(path):
    img = Image.open(path).convert("RGB")
    img.thumbnail((900, 900))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    data = base64.b64encode(buf.getvalue()).decode()
    body = {
        "model": MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
            {"type": "text", "text": PROMPT},
        ]}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        res = json.load(r)
    text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    j = json.loads(m.group(0)) if m else {}
    return str(j.get("gericht", "")).strip(" .\n"), str(j.get("beschreibung", "")).strip()


recognized = 0
if API_KEY:
    errors = 0
    for p in photos:
        if p.get("ai") or recognized >= MAX_AI or errors >= 3:
            continue
        try:
            dish, desc = describe(Path(p["file"]))
        except Exception as e:
            errors += 1
            print(f"KI-Fehler bei {p['file']}: {e}")
            continue
        if dish:
            p["dish"] = dish
            if not p.get("caption"):
                p["caption"] = dish
        if desc and not p.get("desc"):
            p["desc"] = desc
        p["ai"] = True
        recognized += 1
        changed += 1
        print(f"Erkannt: {p['file']} -> {dish} | {desc}")
        time.sleep(0.4)
else:
    print("Kein ANTHROPIC_API_KEY hinterlegt – Gerichte werden nicht erkannt.")

photos.sort(key=lambda p: p["date"])
MANIFEST.write_text(json.dumps({"photos": photos}, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{changed} Änderungen, {recognized} Gerichte erkannt, {len(photos)} Bilder insgesamt.")
