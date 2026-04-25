#!/usr/bin/env python3
"""
calibrate-from-kworb.py — daily refresh of listener-ratios.json.

Replaces the manual browser-based scraper. Fetches monthly-listener counts
from kworb.net (which scrapes Spotify with residential IPs and publishes
the data daily), pairs each artist with the follower count already in
prices.json, and writes a fresh listener-ratios.json that the hourly
fetch-prices.py pipeline reads to estimate listeners from followers.

Why kworb instead of scraping Spotify directly:
  · GitHub Actions IPs are blocked by Spotify's web frontend
  · The official Web API doesn't expose monthly listeners
  · kworb is free, has no API key, has been stable for years
  · 100 % of our roster is covered (104 in listeners.html top-2500;
    boygenius is on listeners2.html which we also fetch)

Outputs:
  listener-ratios.json — same schema the existing pipeline expects:
    {
      "calibratedAt": "YYYY-MM-DD",
      "description": "...",
      "defaultRatio": <median of per-artist ratios>,
      "ratios": { "<spotifyId>": <ratio>, ... }
    }

Exit codes:
  0  success — file written (or unchanged but valid)
  1  fatal — couldn't fetch kworb or roster missing too many artists
"""

import json
import statistics
import sys
import urllib.request
from datetime import date
from pathlib import Path
from html.parser import HTMLParser

HERE = Path(__file__).parent
ARTISTS_FILE = HERE / "artists.json"
PRICES_FILE = HERE / "prices.json"
RATIOS_FILE = HERE / "listener-ratios.json"

# Kworb publishes top monthly-listener artists in pages of 2,500.
# As of 2026-04 our smallest artist (boygenius, ~3.2 M) lands at #4,704,
# so the first two pages always cover us. Fetching a third page costs
# nothing if you ever add an artist below ~2 M monthly listeners.
KWORB_PAGES = [
    "https://kworb.net/spotify/listeners.html",
    "https://kworb.net/spotify/listeners2.html",
]

# Hard floor on coverage. If kworb starts returning empty pages or our
# roster drifts, we want to fail loudly instead of silently overwriting
# good ratios with garbage.
MIN_COVERAGE = 0.90  # 90 % of artists must be found on kworb
USER_AGENT = "muse-calibrator/1.0 (+https://muses.exchange)"


class _RowParser(HTMLParser):
    """Streaming parser for kworb's listeners table.

    The table layout is stable: each <tr> contains
        <td>rank</td>
        <td class="text"><div><a href="artist/<spotifyId>_songs.html">name</a></div></td>
        <td>listeners</td>
        <td>daily change</td>
        ...

    A regex would also work but parsing through 510 KB of HTML with re.DOTALL
    is slow; the html.parser is fast and reliable.
    """

    def __init__(self):
        super().__init__()
        self.rows = []  # list of (spotifyId, listeners)
        self._cur_id = None
        self._tds = []  # raw text content of <td>s in the current row
        self._in_td = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur_id = None
            self._tds = []
        elif tag == "td":
            self._in_td = True
            self._buf = []
        elif tag == "a":
            href = dict(attrs).get("href", "")
            # match "artist/<22 base62 chars>_songs.html"
            if href.startswith("artist/") and href.endswith("_songs.html"):
                sid = href[len("artist/"):-len("_songs.html")]
                if len(sid) == 22:
                    self._cur_id = sid

    def handle_endtag(self, tag):
        if tag == "td":
            self._tds.append("".join(self._buf).strip())
            self._in_td = False
        elif tag == "tr":
            # td[0] = rank, td[1] = name, td[2] = listeners, td[3] = daily change
            if self._cur_id and len(self._tds) >= 3:
                listeners_str = self._tds[2].replace(",", "").replace("\xa0", "")
                try:
                    listeners = int(listeners_str)
                    if listeners > 0:
                        self.rows.append((self._cur_id, listeners))
                except ValueError:
                    pass

    def handle_data(self, data):
        if self._in_td:
            self._buf.append(data)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def kworb_listeners() -> dict[str, int]:
    """Fetch all kworb listener pages and return {spotifyId: monthly_listeners}."""
    all_listeners: dict[str, int] = {}
    for url in KWORB_PAGES:
        print(f"  · fetching {url}")
        html = fetch(url)
        parser = _RowParser()
        parser.feed(html)
        for sid, lst in parser.rows:
            # Keep highest rank (first occurrence) if dupes — shouldn't happen
            all_listeners.setdefault(sid, lst)
        print(f"    parsed {len(parser.rows)} rows; running total {len(all_listeners)}")
    return all_listeners


def main() -> int:
    artists = json.loads(ARTISTS_FILE.read_text())
    print(f"Loaded {len(artists)} artists from {ARTISTS_FILE.name}")

    # Followers come from prices.json (produced by the hourly fetch). If it's
    # missing — first ever run — bail out and tell the user to run the price
    # job once first.
    if not PRICES_FILE.exists():
        print(f"! {PRICES_FILE.name} not found. Run fetch-prices.py once first.",
              file=sys.stderr)
        return 1
    prices = json.loads(PRICES_FILE.read_text())
    followers_by_id: dict[str, int] = {}
    for a in prices.get("artists", []):
        sid = a.get("spotifyId")
        fol = a.get("followers", 0)
        if sid and fol:
            followers_by_id[sid] = fol
    print(f"Loaded follower counts for {len(followers_by_id)} artists from {PRICES_FILE.name}")

    print("Fetching monthly-listener data from kworb.net …")
    try:
        kworb = kworb_listeners()
    except Exception as e:
        print(f"! kworb fetch failed: {e}", file=sys.stderr)
        return 1
    print(f"  · {len(kworb)} unique artists indexed by kworb")

    # Compute per-artist ratios in roster order
    ratios: dict[str, float] = {}
    no_listeners: list[tuple[str, str]] = []
    no_followers: list[tuple[str, str]] = []
    for a in artists:
        sid = a["spotifyId"]
        listeners = kworb.get(sid)
        fol = followers_by_id.get(sid, 0)
        if not listeners:
            no_listeners.append((sid, a["name"]))
            continue
        if fol <= 0:
            no_followers.append((sid, a["name"]))
            continue
        ratios[sid] = round(listeners / fol, 4)

    coverage = len(ratios) / len(artists)
    print(f"\nCoverage: {len(ratios)} / {len(artists)} ({coverage:.1%})")
    if no_listeners:
        print(f"  ! {len(no_listeners)} artist(s) missing from kworb:")
        for sid, name in no_listeners:
            print(f"      {sid}  {name}")
    if no_followers:
        print(f"  ! {len(no_followers)} artist(s) missing follower count:")
        for sid, name in no_followers:
            print(f"      {sid}  {name}")

    if coverage < MIN_COVERAGE:
        print(f"! coverage below {MIN_COVERAGE:.0%} threshold — refusing to write "
              f"listener-ratios.json. Investigate before retrying.", file=sys.stderr)
        return 1

    # Median is robust to outliers (Fontaines D.C., CKay) and matches what
    # the hourly pipeline already expects as defaultRatio for unknown artists.
    median_ratio = round(statistics.median(ratios.values()), 4)

    calibration = {
        "calibratedAt": str(date.today()),
        "description": "Per-artist monthly-listeners / followers ratio from kworb.net.",
        "defaultRatio": median_ratio,
        "ratios": ratios,
    }
    RATIOS_FILE.write_text(json.dumps(calibration, indent=2) + "\n")

    print(f"\nWrote {RATIOS_FILE.name} — defaultRatio (median) {median_ratio}, "
          f"min {min(ratios.values())}, max {max(ratios.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
