#!/usr/bin/env python3
"""
Muse price fetcher — Python version.

Pulls the 24 tracked artists from the Spotify Web API, optionally augments
with YouTube Data API view/subscriber counts, runs the pricing formula,
blends with the previous run for smoothing, and writes prices.json and
history.json next to this file.

Dependencies: Python 3.8+ standard library only. No pip install required.

Environment variables (read from .env in the same folder OR the shell):
    SPOTIFY_CLIENT_ID      (required)
    SPOTIFY_CLIENT_SECRET  (required)
    YOUTUBE_API_KEY        (optional — enables YouTube-weighted pricing)

Output files:
    prices.json            — latest snapshot used by the prototype
    history.json           — rolling price history (1080-point cap)
    youtube-channels.json  — cached YouTube channel IDs (auto-populated)
"""

import base64
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
ARTISTS_FILE = HERE / "artists.json"
OUT_FILE = HERE / "prices.json"
OUT_JS_FILE = HERE / "prices.js"  # file:// fallback — loaded via <script> tag
HISTORY_FILE = HERE / "history.json"
HISTORY_JS_FILE = HERE / "history.js"  # file:// fallback
YOUTUBE_CACHE_FILE = HERE / "youtube-channels.json"
TOKEN_CACHE_FILE = HERE / ".spotify-token.json"
BASELINE_FILE = HERE / ".market-baseline.json"
LISTENER_RATIOS_FILE = HERE / "listener-ratios.json"
DEBUG_LOG_FILE = HERE / "_debug.txt"
HISTORY_MAX_POINTS = 1080  # ~6 months at one point every 4 hours
HISTORY_DEDUP_SECONDS = 60  # drop points logged within 60s of the previous one
MUSE_INDEX_BASELINE = 1000.0  # market index is rebased to this on first run

# Pricing weights — tune these if one signal starts dominating.
YOUTUBE_BOOST_MAX = 0.30  # a huge YouTube presence can boost the Spotify fair
                          # price by up to +30%. Artists with no YouTube data
                          # just get boost = 0 (back to pure Spotify pricing).
CHART_BOOST_MAX = 0.25    # appearing at the top of Spotify's Global Top 50 /
                          # Viral 50 playlists can add up to +25% to the fair
                          # price. Artists off the charts get 0 boost.
POPULARITY_BOOST_MAX = 0.15   # popularity (0-100) maps to ±15% price swing
MOMENTUM_PER_POINT   = 0.005  # 0.5% per point of popularity change
MOMENTUM_CAP         = 0.10   # max ±10% from momentum alone

# Spotify editorial chart playlists we track. These are the canonical "charts"
# endpoints on Spotify — the Web API deprecated the old /v1/browse/charts route
# in 2024 but the playlists themselves are still public and fetchable with the
# same client credentials token we already have.
SPOTIFY_CHART_PLAYLISTS = {
    "global_top50":  {"id": "37i9dQZEVXbMDoHDwVN2tF", "weight": 1.0, "label": "Top 50 Global"},
    "global_viral50": {"id": "37i9dQZEVXbLiRSasKsNU9", "weight": 0.6, "label": "Viral 50 Global"},
}
YOUTUBE_MAX_RESOLVES_PER_RUN = 80  # Channel-search costs 100 quota units each.
                                    # Free tier is 10,000/day, so capping at 80
                                    # keeps first-run cost at 8,000 + the stats
                                    # call (≈1 unit) ≈ 8,001 — well under limit.
                                    # If the roster is >80 unresolved artists,
                                    # the rest finish on subsequent runs.

# Loaded at runtime from artists.json — see load_artists_config().
ARTISTS = []

TOKEN_URL = "https://accounts.spotify.com/api/token"
ARTISTS_URL = "https://api.spotify.com/v1/artists"
SEARCH_URL = "https://api.spotify.com/v1/search"


def load_artists_config():
    """Load the artist roster from artists.json. Must contain ticker/name/
    genre and optional spotifyId. Tickers must be unique."""
    if not ARTISTS_FILE.exists():
        raise SystemExit(f"✗ Missing {ARTISTS_FILE.name} — cannot continue.")
    try:
        data = json.loads(ARTISTS_FILE.read_text())
    except Exception as e:
        raise SystemExit(f"✗ {ARTISTS_FILE.name} is not valid JSON: {e}")
    if not isinstance(data, list) or not data:
        raise SystemExit(f"✗ {ARTISTS_FILE.name} must be a non-empty JSON array.")
    seen = set()
    for a in data:
        t = a.get("ticker")
        if not t or not a.get("name") or not a.get("genre"):
            raise SystemExit(f"✗ Bad entry in {ARTISTS_FILE.name}: {a}")
        if t in seen:
            raise SystemExit(f"✗ Duplicate ticker in {ARTISTS_FILE.name}: {t}")
        seen.add(t)
        a.setdefault("spotifyId", "")
    return data


def save_artists_config(artists):
    """Write the artist roster back, preserving any Spotify IDs we just
    resolved. We keep the compact per-line formatting so diffs stay readable,
    and use ensure_ascii=False so names like 'Rosalía' stay human-readable."""
    def j(v):
        return json.dumps(v, ensure_ascii=False)
    lines = ["["]
    for i, a in enumerate(artists):
        comma = "," if i < len(artists) - 1 else ""
        lines.append(
            f'  {{ "ticker": {j(a["ticker"]):<8}, '
            f'"name": {j(a["name"]):<28}, '
            f'"genre": {j(a["genre"]):<13}, '
            f'"spotifyId": {j(a.get("spotifyId", ""))} }}{comma}'
        )
    lines.append("]")
    ARTISTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


