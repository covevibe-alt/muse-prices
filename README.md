# Muse — Exchange Backend (MVP)

This folder is the **price engine** for the Muse Exchange prototype. It pulls
artist metrics from Spotify every few hours, turns them into a synthetic
"stock price", and writes `prices.json`. The frontend (the exchange
prototype) just fetches that file — no server, no database.

```
app/
├── fetch-prices.js          ← Spotify fetcher + pricing formula
├── prices.json              ← output (committed, served as a static file)
├── package.json
├── .github/workflows/
│   └── fetch-prices.yml     ← cron: runs every 4 hours on GitHub Actions
└── README.md                ← you are here
```

## How the pricing works

For each tracked artist we pull two numbers from Spotify:

- **`popularity`** — Spotify's own 0–100 score, recency-weighted. This
  already tracks what we care about (are people streaming this artist
  *right now*), so it does most of the work.
- **`followers.total`** — long-tail loyalty, smaller effect.

```
headroom    = max(0, popularity − 30)
spotifyFair = 0.8 × headroom^1.5  +  log10(followers + 1) × 3
```

The 30-point floor is because essentially every artist we track scores 60+
on Spotify's popularity scale. Subtracting it and exponentiating the
headroom spreads mid-tier vs. top-tier artists out instead of squashing
them all into the $3,000–$4,000 range.

Then, **if** `YOUTUBE_API_KEY` is set, we also fetch each artist's channel
statistics (`viewCount`, `subscriberCount`) from the YouTube Data API and
turn them into a boost factor capped at +30%:

```
youtubeBoost = clamp(0.6 × viewScore + 0.4 × subScore, 0, 0.30)
fairPrice    = spotifyFair × (1 + youtubeBoost)
```

Without a YouTube key, `youtubeBoost` is 0 and prices are pure Spotify.
Finally, we smooth against the previous run so prices drift instead of
leaping:

```
price_today = 0.85 × price_yesterday  +  0.15 × fairPrice
```

Typical range: ~$50 for niche artists (popularity ~55) up to ~$700 for
stadium pop stars with heavy YouTube presence. Tune weights in `compute_fair_price` and
`blend_price` (Python) or the equivalents in `fetch-prices.js` (Node).

## One-time setup

### 1. Create a Spotify developer app (free, 2 minutes)

1. Go to <https://developer.spotify.com/dashboard> and log in with any
   Spotify account (free works fine).
2. Click **Create app**.
3. Fill in:
   - **App name:** Muse Exchange (anything is fine)
   - **App description:** Synthetic artist stock market
   - **Redirect URI:** `http://localhost` (we don't use it, but the form
     requires something)
   - **APIs used:** tick **Web API**
4. Accept the terms, click **Save**.
5. On the app page, click **Settings**. Copy:
   - **Client ID**
   - **Client secret** (click "View client secret")

Keep these somewhere safe for the next step.

### 2. Put the app in a GitHub repo

```bash
cd app
git init
git add .
git commit -m "initial commit"
gh repo create muse-exchange --private --source=. --push
```

(Or create the repo on github.com and push manually — either is fine.)

### 3. Add the Spotify credentials as GitHub Actions secrets

1. Open the repo on GitHub → **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret**.
2. Add two secrets:
   - Name: `SPOTIFY_CLIENT_ID`, value: the client ID from step 1
   - Name: `SPOTIFY_CLIENT_SECRET`, value: the client secret from step 1

### 4. Run it once to verify

In the repo on GitHub: **Actions** → **Fetch Muse prices** → **Run
workflow**. Within ~30 seconds you should see a new commit:
`chore: update prices 2026-04-08T17:40Z`.

Open `prices.json` in the repo — the numbers should look like real Spotify
data now (instead of the sample values this file ships with).

That's it. From now on it runs itself every 4 hours.

## Optional: enabling the YouTube signal

The fetcher works with just Spotify. Adding YouTube data makes prices less
dependent on Spotify's opaque `popularity` algorithm and makes them react
to viral YouTube moments. Setup takes about 3 minutes.

### 1. Get a YouTube Data API v3 key

1. Open <https://console.cloud.google.com/>. Sign in with any Google
   account.
2. Create a new project (top bar → project dropdown → **New project** →
   name it "Muse Exchange" → **Create**).
3. Enable the API: go to **APIs & Services → Library**, search for
   **YouTube Data API v3**, click it, click **Enable**.
4. Create credentials: **APIs & Services → Credentials → Create
   credentials → API key**. Copy the key.
5. (Recommended) Click the key you just made and under **API
   restrictions** restrict it to "YouTube Data API v3" only.

### 2. Add the key

**Locally:** open `app/.env` in a text editor and add one line:

```
YOUTUBE_API_KEY=AIzaSy...your-key-here
```

**On GitHub Actions:** add `YOUTUBE_API_KEY` as a third repository secret
alongside your Spotify ones.

### 3. First run is slower (and the second run finishes the job)

The first time the fetcher runs with a YouTube key, it resolves each
artist's YouTube channel ID via a search call (100 quota units each, one
per artist). It caches the results in `youtube-channels.json` so every
subsequent run just hits the cheap stats endpoint (~1 unit per call).

Daily quota budget, free tier: **10,000 units**. With 105 artists the
resolver would need 10,500 units for a full cold start, so it's capped at
80 resolutions per run (8,000 units). The remaining ~25 artists finish on
the next run. No action from you — just wait 4 hours.

After both runs complete, every subsequent run costs ~1 unit. You could
run it every 5 minutes for the rest of time and still stay under the free
tier.

## Running it locally (optional)

```bash
cd app
export SPOTIFY_CLIENT_ID=your_id_here
export SPOTIFY_CLIENT_SECRET=your_secret_here
node fetch-prices.js
```

Needs Node 20+. No `npm install` required — we only use the built-in
`fetch` and `fs/promises`.

## Adding or removing artists

Edit **`artists.json`** — it's the single source of truth for the whole
roster. Each entry needs a ticker, name, and genre. The Spotify ID is
optional: leave it as `""` and the fetcher will auto-resolve it via
Spotify search on the next run, writing it back to the file.

```json
{ "ticker": "NEWA", "name": "New Artist", "genre": "Pop", "spotifyId": "" }
```

Ticker must be unique and ≤ 5 characters (the UI is built around tickers
that size). When the roster size changes (e.g. 24 → 105), the Muse Index
automatically rebases to 1000 on the new composition.

## Where the prototype reads this file

The exchange prototype (`Muse - Exchange Prototype.html`) is wired to
`fetch()` the `prices.json` from this repo. Once the repo is on GitHub
you can point it at the raw URL, e.g.:

```
https://raw.githubusercontent.com/<you>/muse-exchange/main/prices.json
```

…or, if you prefer, copy `prices.json` into the same folder as the
prototype after each fetch. Both approaches work — the prototype just
needs *a* JSON file in that shape.

## Troubleshooting

**"Spotify token request failed: 400 invalid_client"** — double-check the
secrets in GitHub Actions. No quotes, no leading spaces.

**"missing Spotify data for …"** — the Spotify ID in `ARTISTS` is wrong
or the artist was removed. Re-copy the ID from the artist's share link.

**Prices barely move between runs** — that's by design. Spotify's
`popularity` score updates slowly and we smooth on top of that. If you
want more drama, raise the `0.15` in `blendPrice()` toward `0.4` or so.

**First run wipes my sample prices.json** — yes. The sample file only
exists to unblock frontend work before your Spotify credentials are
ready; the first real run replaces it.
