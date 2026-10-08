#!/usr/bin/env python3
"""artwallpaper — a museum on your desktop, one artwork a day.

Pulls public-domain paintings from open museum APIs (Art Institute of
Chicago, The Met, Cleveland Museum of Art) and Wikimedia Commons, picks
one per day (seeded by the date, so every machine agrees and a re-run is
idempotent), renders it for your screen — smart crop, museum matte, or a
"section of the day" window into Chinese handscrolls — sets it as the
desktop wallpaper, and keeps attribution.

Stdlib only; Pillow (optional) enables matte mode, captions and
handscroll crops. Python 3.11+.

Usage:
  artwallpaper.py next                 # pick, render, set today's wallpaper
  artwallpaper.py next --shuffle       # ignore the date seed, pick randomly
  artwallpaper.py preview --days 7     # render a week of picks, set nothing
  artwallpaper.py current              # attribution for the current wallpaper
  artwallpaper.py open                 # open the museum page for it
  artwallpaper.py refresh              # force-refresh candidate pools
  artwallpaper.py doctor               # check APIs, Pillow, platform setter
  artwallpaper.py install-schedule --time 08:30
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Wikimedia requires a descriptive User-Agent; the museum APIs are happier with one too.
USER_AGENT = "art-wallpaper/0.1 (personal desktop wallpaper; +https://github.com/JunfengJia/llm-wiki)"

AIC_API = "https://api.artic.edu/api/v1/artworks/search"
AIC_IIIF = "https://www.artic.edu/iiif/2"
MET_API = "https://collectionapi.metmuseum.org/public/collection/v1"
CMA_API = "https://openaccess-api.clevelandart.org/api/artworks/"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

DEFAULT_CONFIG_DIR = Path("~/.art-wallpaper").expanduser()


# ---------------------------------------------------------------- http

def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_json(url: str, timeout: int = 30) -> dict:
    return json.loads(http_get(url, timeout))


def http_download(url: str, dest: Path, timeout: int = 120) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.rename(dest)
    return dest


# ---------------------------------------------------------------- config

DEFAULT_DISPLAY = {
    "width": 2560,
    "height": 1600,
    "mode": "auto",          # fill | matte | auto
    "matte": "auto",         # auto = derive a muted tone from the artwork, or "#1b1b1d"
    "caption": True,
}

DEFAULT_BEHAVIOR = {
    "save_dir": "~/Pictures/ArtWallpapers",
    "history": 90,           # don't repeat an artwork within this many picks
    "min_width": 1400,       # skip source images narrower than this
    "pool_ttl_days": 7,      # re-query the APIs this often
    "keep_files": 30,        # downloaded wallpapers to keep on disk
}


def load_config(path: Path | None) -> dict:
    cfg_path = path or DEFAULT_CONFIG_DIR / "config.toml"
    if not cfg_path.exists():
        bundled = Path(__file__).resolve().parent / "config.example.toml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if bundled.exists():
            shutil.copy(bundled, cfg_path)
            print(f"first run: wrote default config to {cfg_path}", file=sys.stderr)
        else:
            sys.exit(f"no config at {cfg_path} and no bundled config.example.toml found")
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    cfg = {
        "display": DEFAULT_DISPLAY | raw.get("display", {}),
        "behavior": DEFAULT_BEHAVIOR | raw.get("behavior", {}),
        "collection": raw.get("collection", []),
        "_path": cfg_path,
    }
    if not cfg["collection"]:
        sys.exit(f"config {cfg_path} defines no [[collection]] entries")
    return cfg


def state_dir() -> Path:
    d = DEFAULT_CONFIG_DIR
    (d / "cache").mkdir(parents=True, exist_ok=True)
    return d


def slugify(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text).strip("-")
    return text[:limit] or "artwork"


# ---------------------------------------------------------------- sources
# Each fetcher returns a list of candidate dicts:
#   {id, title, artist, date, page, image_url, width, height, source}
# width/height may be None when the API doesn't expose pixel dimensions.


def fetch_aic(coll: dict, min_width: int) -> list[dict]:
    """Art Institute of Chicago — no key, IIIF images (max 1686px wide)."""
    out = []
    for page in (1, 2):
        params = {
            "q": coll["query"],
            "query[term][is_public_domain]": "true",
            "fields": "id,title,artist_display,date_display,image_id,thumbnail",
            "limit": 100,
            "page": page,
        }
        data = http_json(f"{AIC_API}?{urllib.parse.urlencode(params)}")
        for a in data.get("data", []):
            if not a.get("image_id"):
                continue
            thumb = a.get("thumbnail") or {}
            w, h = thumb.get("width"), thumb.get("height")
            if w and w < min_width:
                continue
            out.append({
                "id": f"aic:{a['id']}",
                "title": a.get("title") or "Untitled",
                "artist": (a.get("artist_display") or "").split("\n")[0],
                "date": a.get("date_display") or "",
                "page": f"https://www.artic.edu/artworks/{a['id']}",
                "image_url": f"{AIC_IIIF}/{a['image_id']}/full/1686,/0/default.jpg",
                "width": 1686 if (w or 0) >= 1686 else w,
                "height": round(1686 * h / w) if (w and h and w >= 1686) else h,
                "source": "Art Institute of Chicago",
            })
        if len(data.get("data", [])) < 100:
            break
    return out


def fetch_met(coll: dict, min_width: int) -> list[dict]:
    """The Met — no key, full-resolution originals. One request per object,
    so IDs are capped and fetched concurrently."""
    params = {"q": coll["query"], "hasImages": "true"}
    if coll.get("department"):
        params["departmentId"] = coll["department"]
    if coll.get("medium"):
        params["medium"] = coll["medium"]
    if coll.get("artist_only"):
        params["artistOrCulture"] = "true"
    data = http_json(f"{MET_API}/search?{urllib.parse.urlencode(params)}")
    ids = (data.get("objectIDs") or [])[: coll.get("max_objects", 80)]

    def fetch_object(oid: int) -> dict | None:
        try:
            o = http_json(f"{MET_API}/objects/{oid}", timeout=20)
        except Exception:
            return None
        if not o.get("isPublicDomain") or not o.get("primaryImage"):
            return None
        return {
            "id": f"met:{oid}",
            "title": o.get("title") or "Untitled",
            "artist": o.get("artistDisplayName") or o.get("culture") or "",
            "date": o.get("objectDate") or "",
            "page": o.get("objectURL") or f"https://www.metmuseum.org/art/collection/search/{oid}",
            "image_url": o["primaryImage"],
            "width": None,  # Met originals are large; API doesn't expose pixel dims
            "height": None,
            "source": "The Met",
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(fetch_object, ids)
    return [r for r in results if r]


def fetch_cma(coll: dict, min_width: int) -> list[dict]:
    """Cleveland Museum of Art — no key, CC0 filter, print/full-size JPEGs."""
    params = {"q": coll["query"], "has_image": 1, "cc0": 1, "limit": 100}
    if coll.get("department"):
        params["department"] = coll["department"]
    if coll.get("type"):
        params["type"] = coll["type"]
    data = http_json(f"{CMA_API}?{urllib.parse.urlencode(params)}")
    out = []
    for a in data.get("data", []):
        if coll.get("department") and a.get("department") != coll["department"]:
            continue
        images = a.get("images") or {}
        img = images.get("print") or images.get("full") or images.get("web")
        if not img or not img.get("url"):
            continue
        w = int(img["width"]) if img.get("width") else None
        if w and w < min_width:
            continue
        creators = a.get("creators") or []
        out.append({
            "id": f"cma:{a['id']}",
            "title": a.get("title") or "Untitled",
            "artist": creators[0].get("description", "") if creators else "",
            "date": a.get("creation_date") or "",
            "page": a.get("url") or f"https://www.clevelandart.org/art/{a.get('accession_number', '')}",
            "image_url": img["url"],
            "width": w,
            "height": int(img["height"]) if img.get("height") else None,
            "source": "Cleveland Museum of Art",
        })
    return out


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def fetch_wikimedia(coll: dict, min_width: int) -> list[dict]:
    """Wikimedia Commons — curated file list and/or a whole category."""
    out = []
    entries = coll.get("files", [])
    if entries:
        titles, meta = [], {}
        for e in entries:
            if isinstance(e, str):
                e = {"file": e}
            t = "File:" + e["file"]
            titles.append(t)
            meta[t.replace("_", " ")] = e
        for i in range(0, len(titles), 50):
            params = {
                "action": "query", "format": "json",
                "titles": "|".join(titles[i:i + 50]),
                "prop": "imageinfo",
                "iiprop": "url|size|extmetadata",
                "iiurlwidth": 4000,
            }
            data = http_json(f"{COMMONS_API}?{urllib.parse.urlencode(params)}")
            for p in (data.get("query", {}).get("pages", {}) or {}).values():
                out.extend(_commons_candidate(p, meta.get(p.get("title", ""), {}), min_width))
    if coll.get("category"):
        params = {
            "action": "query", "format": "json",
            "generator": "categorymembers",
            "gcmtitle": "Category:" + coll["category"],
            "gcmtype": "file", "gcmlimit": 100,
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": 4000,
        }
        data = http_json(f"{COMMONS_API}?{urllib.parse.urlencode(params)}")
        for p in (data.get("query", {}).get("pages", {}) or {}).values():
            out.extend(_commons_candidate(p, {}, min_width))
    return out


def _commons_candidate(page: dict, meta: dict, min_width: int) -> list[dict]:
    info = (page.get("imageinfo") or [None])[0]
    if not info or "missing" in page:
        return []
    w, h = info.get("width"), info.get("height")
    if not w or w < min_width:
        return []
    ext = (info.get("extmetadata") or {})
    # Handscrolls keep the original (full detail for section crops);
    # everything else uses a 4000px thumb to avoid 100MB scans.
    is_scroll = h and (w / h) > 3.0
    url = info["url"] if (is_scroll or w <= 4500) else (info.get("thumburl") or info["url"])
    title = meta.get("title") or re.sub(
        r"\.(jpe?g|png|tiff?)$", "", page.get("title", "").removeprefix("File:"), flags=re.I
    ).replace("_", " ")
    artist = meta.get("artist") or strip_tags(ext.get("Artist", {}).get("value", ""))[:80]
    date = meta.get("date") or strip_tags(ext.get("DateTimeOriginal", {}).get("value", ""))[:40]
    return [{
        "id": "wm:" + page.get("title", "")[:120],
        "title": title,
        "artist": artist,
        "date": date,
        "page": info.get("descriptionurl") or "https://commons.wikimedia.org",
        "image_url": url,
        "width": w if (is_scroll or w <= 4500) else min(w, 4000),
        "height": h if (is_scroll or w <= 4500) else round(h * min(w, 4000) / w),
        "source": meta.get("source") or "Wikimedia Commons",
    }]


FETCHERS = {"aic": fetch_aic, "met": fetch_met, "cma": fetch_cma, "wikimedia": fetch_wikimedia}


# ---------------------------------------------------------------- pools

def build_pools(cfg: dict, force: bool = False) -> dict[str, list[dict]]:
    pools = {}
    ttl = dt.timedelta(days=cfg["behavior"]["pool_ttl_days"])
    cache_dir = state_dir() / "cache"
    for coll in cfg["collection"]:
        if coll.get("weight", 1) <= 0:
            continue
        name = coll["name"]
        cache_file = cache_dir / (slugify(name) + ".json")
        cached = None
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            fresh = dt.datetime.fromisoformat(cached["ts"]) > dt.datetime.now() - ttl
            if fresh and not force:
                pools[name] = cached["candidates"]
                continue
        try:
            candidates = FETCHERS[coll["source"]](coll, cfg["behavior"]["min_width"])
            if candidates:
                cache_file.write_text(json.dumps(
                    {"ts": dt.datetime.now().isoformat(), "candidates": candidates},
                    ensure_ascii=False))
                pools[name] = candidates
                print(f"  pool '{name}': {len(candidates)} artworks", file=sys.stderr)
            elif cached:
                pools[name] = cached["candidates"]
        except Exception as e:
            print(f"  pool '{name}' failed ({e}); "
                  + ("using stale cache" if cached else "skipping"), file=sys.stderr)
            if cached:
                pools[name] = cached["candidates"]
    if not pools:
        sys.exit("no candidate pools available (network down and no cache?)")
    return pools


# ---------------------------------------------------------------- selection

def load_history() -> list[dict]:
    f = state_dir() / "history.json"
    return json.loads(f.read_text()) if f.exists() else []


def save_history(hist: list[dict]) -> None:
    (state_dir() / "history.json").write_text(
        json.dumps(hist[-500:], ensure_ascii=False, indent=1))


def choose(cfg: dict, pools: dict, day: dt.date, exclude: set[str],
           shuffle: bool = False) -> dict:
    seed = os.urandom(8).hex() if shuffle else f"artwallpaper:{day.isoformat()}"
    rng = random.Random(seed)
    colls = [c for c in cfg["collection"]
             if c.get("weight", 1) > 0 and pools.get(c["name"])]
    weights = [c["weight"] for c in colls]
    for _ in range(len(colls) * 2):
        coll = rng.choices(colls, weights=weights)[0]
        fresh = [a for a in pools[coll["name"]] if a["id"] not in exclude]
        if fresh:
            art = rng.choice(fresh)
            art["_collection"] = coll["name"]
            return art
    # every pool exhausted by history — allow repeats
    coll = rng.choices(colls, weights=weights)[0]
    art = rng.choice(pools[coll["name"]])
    art["_collection"] = coll["name"]
    return art


# ---------------------------------------------------------------- rendering

FONT_CANDIDATES = [
    # CJK-capable fonts first so Chinese titles render.
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def find_font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return None


def muted_tone(img) -> tuple[int, int, int]:
    r, g, b = img.resize((1, 1), Image.LANCZOS).getpixel((0, 0))
    # pull the average toward a deep neutral so the matte recedes
    return tuple(int(c * 0.30 + 20) for c in (r, g, b))


def render(src: Path, art: dict, cfg: dict, day: dt.date, out: Path) -> tuple[Path, str]:
    """Returns (path_to_set, mode_used)."""
    if not HAS_PIL:
        return src, "native (Pillow not installed — OS scales the image)"
    disp = cfg["display"]
    W, H = disp["width"], disp["height"]
    img = Image.open(src).convert("RGB")
    iw, ih = img.size
    sr, ir = W / H, iw / ih
    mode_used = []

    # Handscroll: show a different section each day.
    if ir > 2.5 * sr:
        win = int(ih * sr)
        if win < iw:
            rng = random.Random(f"{day.isoformat()}:{art['id']}:crop")
            x = rng.randint(0, iw - win)
            img = img.crop((x, 0, x + win, ih))
            iw, ih = img.size
            ir = iw / ih
            mode_used.append("handscroll section")

    mode = disp["mode"]
    if mode == "auto":
        mode = "fill" if 0.75 <= ir / sr <= 1.33 else "matte"

    if mode == "fill":
        scale = max(W / iw, H / ih)
        img = img.resize((round(iw * scale), round(ih * scale)), Image.LANCZOS)
        left = (img.width - W) // 2
        top = (img.height - H) // 2
        canvas = img.crop((left, top, left + W, top + H))
        mode_used.append("fill")
    else:
        margin = int(0.06 * min(W, H))
        scale = min((W - 2 * margin) / iw, (H - 2 * margin) / ih, 1.6)
        aw, ah = round(iw * scale), round(ih * scale)
        artimg = img.resize((aw, ah), Image.LANCZOS)
        matte = disp["matte"]
        bg = (tuple(int(matte.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
              if isinstance(matte, str) and matte.startswith("#") else muted_tone(img))
        canvas = Image.new("RGB", (W, H), bg)
        x, y = (W - aw) // 2, (H - ah) // 2
        shadow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(shadow).rectangle(
            (x - 2, y + margin // 10, x + aw + 2, y + ah + margin // 5), fill=90)
        shadow = shadow.filter(ImageFilter.GaussianBlur(margin // 4))
        canvas.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), shadow)
        canvas.paste(artimg, (x, y))
        mode_used.append("matte")

    if disp["caption"]:
        parts = [art["title"].strip()]
        if art.get("artist"):
            parts.append(art["artist"].strip())
        if art.get("date"):
            parts.append(str(art["date"]).strip())
        text = " · ".join(p for p in parts if p)[:140] + f"  —  {art['source']}"
        font = find_font(max(13, W // 115))
        if font:
            overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(overlay)
            tw = d.textlength(text, font=font)
            tx, ty = W - tw - int(0.02 * W), H - int(0.035 * H)
            d.text((tx + 1, ty + 1), text, font=font, fill=(0, 0, 0, 110))
            d.text((tx, ty), text, font=font, fill=(255, 255, 255, 175))
            canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    canvas.save(out, "JPEG", quality=90)
    return out, " + ".join(mode_used)


# ---------------------------------------------------------------- wallpaper setting

def set_wallpaper(path: Path) -> str:
    system = platform.system()
    p = str(path.resolve())
    if system == "Darwin":
        script = f'tell application "System Events" to tell every desktop to set picture to "{p}"'
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
        return "macOS (System Events)"
    if system == "Windows":
        import ctypes
        if not ctypes.windll.user32.SystemParametersInfoW(20, 0, p, 3):
            raise RuntimeError("SystemParametersInfoW failed")
        return "Windows"
    # Linux: try the common desktops in order.
    uri = f"file://{p}"
    attempts = [
        (["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri], "GNOME"),
        (["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri], "GNOME"),
        (["gsettings", "set", "org.gnome.desktop.background", "picture-options", "zoom"], "GNOME"),
        (["plasma-apply-wallpaperimage", p], "KDE"),
        (["feh", "--bg-fill", p], "feh"),
    ]
    succeeded = None
    for cmd, name in attempts:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=10)
                succeeded = succeeded or name
                if name != "GNOME":
                    break
            except Exception:
                continue
    if succeeded:
        return f"Linux ({succeeded})"
    raise RuntimeError("no wallpaper setter found (tried gsettings, plasma, feh); "
                       f"image saved at {p}")


# ---------------------------------------------------------------- commands

def cmd_next(cfg: dict, args) -> None:
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    pools = build_pools(cfg)
    hist = load_history()
    exclude = {h["id"] for h in hist[-cfg["behavior"]["history"]:]}
    art = choose(cfg, pools, day, exclude, shuffle=args.shuffle)

    save_dir = Path(cfg["behavior"]["save_dir"]).expanduser()
    raw = save_dir / "raw" / f"{slugify(art['id'])}.img"
    print(f"→ {art['title']} — {art['artist']} ({art['date']}) [{art['source']}]")
    if args.dry_run:
        print(f"  would download {art['image_url']}")
        return
    http_download(art["image_url"], raw)
    final = save_dir / f"{day.isoformat()}-{slugify(art['title'])}.jpg"
    rendered, mode = render(raw, art, cfg, day, final)
    print(f"  rendered: {mode} → {rendered}")

    if not args.no_set:
        how = set_wallpaper(rendered)
        print(f"  wallpaper set via {how}")

    hist.append({"id": art["id"], "date": day.isoformat(), "title": art["title"]})
    save_history(hist)
    (state_dir() / "current.json").write_text(json.dumps(
        {**{k: v for k, v in art.items() if not k.startswith("_")},
         "file": str(rendered), "set_on": day.isoformat(), "mode": mode},
        ensure_ascii=False, indent=1))
    prune_files(save_dir, cfg["behavior"]["keep_files"])


def prune_files(save_dir: Path, keep: int) -> None:
    files = sorted(save_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    for f in files[:-keep]:
        f.unlink(missing_ok=True)


def cmd_preview(cfg: dict, args) -> None:
    out_dir = Path(args.out or Path(cfg["behavior"]["save_dir"]).expanduser() / "preview")
    out_dir.mkdir(parents=True, exist_ok=True)
    pools = build_pools(cfg)
    exclude: set[str] = set()
    start = dt.date.today()
    for i in range(args.days):
        day = start + dt.timedelta(days=i)
        art = choose(cfg, pools, day, exclude)
        exclude.add(art["id"])
        raw = out_dir / "raw" / f"{slugify(art['id'])}.img"
        try:
            http_download(art["image_url"], raw)
            final = out_dir / f"{day.isoformat()}-{slugify(art['title'])}.jpg"
            _, mode = render(raw, art, cfg, day, final)
            print(f"{day}  {art['title']} — {art['artist']}  [{mode}]")
        except Exception as e:
            print(f"{day}  FAILED {art['title']}: {e}", file=sys.stderr)
    print(f"\npreviews in {out_dir}")


def cmd_current(cfg: dict, args) -> None:
    f = state_dir() / "current.json"
    if not f.exists():
        sys.exit("no wallpaper set yet — run: artwallpaper.py next")
    print(f.read_text())


def cmd_open(cfg: dict, args) -> None:
    f = state_dir() / "current.json"
    if not f.exists():
        sys.exit("no wallpaper set yet — run: artwallpaper.py next")
    webbrowser.open(json.loads(f.read_text())["page"])


def cmd_refresh(cfg: dict, args) -> None:
    build_pools(cfg, force=True)


def cmd_doctor(cfg: dict, args) -> None:
    print(f"config: {cfg['_path']}")
    print(f"python: {sys.version.split()[0]}   Pillow: {'yes' if HAS_PIL else 'NO (pip install Pillow for matte/caption/scroll modes)'}")
    checks = {
        "Art Institute of Chicago": f"{AIC_API}?q=monet&limit=1",
        "The Met": f"{MET_API}/objects/436535",
        "Cleveland Museum of Art": f"{CMA_API}?q=monet&limit=1",
        "Wikimedia Commons": f"{COMMONS_API}?action=query&meta=siteinfo&format=json",
    }
    for name, url in checks.items():
        try:
            http_json(url, timeout=15)
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
    print(f"display target: {cfg['display']['width']}x{cfg['display']['height']} "
          f"(set [display] width/height in config to match your screen)")


def cmd_install_schedule(cfg: dict, args) -> None:
    script = str(Path(__file__).resolve())
    py = sys.executable
    hour, minute = args.time.split(":")
    system = platform.system()
    if system == "Darwin":
        plist_path = Path("~/Library/LaunchAgents/com.artwallpaper.daily.plist").expanduser()
        log = state_dir() / "run.log"
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.artwallpaper.daily</string>
  <key>ProgramArguments</key><array>
    <string>{py}</string><string>{script}</string><string>next</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>{int(hour)}</integer>
    <key>Minute</key><integer>{int(minute)}</integer>
  </dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
        plist_path.write_text(plist)
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)
        print(f"launchd job installed: daily at {args.time} (also fires after wake if missed)")
    elif system == "Linux":
        uid = os.getuid()
        line = (f'{int(minute)} {int(hour)} * * * DISPLAY=:0 '
                f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus '
                f'{py} {script} next >> {state_dir()}/run.log 2>&1')
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if script in existing:
            print("crontab already has an artwallpaper entry; edit it with: crontab -e")
            return
        subprocess.run(["crontab", "-"], input=existing + line + "\n", text=True, check=True)
        print(f"cron job installed: daily at {args.time}")
    else:
        print("run this in an elevated prompt:")
        print(f'  schtasks /Create /SC DAILY /ST {args.time} /TN ArtWallpaper '
              f'/TR "{py} {script} next"')


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("next", help="pick, render and set today's wallpaper")
    p.add_argument("--shuffle", action="store_true", help="random pick, ignore date seed")
    p.add_argument("--date", help="pretend it's this date (YYYY-MM-DD)")
    p.add_argument("--no-set", action="store_true", help="download+render only")
    p.add_argument("--dry-run", action="store_true", help="show the pick, download nothing")
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("preview", help="render N days of picks without setting anything")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--out", help="output directory")
    p.set_defaults(fn=cmd_preview)

    sub.add_parser("current", help="attribution for the current wallpaper").set_defaults(fn=cmd_current)
    sub.add_parser("open", help="open the museum page for the current wallpaper").set_defaults(fn=cmd_open)
    sub.add_parser("refresh", help="force-refresh candidate pools").set_defaults(fn=cmd_refresh)
    sub.add_parser("doctor", help="check API reachability and local setup").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("install-schedule", help="run daily via launchd/cron/Task Scheduler")
    p.add_argument("--time", default="08:30", help="HH:MM local time (default 08:30)")
    p.set_defaults(fn=cmd_install_schedule)

    args = ap.parse_args()
    cfg = load_config(args.config)
    args.fn(cfg, args)


if __name__ == "__main__":
    main()