MIN_RESOLVE_FOLLOWERS = 50_000  # reject "Giveon" with 11 followers, etc.
MIN_RESOLVE_POPULARITY = 40     # also reject dormant namesake accounts


def _spotify_search(token, q):
    headers = {"Authorization": f"Bearer {token}"}
    params = urllib.parse.urlencode({"q": q, "type": "artist", "limit": 10})
    try:
        resp = http_request("GET", f"{SEARCH_URL}?{params}", headers=headers)
    except urllib.error.HTTPError:
        return []
    return (resp.get("artists") or {}).get("items") or []


def spotify_search_artist_id(token, name):
    """Resolve an artist name → Spotify artist ID via the Search endpoint.
    Filters out low-quality matches (tiny namesake accounts) and prefers
    the candidate with the highest popularity. Returns None on miss.

    Guards against the "Giveon" problem: an exact literal search for
    'artist:"Giveon"' may return a tiny unrelated account, while the real
    Giveon is indexed under 'GIVĒON'. We therefore try several queries
    and always reject candidates under the follower/popularity floor.
    """
    # Try progressively broader queries until we find a high-quality match.
    candidates = []
    for q in (f'artist:"{name}"', name, name.split(",")[0].strip(), name.split()[0]):
        for item in _spotify_search(token, q):
            followers = (item.get("followers") or {}).get("total") or 0
            pop = item.get("popularity") or 0
            if followers < MIN_RESOLVE_FOLLOWERS and pop < MIN_RESOLVE_POPULARITY:
                continue
            candidates.append(item)
        if candidates:
            break
    if not candidates:
        return None
    # De-dupe by id (the broader queries may return the same item twice).
    seen = set()
    unique = []
    for it in candidates:
        if it["id"] not in seen:
            seen.add(it["id"])
            unique.append(it)
    # Rank by: exact-name-match, then popularity, then followers.
    def score(it):
        n = it.get("name", "").lower()
        name_match = 1 if n == name.lower() else (0.5 if name.lower() in n or n in name.lower() else 0)
        return (name_match, it.get("popularity", 0), (it.get("followers") or {}).get("total") or 0)
    unique.sort(key=score, reverse=True)
    return unique[0].get("id")


def resolve_missing_spotify_ids(artists, token):
    """For each artist without a spotifyId, search Spotify and fill it in.
    Persists the updated IDs back to artists.json so subsequent runs skip
    the search calls entirely."""
    missing = [a for a in artists if not a.get("spotifyId")]
    if not missing:
        return 0
    print(f"  · resolving Spotify IDs for {len(missing)} new artists…")
    resolved = 0
    for a in missing:
        cid = spotify_search_artist_id(token, a["name"])
        if cid:
            a["spotifyId"] = cid
            resolved += 1
        else:
            print(f"    ! could not resolve {a['ticker']} ({a['name']})")
    if resolved:
        save_artists_config(artists)
    return resolved


