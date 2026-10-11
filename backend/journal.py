"""
trailbook: turn a folder of geotagged trip photos into JSON any frontend can use.

Pipeline
  1. Read GPS + timestamp from each photo's EXIF data
  2. Look up the address for each spot (OpenStreetMap Nominatim, cached)
  3. A LOCAL model (via Ollama) fills in, for each photo: location name, title, quote
  4. The same model writes one story for the whole journey
  5. Output: JSON (stops + GeoJSON). No HTML, no map, no images inside.

Usage
  python journal.py ./trip_photos --model <your-ollama-model>
  python journal.py ./trip_photos --no-ai              # EXIF + locations only
  python journal.py ./trip_photos --model <m> --out -  # print JSON to stdout
"""

import argparse
import base64
import io
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ImageOps

try:  # iPhone HEIC support is optional
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_OK = True
except ImportError:
    HEIC_OK = False

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "trailbook/0.2 (local photo journal; hacktoberfest project)"


def err(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass
class Stop:
    path: Path
    lat: float
    lon: float
    taken: datetime | None
    address: str = ""        # from the map lookup (factual)
    location_name: str = ""  # friendly name (AI, grounded in the address)
    title: str = ""          # AI
    quote: str = ""          # AI


# --------------------------------------------------------------------------
# 1. EXIF
# --------------------------------------------------------------------------
def _dms_to_deg(dms, ref) -> float:
    d, m, s = (float(x) for x in dms)
    deg = d + m / 60 + s / 3600
    return -deg if str(ref).upper() in ("S", "W") else deg


def read_exif(path: Path):
    """Return (lat, lon, datetime). lat/lon are None if the photo has no GPS."""
    with Image.open(path) as im:
        exif = im.getexif()
        gps = exif.get_ifd(0x8825)  # GPSInfo
        sub = exif.get_ifd(0x8769)  # Exif sub-IFD

    lat = lon = None
    try:
        if 2 in gps and 4 in gps:
            lat = _dms_to_deg(gps[2], gps.get(1, "N"))
            lon = _dms_to_deg(gps[4], gps.get(3, "E"))
            if lat == 0 and lon == 0:  # some devices write 0,0 when there is no fix
                lat = lon = None
    except (TypeError, ValueError, ZeroDivisionError):
        lat = lon = None

    if lat is not None and lon is not None:
        if not (math.isfinite(lat) and math.isfinite(lon)):
            lat = lon = None

    taken = None
    raw = sub.get(0x9003) or exif.get(0x0132)  # DateTimeOriginal / DateTime
    if raw:
        try:
            taken = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass
    return lat, lon, taken


def collect_stops(folder: Path):
    stops, skipped = [], []
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in PHOTO_EXTS)
    if not files:
        sys.exit(f"No photos found in {folder}")

    for p in files:
        if p.suffix.lower() in (".heic", ".heif") and not HEIC_OK:
            skipped.append({"filename": p.name, "reason": "HEIC needs: pip install pillow-heif"})
            continue
        try:
            lat, lon, taken = read_exif(p)
        except Exception as e:
            skipped.append({"filename": p.name, "reason": f"unreadable ({e})"})
            continue
        if lat is None or lon is None:
            skipped.append({"filename": p.name, "reason": "no GPS data"})
            continue
        stops.append(Stop(p, lat, lon, taken))

    # Oldest first; photos with no timestamp go last, in filename order.
    stops.sort(key=lambda s: (s.taken is None, s.taken or datetime.min, s.path.name))
    return stops, skipped


# --------------------------------------------------------------------------
# 2. Address lookup
# --------------------------------------------------------------------------
def load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, cache: dict) -> None:
    try:
        path.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    except OSError:
        pass


def reverse_geocode(lat: float, lon: float, cache: dict, cache_path: Path) -> str:
    key = f"{lat:.4f},{lon:.4f}"
    if key in cache:
        return cache[key]
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 17},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return ""  # failures are not cached

    addr = data.get("address", {})
    specific = data.get("name") or next(
        (addr[k] for k in ("tourism", "amenity", "leisure", "natural", "historic",
                           "road", "pedestrian", "path") if addr.get(k)), "")
    area = next((addr[k] for k in ("suburb", "neighbourhood", "village", "town",
                                   "city", "county") if addr.get(k)), "")
    region = next((addr[k] for k in ("state", "country") if addr.get(k)), "")
    label = ", ".join(dict.fromkeys(p for p in (specific, area, region) if p))
    if not label:
        label = ", ".join((data.get("display_name") or "").split(", ")[:3])

    cache[key] = label
    save_cache(cache_path, cache)
    time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
    return label


