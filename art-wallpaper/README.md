# art-wallpaper

A museum on your desktop: one public-domain artwork a day, picked to your
taste (莫奈 / Impressionism, 中国山水, and friends), rendered like a framed
piece and set as the wallpaper. Single Python file, no API keys, no accounts.

```bash
pip install Pillow            # optional but recommended (matte / caption / scroll modes)
python3 artwallpaper.py doctor          # check the APIs are reachable
python3 artwallpaper.py preview --days 7   # see a week of picks before committing
python3 artwallpaper.py next            # set today's wallpaper
python3 artwallpaper.py install-schedule --time 08:30
```

First run copies `config.example.toml` to `~/.art-wallpaper/config.toml`.
Set `[display] width/height` to your screen and tune collection `weight`s.

## What it does

- **Date-seeded daily pick** — same artwork all day, new one tomorrow,
  idempotent re-runs. `--shuffle` for an immediate random change.
- **Weighted taste profile** — collections (Monet at AIC, Chinese paintings
  at the Met, curated classics on Commons…) with weights; no repeats within
  the last 90 picks.
- **Three render modes** (`auto` picks per artwork):
  - `fill` — edge-to-edge crop when the aspect ratio is close to the screen.
  - `matte` — museum matte in a muted tone derived from the artwork, with a
    drop shadow and a small wall-label caption. This is what makes hanging
    scrolls like 溪山行旅图 work as wallpaper.
  - **handscroll section-of-the-day** — for 手卷 (aspect > ~4:1, e.g.
    千里江山图 at 39,974×1,600 px) it crops a different screen-shaped window
    each day: a slow walk through the scroll over weeks.
- **Attribution kept** — `current` prints title/artist/museum/license link;
  `open` jumps to the museum page.
- **Resilient** — candidate pools cached 7 days; if an API is down it falls
  back to the stale cache.

Wallpaper setting: macOS (System Events), Windows (SystemParametersInfo),
Linux (GNOME gsettings → KDE → feh fallback).

## Data sources (all keyless)

| Source | API | Notes |
|---|---|---|
| Art Institute of Chicago | `api.artic.edu/api/v1/artworks/search` | `is_public_domain` filter; IIIF images capped at 1686px wide — fine on a laptop, soft on 4K (matte mode compensates). World's largest group of Monet's *Stacks of Wheat*. |
| The Met | `collectionapi.metmuseum.org/public/collection/v1` | Full-resolution originals, `isPublicDomain` flag. Department 6 = Asian Art: one of the best 宋–清 painting collections outside Asia. One request per object, so the tool caps + parallelizes. |
| Cleveland Museum of Art | `openaccess-api.clevelandart.org/api/artworks/` | `cc0=1` filter, ~3400px `print` JPEGs, strong Chinese Art department. |
| Wikimedia Commons | `commons.wikimedia.org/w/api.php` | For everything the US museums don't hold: 北京故宫 (千里江山图), 台北故宫 (早春图/溪山行旅图/富春山居图), Marmottan (Impression, Sunrise), MoMA-held but PD works (Starry Night). Curated file list and/or whole categories. **Requires a descriptive User-Agent** (the tool sends one). |

Worth adding later: Rijksmuseum (free key, superb scans), Smithsonian Open
Access (key; National Museum of Asian Art = Freer/Sackler for Chinese art),
台北故宫 Open Data (bulk downloads, no clean API).

## Prior art reviewed

- [justdataplease/art-history-museum](https://github.com/justdataplease/art-history-museum) —
  the inspiration: walkable 3D browser museum, 526 artists / 82k works
  across 35 traditions including Chinese. All data from Wikipedia/Wikidata/
  Wikimedia Commons with a Dagster pipeline; Next.js front end. Validates
  Commons as the backbone source; this tool swaps the 3D gallery for your
  desktop and adds the dedicated museum APIs for higher-res scans.
- [muzei/muzei](https://github.com/muzei/muzei) — Android daily-art live
  wallpaper (WikiArt-curated). Borrowed ideas: daily cadence, artwork
  attribution surfaced, pluggable sources.
- [nghiatranuit/artify-macos](https://github.com/nghiatranuit/artify-macos) —
  macOS daily-art menubar app; native but single-source and unmaintained.
- Variety (Linux), Bing-wallpaper scripts — the cron/launchd + fetch + set
  pattern this follows, minus their random-photo sources.

## Tuning taste

Everything is `[[collection]]` entries in the config:

```toml
[[collection]]
name = "Pissarro"
source = "aic"            # aic | met | cma | wikimedia
query = "Camille Pissarro"
weight = 2                 # relative frequency; 0 disables

[[collection]]
name = "宋画 Song paintings (Commons category)"
source = "wikimedia"
category = "Paintings of the Song Dynasty in the National Palace Museum"
weight = 2
```

`met` collections also take `department` (6 = Asian Art), `medium`
(e.g. `"Paintings"`), `artist_only = true`. `cma` takes `department` /
`type`. `wikimedia` takes `files = [...]` (string or
`{ file, title, artist, date, source }`) and/or `category`.

## Troubleshooting

- `doctor` checks each API host — corporate proxies are honored via the
  standard `HTTPS_PROXY` env var.
- Cron on GNOME needs `DBUS_SESSION_BUS_ADDRESS` — `install-schedule`
  writes it into the cron line for you.
- Low-res handscroll scans (e.g. 富春山居图 at 508px tall) upscale soft;
  ink wash tolerates it, but drop them from the curated list if it bothers
  you.
- macOS multi-space: System Events sets the picture per current space
  design; spaces created later may keep the old image until revisited.

## Files

- `artwallpaper.py` — the whole tool (~550 lines, stdlib + optional Pillow)
- `config.example.toml` — default taste profile: Monet×3 sources, Chinese
  painting×2 sources, curated classics incl. 千里江山图/早春图/溪山行旅图/
  富春山居图/星夜/神奈川冲浪里
- `samples/gallery.html` — hotlinked preview gallery of the curated set