def load_dotenv():
    """Minimal .env loader — only needed when run outside GitHub Actions."""
    env_path = HERE / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http_request(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------- Real monthly listeners (Spotify internal partner API) ----------
#
# The Spotify Web API does NOT expose monthly listeners — only followers and
# popularity. The old approach of scraping the open.spotify.com artist page
# no longer works because Spotify now serves a pure JavaScript SPA shell
# with zero artist data in the initial HTML.
#
# Instead, we use the same internal partner API that the Spotify web player
# calls.  This requires a web-player access token obtained via the `sp_dc`
# cookie from any logged-in Spotify session.
#
# Setup (one-time):
#   1. Log in to https://open.spotify.com in your browser.
#   2. Open DevTools → Application → Cookies → open.spotify.com
#   3. Copy the value of the `sp_dc` cookie.
#   4. Store it as a GitHub repo secret named  SP_DC .
#
# The cookie typically stays valid for ~1 year. If the pipeline starts
# falling back to the follower proxy for every artist, the cookie has
# probably expired — just repeat the steps above.
#
# We treat this as best-effort: if anything fails we return None and the
# caller falls back to the follower-based proxy so the pipeline keeps
# working.

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Spotify partner API GraphQL endpoint (same one the web player uses).
_PARTNER_API = "https://api-partner.spotify.com/pathfinder/v1/query"

# Persisted-query hash for the "queryArtistOverview" operation.  Spotify
# rotates these hashes when they ship new web-player builds, but the
# artist-overview hash has historically been very stable.  If Spotify
# changes it, update the value here.
_ARTIST_OVERVIEW_HASH = "da986392124383827dc03cbb3d66c1de81225244b6e82571ece77f1b596e9e05"


def _get_web_access_token(sp_dc, timeout=15, _dbg=None):
    """Exchange an sp_dc cookie for a short-lived web-player access token.

    Returns (access_token: str, client_id: str) or (None, None) on failure.
    """
    def log(msg):
        print(msg)
        if _dbg is not None:
            _dbg.append(msg)

    url = ("https://open.spotify.com/get_access_token"
           "?reason=transport&productType=web_player")
    req = urllib.request.Request(url, headers={
        "User-Agent": _BROWSER_UA,
        "Cookie": f"sp_dc={sp_dc}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            data = json.loads(raw)
        token = data.get("accessToken")
        client_id = data.get("clientId")
        is_anon = data.get("isAnonymous")
        log(f"  · token exchange response: isAnonymous={is_anon}, "
            f"hasToken={bool(token)}, clientId={client_id or 'none'}")
        if token and not is_anon:
            return token, client_id
        log(f"  ! sp_dc token exchange returned anonymous/empty token "
            f"(isAnonymous={is_anon})")
        return None, None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        log(f"  ! sp_dc token exchange failed: HTTP {e.code} {body}")
        return None, None
    except Exception as e:
        log(f"  ! sp_dc token exchange failed: {e}")
        return None, None


def _query_partner_api(access_token, spotify_id, timeout=15):
    """Call the Spotify partner API to get monthly listeners for one artist.

    Returns an int, or None on failure.
    """
    variables = json.dumps({
        "uri": f"spotify:artist:{spotify_id}",
        "locale": "en",
        "includePrerelease": True,
    })
    extensions = json.dumps({
        "persistedQuery": {
            "version": 1,
            "sha256Hash": _ARTIST_OVERVIEW_HASH,
        }
    })
    params = urllib.parse.urlencode({
        "operationName": "queryArtistOverview",
        "variables": variables,
        "extensions": extensions,
    })
    url = f"{_PARTNER_API}?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _BROWSER_UA,
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "App-Platform": "WebPlayer",
        "Spotify-App-Version": "1.2.52.442.g0f1fed98",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        print(f"  ! partner API failed for {spotify_id}: HTTP {e.code} {body}")
        return None
    except Exception as e:
        print(f"  ! partner API failed for {spotify_id}: {e}")
        return None

    # Navigate the GraphQL response to find monthlyListeners.
    try:
        stats = data["data"]["artistUnion"]["stats"]
        listeners = int(stats["monthlyListeners"])
        if 1_000 <= listeners <= 500_000_000:
            return listeners
        print(f"  ! implausible listener count for {spotify_id}: {listeners}")
        return None
    except (KeyError, TypeError, ValueError) as e:
        print(f"  ! could not parse partner API response for {spotify_id}: {e}")
        return None


def fetch_all_monthly_listeners(spotify_ids):
    """Fetch real monthly listeners for every artist via the partner API.

    Returns { spotify_id: int }. Missing entries mean the fetch failed
    for that artist — the caller should fall back to follower proxy.
    """
    debug_lines = []
    def dbg(msg):
        print(msg)
        debug_lines.append(msg)

    sp_dc = os.environ.get("SP_DC", "").strip()
    dbg(f"  · SP_DC env var: {'set (' + str(len(sp_dc)) + ' chars)' if sp_dc else 'NOT SET'}")
    if sp_dc:
        dbg(f"  · SP_DC first 8 chars: {sp_dc[:8]}...")
    if not sp_dc:
        dbg("  ! SP_DC env var not set — skipping monthly-listener fetch "
            "(all artists will use follower proxy)")
        _write_debug_log(debug_lines)
        return {}

    access_token, _ = _get_web_access_token(sp_dc, _dbg=debug_lines)
    if not access_token:
        dbg("  ! could not obtain web access token — "
            "SP_DC cookie may have expired")
        _write_debug_log(debug_lines)
        return {}

    dbg(f"  ✓ obtained web-player access token, querying {len(spotify_ids)} artists …")
    out = {}
    ok = 0
    for sid in spotify_ids:
        n = _query_partner_api(access_token, sid)
        if n is not None:
            out[sid] = n
            ok += 1
        time.sleep(0.1)  # ~10 req/sec, well under any rate limit

    dbg(f"  ✓ scraped monthly listeners for {ok}/{len(spotify_ids)} artists")
    _write_debug_log(debug_lines)
    return out


def _write_debug_log(lines):
    """Write debug lines to _debug.log for diagnosing GitHub Actions runs."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = f"=== debug run {ts} ===\n"
        DEBUG_LOG_FILE.write_text(header + "\n".join(lines) + "\n")
    except Exception as e:
        print(f"  ! failed to write debug log: {e}")


def load_cached_token():
    """Return a cached Spotify access token if it's still valid, else None."""
    if not TOKEN_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_FILE.read_text())
    except Exception:
        return None
    # Require ≥60s left to avoid using a token that expires mid-request.
    if data.get("expires_at", 0) - time.time() > 60:
        return data.get("access_token")
    return None


def save_cached_token(access_token, expires_in):
    try:
        TOKEN_CACHE_FILE.write_text(json.dumps({
            "access_token": access_token,
            "expires_at": int(time.time()) + int(expires_in),
        }))
    except Exception:
        pass  # token cache is best-effort


def get_access_token(client_id, client_secret):
    cached = load_cached_token()
    if cached:
        return cached
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        resp = http_request("POST", TOKEN_URL, headers=headers, data=body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Spotify token request failed: HTTP {e.code}\n{detail}")
    save_cached_token(resp["access_token"], resp.get("expires_in", 3600))
    return resp["access_token"]


def fetch_all_artists(token):
    """Batch the Spotify /artists endpoint (max 50 IDs per call)."""
    ids = [a["spotifyId"] for a in ARTISTS if a.get("spotifyId")]
    headers = {"Authorization": f"Bearer {token}"}
    by_id = {}
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        url = f"{ARTISTS_URL}?ids={','.join(batch)}"
        resp = http_request("GET", url, headers=headers)
        for a in (resp.get("artists") or []):
            if a:
                by_id[a["id"]] = a
    return by_id


# -------- Spotify editorial charts (Top 50 Global + Viral 50 Global) --------
PLAYLIST_URL = "https://api.spotify.com/v1/playlists"


def fetch_spotify_playlist(token, playlist_id):
    """Fetch up to 50 tracks from a Spotify playlist. Returns the raw items
    list, or [] on failure."""
    headers = {"Authorization": f"Bearer {token}"}
    params = urllib.parse.urlencode({
        "fields": "items(track(name,artists(id,name)))",
        "limit": 50,
    })
    url = f"{PLAYLIST_URL}/{playlist_id}/tracks?{params}"
    try:
        resp = http_request("GET", url, headers=headers)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        print(f"  ! Spotify playlist {playlist_id} fetch failed: HTTP {e.code} {detail}")
        return []
    return resp.get("items") or []


def fetch_chart_positions(token):
    """Return { spotifyArtistId: {global_top50: pos, global_viral50: pos, best_weighted_pos} }.

    For each playlist we walk the track list in order. Each track's primary
    artists get a position = their track's rank (1-indexed). If the same
    artist appears on multiple tracks we keep the best (lowest) rank.
    """
    positions = {}  # id -> { playlist_key: 1-indexed_pos }
    for key, meta in SPOTIFY_CHART_PLAYLISTS.items():
        items = fetch_spotify_playlist(token, meta["id"])
        if not items:
            continue
        for rank, item in enumerate(items, start=1):
            track = (item or {}).get("track") or {}
            for art in (track.get("artists") or []):
                aid = art.get("id")
                if not aid:
                    continue
                bucket = positions.setdefault(aid, {})
                cur = bucket.get(key)
                if cur is None or rank < cur:
                    bucket[key] = rank
        print(f"  · fetched {meta['label']}: {len(items)} tracks → "
              f"{sum(1 for p in positions.values() if key in p)} unique artists on chart")
    return positions


def chart_boost_factor(chart_stats):
    """Turn chart positions into a 0.0–0.25 multiplier.

    Scoring per chart: position 1 → 1.0, position 50 → 0.02, off-chart → 0.
    We then weight charts (Top 50 Global outweighs Viral 50), take the max
    weighted score across charts, and scale to CHART_BOOST_MAX.
    """
    if not chart_stats:
        return 0.0
    best = 0.0
    for key, meta in SPOTIFY_CHART_PLAYLISTS.items():
        pos = chart_stats.get(key)
        if pos is None:
            continue
        # Linear decay from 1.0 at rank 1 to ~0 at rank 50.
        raw = max(0.0, (51 - pos) / 50.0)
        weighted = raw * meta["weight"]
        if weighted > best:
            best = weighted
    return round(min(CHART_BOOST_MAX, best * CHART_BOOST_MAX), 4)


# -------- YouTube Data API (optional second signal) --------
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


def load_youtube_cache():
    if not YOUTUBE_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(YOUTUBE_CACHE_FILE.read_text())
    except Exception:
        return {}


def save_youtube_cache(cache):
    YOUTUBE_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def youtube_search_channel_id(api_key, name):
    """Resolve an artist name → canonical YouTube channel ID. Costs 100 quota
    units per call, vs 1 for a stats fetch — so we only call this once per
    artist and cache the result in youtube-channels.json."""
    params = urllib.parse.urlencode({
        "part": "snippet",
        "q": name,
        "type": "channel",
        "maxResults": 1,
        "key": api_key,
    })
    try:
        resp = http_request("GET", f"{YOUTUBE_SEARCH_URL}?{params}")
    except urllib.error.HTTPError as e:
        print(f"  ! YouTube search failed for {name}: HTTP {e.code}")
        return None
    items = resp.get("items", [])
    if not items:
        return None
    return items[0]["snippet"].get("channelId")


def youtube_fetch_stats(api_key, channel_ids):
    """Fetch viewCount / subscriberCount for any number of channel IDs.

    The YouTube channels endpoint caps at 50 IDs per call, so we batch in
    groups of 50. Each batch costs 1 quota unit, so fetching 105 artists
    costs ≈3 units — trivial compared to the free daily 10 000.
    """
    if not channel_ids:
        return {}
    out = {}
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i:i + 50]
        params = urllib.parse.urlencode({
            "part": "statistics",
            "id": ",".join(batch),
            "key": api_key,
        })
        try:
            resp = http_request("GET", f"{YOUTUBE_CHANNELS_URL}?{params}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            print(f"  ! YouTube stats batch {i // 50 + 1} failed: HTTP {e.code} {detail}")
            continue
        for item in resp.get("items", []):
            stats = item.get("statistics", {}) or {}
            out[item["id"]] = {
                "subscribers": int(stats.get("subscriberCount") or 0),
                "views": int(stats.get("viewCount") or 0),
                "videos": int(stats.get("videoCount") or 0),
            }
    return out


def fetch_all_youtube(api_key):
    """Returns { ticker: {subscribers, views, videos} } for every artist we
    can resolve, or {} if the API key is missing/invalid. Safe to call with
    a missing key — just returns empty."""
    if not api_key:
        return {}
    cache = load_youtube_cache()
    cache_dirty = False
    # Resolve any missing channel IDs, capped at YOUTUBE_MAX_RESOLVES_PER_RUN
    # to stay under the daily quota. Any remaining unresolved artists will be
    # picked up on the next run.
    pending = [a for a in ARTISTS
               if not (cache.get(a["ticker"]) or {}).get("channelId")]
    to_resolve = pending[:YOUTUBE_MAX_RESOLVES_PER_RUN]
    if len(pending) > len(to_resolve):
        print(f"  · {len(pending)} YouTube channels unresolved; resolving "
              f"{len(to_resolve)} this run (quota-capped), remainder next run")
    for a in to_resolve:
        print(f"  · resolving YouTube channel for {a['name']}…")
        # Prefer "Topic" auto-channels (more canonical) but fall back to direct.
        cid = youtube_search_channel_id(api_key, a["name"] + " topic")
        if not cid:
            cid = youtube_search_channel_id(api_key, a["name"])
        cache[a["ticker"]] = {"channelId": cid, "name": a["name"]}
        cache_dirty = True
    if cache_dirty:
        save_youtube_cache(cache)

    id_to_ticker = {}
    channel_ids = []
    for a in ARTISTS:
        entry = cache.get(a["ticker"]) or {}
        cid = entry.get("channelId")
        if cid:
            id_to_ticker[cid] = a["ticker"]
            channel_ids.append(cid)
    # YouTube channels endpoint caps at 50 IDs per call; we only have 24.
    stats_by_id = youtube_fetch_stats(api_key, channel_ids)
    return {id_to_ticker[cid]: stats for cid, stats in stats_by_id.items()}


def youtube_boost_factor(yt_stats):
    """Turn YouTube stats into a 0.0-0.3 multiplier that scales the Spotify
    fair price. Scaled so mega-artists like Taylor/Bad Bunny sit near the cap
    and niche artists with <10M views get close to 0."""
    if not yt_stats:
        return 0.0
    views = yt_stats.get("views", 0) or 0
    subs = yt_stats.get("subscribers", 0) or 0
    # log10(5B) ≈ 9.7, log10(100M) ≈ 8. Normalize so 1B views ≈ half the cap.
    view_score = max(0.0, (math.log10(views + 1) - 6.0) / 4.0)   # 0 at 1M, ~0.9 at 10B
    sub_score  = max(0.0, (math.log10(subs + 1) - 5.0) / 3.0)    # 0 at 100k, ~1.0 at 100M
    # Blend and cap.
    raw = 0.6 * view_score + 0.4 * sub_score
    return round(min(YOUTUBE_BOOST_MAX, max(0.0, raw * YOUTUBE_BOOST_MAX)), 4)


def popularity_boost_factor(popularity, prev_popularity=None):
    """Map Spotify popularity (0-100) to a price modifier.

    The popularity score reflects recent streaming momentum — it updates
    faster than raw listener counts and captures virality. We use it as
    a ±15% swing around the base price:
      - popularity 50 (average) → 0% boost (neutral)
      - popularity 100 (peak) → +15% boost
      - popularity 0 (dead) → -15% penalty
    Plus a momentum kicker: if popularity changed since last run, amplify
    the move to create visible intraday price action.
    """
    pop = max(0, min(100, popularity or 0))
    # Linear map: 0→-0.15, 50→0.0, 100→+0.15
    base_boost = ((pop - 50) / 50) * POPULARITY_BOOST_MAX

    # Momentum: amplify recent popularity changes.
    momentum = 0.0
    if prev_popularity is not None:
        delta = pop - prev_popularity
        momentum = max(-MOMENTUM_CAP, min(MOMENTUM_CAP, delta * MOMENTUM_PER_POINT))

    return round(base_boost + momentum, 4)


def compute_fair_price(popularity, followers, youtube_stats=None, chart_stats=None,
                       monthly_listeners=None, prev_popularity=None):
    # ── Muse Streaming Index formula ──
    # Must stay in sync with the frontend `fairFromListeners()`:
    #   fairPrice = (monthlyListeners × VALUE_PER_LISTENER) / SHARES_OUTSTANDING
    # where VALUE_PER_LISTENER = €0.03 and SHARES_OUTSTANDING = 1 000 000.
    VALUE_PER_LISTENER = 0.03
    SHARES_OUTSTANDING = 1_000_000
    listeners = monthly_listeners if monthly_listeners and monthly_listeners > 0 else int(round((followers or 0) * 0.6))
    base = max(0.01, (listeners * VALUE_PER_LISTENER) / SHARES_OUTSTANDING)
    yt_boost = youtube_boost_factor(youtube_stats)
    ch_boost = chart_boost_factor(chart_stats)
    pop_boost = popularity_boost_factor(popularity, prev_popularity)
    # Boosts stack additively — an artist at #1 on the Global Top 50,
    # with a billion-view YouTube presence, and trending popularity
    # can get up to +70% over their pure Spotify fair price.
    return round(base * (1 + yt_boost + ch_boost + pop_boost), 2)


def blend_price(fair, previous):
    if previous is None:
        return fair
    # If previous price is more than 5× away from fair in either direction,
    # the old value is from a stale/different formula — snap to fair immediately
    if previous > 0 and fair > 0:
        ratio = previous / fair
        if ratio > 5 or ratio < 0.2:
            return fair
    return round(previous * 0.50 + fair * 0.50, 2)


def load_previous_prices():
    if not OUT_FILE.exists():
        return {}
    try:
        data = json.loads(OUT_FILE.read_text())
        return {a["ticker"]: a for a in data.get("artists", [])}
    except Exception:
        return {}


def load_history():
    """Load the rolling price history. Shape: { ticker: [{t, p}, …] }."""
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return {}


def parse_iso(ts):
    """Parse an ISO8601 timestamp (with trailing Z) into an aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def append_history(history, ticker, timestamp, price, now_dt, listeners=None, popularity=None):
    """Append one point to a ticker's series, trimming to HISTORY_MAX_POINTS.

    Each point carries the raw streaming fundamentals (`listeners`,
    `popularity`) so the frontend can re-derive a fair price from the
    actual metrics we observed at that moment — not just a cached
    number that may have been computed with a different formula.

    If the most recent existing point is within HISTORY_DEDUP_SECONDS, we
    overwrite it instead of appending — this prevents rapid manual reruns
    from stuffing the rolling window with near-duplicate points.
    """
    point = {"t": timestamp, "p": price}
    if listeners is not None:
        point["listeners"] = listeners
    if popularity is not None:
        point["pop"] = popularity
    series = history.get(ticker, [])
    if series:
        last = series[-1]
        last_dt = parse_iso(last.get("t", ""))
        if last_dt and (now_dt - last_dt).total_seconds() < HISTORY_DEDUP_SECONDS:
            series[-1] = point
            history[ticker] = series
            return history
    series.append(point)
    if len(series) > HISTORY_MAX_POINTS:
        series = series[-HISTORY_MAX_POINTS:]
    history[ticker] = series
    return history


def price_24h_ago(series, now_dt):
    """Walk a ticker's history backwards looking for the most recent point
    that's at least 24h old. Returns the price at that point, or None if we
    don't have enough history yet."""
    if not series:
        return None
    cutoff = now_dt - timedelta(hours=24)
    for point in reversed(series):
        pt_dt = parse_iso(point.get("t", ""))
        if pt_dt and pt_dt <= cutoff:
            return point.get("p")
    # Fall back to the oldest point we have — gives a partial-day estimate
    # until history fills out past 24h.
    oldest = series[0]
    return oldest.get("p") if oldest else None


def compute_volatility(series, window=30):
    """Sample stdev of the last `window` prices. 0.0 if <2 points available."""
    if not series or len(series) < 2:
        return 0.0
    prices = [pt.get("p") for pt in series[-window:] if isinstance(pt.get("p"), (int, float))]
    if len(prices) < 2:
        return 0.0
    try:
        return round(statistics.pstdev(prices), 3)
    except Exception:
        return 0.0


def compute_volume(popularity, followers, pop_delta):
    """Follower-weighted volume proxy. Big artists with changing popularity
    show heavier volume; stable niches trade thin. Tuned so stadium acts
    during a viral moment hit ~50k and a quiet indie sits near ~200."""
    if followers <= 0:
        return 0
    base = math.sqrt(followers) * 0.6
    movement_factor = 1 + min(5.0, abs(pop_delta) * 0.8)
    return int(round(base * movement_factor))


def load_baseline():
    """Returns (rawAverage, rosterSize) or (None, None) if no baseline yet."""
    if not BASELINE_FILE.exists():
        return None, None
    try:
        data = json.loads(BASELINE_FILE.read_text())
        return data.get("rawAverage"), data.get("rosterSize")
    except Exception:
        return None, None


def save_baseline(raw_average, roster_size):
    try:
        BASELINE_FILE.write_text(json.dumps({
            "rawAverage": raw_average,
            "rosterSize": roster_size,
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "note": "Market index is normalized so that on this day the Muse Index = 1000. "
                    "Automatically rebased when rosterSize changes.",
        }, indent=2))
    except Exception:
        pass


def compute_sector_indices(out_artists):
    """Group artists by genre and return [{sector, avgPrice, chg24h, members}]."""
    by_sector = {}
    for a in out_artists:
        by_sector.setdefault(a["genre"], []).append(a)
    sectors = []
    for sector, members in sorted(by_sector.items()):
        avg = sum(m["price"] for m in members) / len(members)
        chg_values = [m["chg24h"] for m in members if m.get("chg24h") is not None]
        avg_chg = sum(chg_values) / len(chg_values) if chg_values else 0.0
        sectors.append({
            "sector": sector,
            "avgPrice": round(avg, 2),
            "chg24h": round(avg_chg, 2),
            "members": [m["ticker"] for m in members],
        })
    return sectors


def main():
    global ARTISTS
    load_dotenv()
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            "✗ Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET.\n"
            f"  Expected them in {HERE / '.env'} or as environment variables."
        )

    ARTISTS = load_artists_config()
    print(f"Loaded {len(ARTISTS)} artists from {ARTISTS_FILE.name}")

    token = get_access_token(client_id, client_secret)

    # First-run bootstrap: resolve any artist that doesn't have a spotifyId.
    resolved = resolve_missing_spotify_ids(ARTISTS, token)
    if resolved:
        print(f"  · resolved {resolved} new Spotify IDs and wrote them back to {ARTISTS_FILE.name}")

    print(f"Fetching Spotify data for {len([a for a in ARTISTS if a.get('spotifyId')])} artists…")
    spotify_data = fetch_all_artists(token)

    # Scrape real monthly listener counts from open.spotify.com artist
    # pages. The Web API doesn't expose this number, so we pull it from
    # the public artist page HTML (~1 req per artist, no auth). If the
    # scrape fails entirely (e.g. Cloudflare blocks GitHub Actions IPs),
    # we fall back to the follower-based proxy so the pipeline keeps running.
    monthly_listeners_by_id = {}
    try:
        print("Scraping real monthly listener counts from open.spotify.com…")
        listener_ids = [a["spotifyId"] for a in ARTISTS if a.get("spotifyId")]
        monthly_listeners_by_id = fetch_all_monthly_listeners(listener_ids)
        scraped_ok = len(monthly_listeners_by_id)
        print(f"  · scraped monthly listeners for {scraped_ok}/{len(listener_ids)} artists")
    except Exception as e:
        print(f"  ! monthly-listener scraping failed entirely: {e}")
        print("  · falling back to follower-based proxy for all artists")

    # Load per-artist listener/follower calibration ratios. These were
    # computed once from real Spotify monthly-listener data and allow
    # accurate estimates when the real scrape fails (datacenter IP blocks).
    listener_ratios = {}
    default_listener_ratio = 0.6  # safe fallback if file is missing
    try:
        with open(LISTENER_RATIOS_FILE) as f:
            cal = json.load(f)
        listener_ratios = cal.get("ratios", {})
        default_listener_ratio = cal.get("defaultRatio", 0.6)
        print(f"  · loaded calibration ratios for {len(listener_ratios)} artists "
              f"(default ratio: {default_listener_ratio})")
    except FileNotFoundError:
        print("  ! listener-ratios.json not found — using flat 0.6 fallback")
    except Exception as e:
        print(f"  ! failed to load listener-ratios.json: {e} — using flat 0.6")

    print("Fetching Spotify editorial chart positions…")
    chart_positions_by_id = fetch_chart_positions(token)
    charted_in_roster = sum(
        1 for a in ARTISTS if chart_positions_by_id.get(a.get("spotifyId"))
    )
    print(f"  · {charted_in_roster}/{len(ARTISTS)} of our roster is currently on at least one chart")

    youtube_key = os.environ.get("YOUTUBE_API_KEY")
    if youtube_key:
        print("Fetching YouTube channel statistics…")
        youtube_data = fetch_all_youtube(youtube_key)
        print(f"  · YouTube stats for {len(youtube_data)}/{len(ARTISTS)} artists")
    else:
        youtube_data = {}
        print("· YOUTUBE_API_KEY not set, skipping YouTube signal")

    previous = load_previous_prices()
    history = load_history()
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    out_artists = []
    for a in ARTISTS:
        live = spotify_data.get(a["spotifyId"])
        if not live:
            print(f"  ! missing data for {a['ticker']} ({a['name']}), skipping")
            continue
        popularity = live.get("popularity", 0)
        followers = (live.get("followers") or {}).get("total", 0)
        image = (live.get("images") or [{}])[0].get("url") if live.get("images") else None

        # Real monthly listeners scraped from open.spotify.com. If the
        # scrape failed for this artist, estimate from followers using a
        # per-artist calibrated ratio (scraped once from real data) or
        # fall back to the overall median ratio.
        scraped_listeners = monthly_listeners_by_id.get(a["spotifyId"])
        if scraped_listeners and scraped_listeners > 0:
            monthly_listeners = scraped_listeners
            listeners_source = "spotify-page"
        else:
            ratio = listener_ratios.get(a["spotifyId"], default_listener_ratio)
            monthly_listeners = int(round(followers * ratio))
            listeners_source = "calibrated-proxy"

        yt_stats = youtube_data.get(a["ticker"])
        chart_stats = chart_positions_by_id.get(a["spotifyId"])
        prev_entry = previous.get(a["ticker"]) or {}
        prev_price = prev_entry.get("price")
        prev_popularity = prev_entry.get("popularity", popularity)
        fair = compute_fair_price(popularity, followers, yt_stats, chart_stats,
                                  monthly_listeners=monthly_listeners,
                                  prev_popularity=prev_popularity)
        price = blend_price(fair, prev_price)

        # Append before we compute chg24h / volatility so the point we just
        # wrote is reflected in both. The de-dup logic inside append_history
        # ensures rapid reruns overwrite instead of stacking. We store the
        # REAL monthly listeners on the history point so the client can
        # re-derive fair price from it later via the market-cap formula.
        append_history(history, a["ticker"], now_iso, price, now_dt,
                       listeners=monthly_listeners, popularity=popularity)
        series = history.get(a["ticker"], [])

        # Real 24h change from rolling history, not from the last run (which
        # could be minutes ago). Falls back to oldest-point comparison until
        # we have ≥24h of history.
        ref_price = price_24h_ago(series, now_dt)
        if ref_price and ref_price > 0:
            chg = round(((price - ref_price) / ref_price) * 100, 2)
        else:
            chg = 0.0

        volatility = compute_volatility(series)
        pop_delta = popularity - prev_popularity
        volume = compute_volume(popularity, followers, pop_delta)

        out_artists.append({
            "ticker": a["ticker"],
            "name": a["name"],
            "genre": a["genre"],
            "spotifyId": a["spotifyId"],
            "popularity": popularity,
            "popularityDelta": pop_delta,
            "followers": followers,
            "monthlyListeners": monthly_listeners,
            "listenersSource": listeners_source,
            "image": image,
            "fairPrice": fair,
            "price": price,
            "chg24h": chg,
            "volatility30d": volatility,
            "volume": volume,
            "youtube": yt_stats or None,
            "youtubeBoost": youtube_boost_factor(yt_stats) if yt_stats else 0.0,
            "chartPositions": chart_stats or None,
            "chartBoost": chart_boost_factor(chart_stats),
        })

    if not out_artists:
        sys.exit("✗ No artist data returned from Spotify.")

    # ---- Market index: normalize to Muse 1000 baseline ----
    # When the artist roster changes size (e.g. we go from 24 → 105), the old
    # baseline is no longer comparable, so we rebase to 1000 on the new roster.
    raw_average = sum(a["price"] for a in out_artists) / len(out_artists)
    baseline, baseline_roster = load_baseline()
    if (baseline is None or baseline <= 0
            or baseline_roster != len(out_artists)):
        if baseline_roster and baseline_roster != len(out_artists):
            print(f"  · roster changed ({baseline_roster} → {len(out_artists)}), rebasing Muse Index")
        baseline = raw_average
        save_baseline(baseline, len(out_artists))
    market_index = round(MUSE_INDEX_BASELINE * raw_average / baseline, 2)

    # ---- Gainers / losers by real 24h change ----
    sorted_by_chg = sorted(out_artists, key=lambda x: x["chg24h"], reverse=True)
    top_gainers = [{"ticker": a["ticker"], "chg24h": a["chg24h"]} for a in sorted_by_chg[:5]]
    top_losers  = [{"ticker": a["ticker"], "chg24h": a["chg24h"]} for a in sorted_by_chg[-5:][::-1]]

    # ---- Sector indices ----
    sector_indices = compute_sector_indices(out_artists)

    payload = {
        "updatedAt": now_iso,
        "marketIndex": market_index,
        "rawAveragePrice": round(raw_average, 2),
        "topGainers": top_gainers,
        "topLosers": top_losers,
        "sectorIndices": sector_indices,
        "artists": out_artists,
    }

    OUT_FILE.write_text(json.dumps(payload, indent=2))
    HISTORY_FILE.write_text(json.dumps(history, separators=(",", ":")))
    # ALSO write JS-global versions so the prototype can work when opened
    # via file:// (where fetch() is blocked in Safari/Chrome). These files
    # are loaded with <script src="prices.js"> instead of fetch().
    OUT_JS_FILE.write_text(
        "window.__MUSE_PRICES = " + json.dumps(payload) + ";\n",
        encoding="utf-8",
    )
    HISTORY_JS_FILE.write_text(
        "window.__MUSE_HISTORY = " + json.dumps(history, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    total_points = sum(len(v) for v in history.values())

    # ---- Pretty terminal summary ----
    print()
    print(f"✓ wrote {OUT_FILE.name}  ({len(out_artists)} artists)")
    print(f"✓ wrote {HISTORY_FILE.name} ({total_points} points total)")
    print()
    print(f"  Muse Index: {market_index:>8.2f}   (raw avg ${raw_average:.2f}, baseline ${baseline:.2f})")
    real_chg_artists = [a for a in out_artists if a["chg24h"] != 0.0]
    if real_chg_artists:
        print()
        print("  Top gainers (24h):")
        for a in sorted_by_chg[:3]:
            if a["chg24h"] <= 0:
                break
            print(f"    {a['ticker']:<5} {a['chg24h']:+6.2f}%   ${a['price']:<7.2f}  {a['name']}")
        print("  Top losers (24h):")
        for a in sorted_by_chg[::-1][:3]:
            if a["chg24h"] >= 0:
                break
            print(f"    {a['ticker']:<5} {a['chg24h']:+6.2f}%   ${a['price']:<7.2f}  {a['name']}")
    else:
        print("  (no 24h change yet — gainers/losers kick in once history is ≥24h old)")
    print()
    charted = [a for a in out_artists if a.get("chartPositions")]
    if charted:
        print()
        print(f"  Charted artists: {len(charted)}/{len(out_artists)} — showing top 5 by chart boost:")
        for a in sorted(charted, key=lambda x: -x["chartBoost"])[:5]:
            pos = a["chartPositions"]
            parts = [f"{k}=#{v}" for k, v in pos.items()]
            print(f"    {a['ticker']:<5} boost +{a['chartBoost']*100:4.1f}%   {' · '.join(parts)}   {a['name']}")
    print()
    print("  Sector indices:")
    for s in sector_indices:
        print(f"    {s['sector']:<9} ${s['avgPrice']:<7.2f}  ({s['chg24h']:+5.2f}%)  "
              f"[{len(s['members'])} artists]")


if __name__ == "__main__":
    main()