# --------------------------------------------------------------------------
# 3 + 4. Local model (Ollama)
# --------------------------------------------------------------------------
class Model:
    def __init__(self, host: str, name: str):
        self.host, self.name = host.rstrip("/"), name

    def chat(self, prompt: str, images=None, timeout=600) -> str:
        msg = {"role": "user", "content": prompt}
        if images:
            msg["images"] = images
        payload = {
            "model": self.name,
            "messages": [msg],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.7},
        }
        r = requests.post(f"{self.host}/api/chat", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()["message"]["content"].strip()

    def check(self) -> None:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException:
            sys.exit(f"Can't reach Ollama at {self.host}. Is it running? (ollama serve)")
        names = [m["name"] for m in r.json().get("models", [])]
        if not any(n == self.name or n.split(":")[0] == self.name for n in names):
            sys.exit(f"Model '{self.name}' not found. Installed: {', '.join(names) or 'none'}\n"
                     f"Pull it with: ollama pull {self.name}")


def parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return {}


def model_image(path: Path, max_px=768) -> str:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def fill_stop(model: Model, stop: Stop, index: int, total: int, earlier: list[Stop]) -> None:
    """Ask the model for location_name, title and quote for one photo."""
    when = stop.taken.strftime("%A, %H:%M") if stop.taken else "unknown time"
    history = "; ".join(f'{i}. {s.title or s.location_name} ("{s.quote}")'
                        for i, s in enumerate(earlier, 1)) or "this is the first stop"
    prompt = (
        f"You are filling in one entry of a travel journal. This is photo {index} of {total}.\n"
        f"Address from a map lookup: {stop.address or 'not available'}\n"
        f"Time taken: {when}\n"
        f"Earlier stops of this journey: {history}\n\n"
        "Look at the photo and return JSON only, in exactly this shape:\n"
        '{"location_name": "...", "title": "...", "quote": "..."}\n'
        "- location_name: a short, friendly place name based ONLY on the address above. "
        'If the address is "not available", use an empty string.\n'
        "- title: 2 to 6 words for this photo.\n"
        "- quote: one short first-person sentence, a thought or feeling that fits the photo "
        "and where the journey is so far. Do not invent people, names or events."
    )
    data = parse_json(model.chat(prompt, images=[model_image(stop.path)]))
    stop.location_name = str(data.get("location_name") or "").strip()
    stop.title = str(data.get("title") or "").strip()
    stop.quote = str(data.get("quote") or "").strip()


def write_story(model: Model, stops: list[Stop], tone: str, about: str) -> dict:
    lines = []
    for i, s in enumerate(stops, 1):
        when = s.taken.strftime("%d %b, %H:%M") if s.taken else "time unknown"
        lines.append(f'{i}. {s.location_name or s.address or "unnamed place"} ({when}) - '
                     f'"{s.title}": {s.quote}')
    prompt = (
        f"Write the main story of a journey, in first person. Tone: {tone}.\n"
        + (f"The journey is about: {about}\n" if about else "")
        + "These are the stops in order. Use ONLY these facts; do not invent names, "
        "events or people.\n\n" + "\n".join(lines)
        + '\n\nReturn JSON only: {"title": "short journey title", '
        '"story": "one cohesive story of 1 to 3 short paragraphs"}'
    )
    return parse_json(model.chat(prompt))


# --------------------------------------------------------------------------
# 5. Output
# --------------------------------------------------------------------------
def haversine_km(a: Stop, b: Stop) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def build_output(stops, skipped, journey_title, story) -> dict:
    iso = lambda d: d.isoformat() if d else None
    taken = [s.taken for s in stops if s.taken]
    stop_dicts = []
    for i, s in enumerate(stops, 1):
        stop_dicts.append({
            "order": i,
            "filename": s.path.name,
            "taken_at": iso(s.taken),
            "location": {
                "lat": round(s.lat, 6),
                "lon": round(s.lon, 6),
                "name": s.location_name or s.address,
                "address": s.address,
            },
            "title": s.title,
            "quote": s.quote,
        })

    # GeoJSON uses [longitude, latitude] order. Most map libraries accept it directly.
    features = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(s.lon, 6), round(s.lat, 6)]},
        "properties": {"order": d["order"], "filename": d["filename"],
                       "name": d["location"]["name"], "title": d["title"],
                       "quote": d["quote"], "taken_at": d["taken_at"]},
    } for s, d in zip(stops, stop_dicts)]
    if len(stops) > 1:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[round(s.lon, 6), round(s.lat, 6)] for s in stops]},
            "properties": {"kind": "route"},
        })

    return {
        "journey": {
            "title": journey_title,
            "story": story,
            "photo_count": len(stops),
            "started_at": iso(min(taken)) if taken else None,
            "ended_at": iso(max(taken)) if taken else None,
            "distance_km": round(sum(haversine_km(a, b) for a, b in zip(stops, stops[1:])), 2),
        },
        "stops": stop_dicts,
        "geojson": {"type": "FeatureCollection", "features": features},
        "skipped": skipped,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build_journal(folder: Path, model_name: str | None = None,
                  host: str = "http://localhost:11434",
                  tone: str = "warm, observant and a little playful", about: str = "",
                  geocode: bool = True, use_ai: bool = True, log=err) -> dict:
    """Run the whole pipeline and return the result as a dict (ready for json.dumps)."""
    stops, skipped = collect_stops(folder)
    for s in skipped:
        log(f"Skipped {s['filename']}: {s['reason']}")
    if not stops:
        sys.exit("None of the photos had GPS data, so there is nothing to map. "
                 "Tip: photos sent through chat apps usually lose their location.")
    log(f"Found {len(stops)} geotagged photo(s).")

    if geocode:
        cache_path = folder / ".trailbook_geocache.json"
        cache = load_cache(cache_path)
        log("Looking up addresses...")
        for s in stops:
            s.address = reverse_geocode(s.lat, s.lon, cache, cache_path)

    title = folder.resolve().name.replace("_", " ").replace("-", " ").title()
    story = ""
    if use_ai:
        if not model_name:
            sys.exit("A model name is required when use_ai is true")
        model = Model(host, model_name)
        model.check()
        for i, s in enumerate(stops, 1):
            log(f"Filling in photo {i}/{len(stops)}...")
            try:
                fill_stop(model, s, i, len(stops), stops[:i - 1])
            except requests.RequestException as e:
                log(f"  model error: {e}")
        log("Writing the journey story...")
        try:
            result = write_story(model, stops, tone, about)
            title = result.get("title") or title
            story = str(result.get("story") or "")
        except requests.RequestException as e:
            log(f"  model error: {e}")

    return build_output(stops, skipped, title, story)


def main() -> None:
    ap = argparse.ArgumentParser(description="Turn geotagged photos into journal JSON.")
    ap.add_argument("folder", type=Path, help="folder of trip photos")
    ap.add_argument("--out", default="journal.json", help='output file, or "-" for stdout')
    ap.add_argument("--model", default=os.environ.get("JOURNAL_MODEL"),
                    help="Ollama model name (or set JOURNAL_MODEL)")
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--tone", default="warm, observant and a little playful")
    ap.add_argument("--about", default="", help="one line about the journey, for context")
    ap.add_argument("--no-geocode", action="store_true",
                    help="skip address lookups (no network calls at all)")
    ap.add_argument("--no-ai", action="store_true", help="skip the model")
    args = ap.parse_args()

    if not args.folder.is_dir():
        sys.exit(f"Not a folder: {args.folder}")
    if not args.no_ai and not args.model:
        sys.exit("Pick a model with --model <name> (or JOURNAL_MODEL=...), "
                 "or use --no-ai to run without one.")
    if not args.host.startswith("http"):
        args.host = "http://" + args.host

    result = build_journal(args.folder, args.model, args.host, args.tone, args.about,
                           geocode=not args.no_geocode, use_ai=not args.no_ai)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        err(f"Done. Wrote {args.out}")


if __name__ == "__main__":
    main()
