#!/usr/bin/env node
/**
 * Muse — price fetcher
 *
 * Pulls artist metrics from the Spotify Web API (Client Credentials flow,
 * no user login), computes a synthetic "stock price" for each tracked
 * artist, blends with yesterday's close, and writes prices.json.
 *
 * Intended to run on a cron (every 4–6 hours) via GitHub Actions or any
 * other scheduled runtime.
 *
 * Environment:
 *   SPOTIFY_CLIENT_ID      — from https://developer.spotify.com/dashboard
 *   SPOTIFY_CLIENT_SECRET  — same dashboard
 *
 * Output: ./prices.json (replaces existing file each run)
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PRICES_PATH = path.join(__dirname, 'prices.json');

const { SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET } = process.env;
if (!SPOTIFY_CLIENT_ID || !SPOTIFY_CLIENT_SECRET) {
  console.error('Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET env vars.');
  console.error('Get them from https://developer.spotify.com/dashboard');
  process.exit(1);
}

/**
 * Tracked artists. Spotify IDs are stable — pulled from the share URL,
 * e.g. https://open.spotify.com/artist/74KM79TiuVKeVCqs8QtB0B → 74KM79TiuVKeVCqs8QtB0B
 *
 * Add new artists by appending to this list. Ticker must be unique and
 * ≤ 5 chars to fit the UI.
 */
const ARTISTS = [
  { ticker: 'SABR', name: 'Sabrina Carpenter', spotifyId: '74KM79TiuVKeVCqs8QtB0B', genre: 'Pop' },
  { ticker: 'BNNY', name: 'Bad Bunny',         spotifyId: '4q3ewBCX7sLwd24euuV69X', genre: 'Latin' },
  { ticker: 'CHPL', name: 'Chappell Roan',     spotifyId: '7GlBOeep6PqTfFi59PTUUN', genre: 'Pop' },
  { ticker: 'TSWF', name: 'Taylor Swift',      spotifyId: '06HL4z0CvFAxyc27GXpf02', genre: 'Pop' },
  { ticker: 'WKND', name: 'The Weeknd',        spotifyId: '1Xyo4u8uXC1ZmMpatF05PJ', genre: 'R&B' },
  { ticker: 'TYLA', name: 'Tyla',              spotifyId: '3f5IlzRdqvQpotQ1CJGzNC', genre: 'Afropop' },
  { ticker: 'PESO', name: 'Peso Pluma',        spotifyId: '12GqGscKJx3aE4t07u7eVZ', genre: 'Latin' },
  { ticker: 'OLVR', name: 'Olivia Rodrigo',    spotifyId: '1McMsnEElThX1knmY4oliG', genre: 'Pop' },
  { ticker: 'DRKE', name: 'Drake',             spotifyId: '3TVXtAsR1Inumwj472S9r4', genre: 'Hip-hop' },
  { ticker: 'BILL', name: 'Billie Eilish',     spotifyId: '6qqNVTkY8uBg9cP3Jd7DAH', genre: 'Alt' },
  { ticker: 'TATE', name: 'Tate McRae',        spotifyId: '45dkTj5sMRSjrmBSBeiHym', genre: 'Pop' },
  { ticker: 'ROSA', name: 'Rosalía',           spotifyId: '7ltDVBr6mKbRvohxheJ9h1', genre: 'Latin' },
  { ticker: 'TRVS', name: 'Travis Scott',      spotifyId: '0Y5tJX1MQlPlqiwlOH1tJY', genre: 'Hip-hop' },
  { ticker: 'KROL', name: 'Karol G',           spotifyId: '790FomKkXshlbRYZFtlgla', genre: 'Latin' },
  { ticker: 'LANA', name: 'Lana Del Rey',      spotifyId: '00FQb4jTyendYWaN8pK0wa', genre: 'Alt' },
  { ticker: 'ICE',  name: 'Ice Spice',         spotifyId: '3LZZPxNDGDFVSIPqf4JuEf', genre: 'Hip-hop' },
  { ticker: 'DUAL', name: 'Dua Lipa',          spotifyId: '6M2wZ9GZgrQXHCFfjv46we', genre: 'Pop' },
  { ticker: 'REMA', name: 'Rema',              spotifyId: '46pWGuE3dSwY3bMMXGBvVS', genre: 'Afropop' },
  { ticker: 'MARI', name: 'The Marías',        spotifyId: '2R21vXR83lH98kGeO99Y66', genre: 'Indie' },
  { ticker: 'CLRO', name: 'Clairo',            spotifyId: '3l0CmX0FuQjFxr8SK7Vqag', genre: 'Indie' },
  { ticker: 'KDOT', name: 'Kendrick Lamar',    spotifyId: '2YZyLoL8N0Wb9xBt1NhZWg', genre: 'Hip-hop' },
  { ticker: 'SZA',  name: 'SZA',               spotifyId: '7tYKF4w9nC0nq9CsPZTHyP', genre: 'R&B' },
  { ticker: 'AYRA', name: 'Ayra Starr',        spotifyId: '3ZpEKRjHaHANcpk10u6Ntq', genre: 'Afropop' },
  { ticker: 'BEAB', name: 'Beabadoobee',       spotifyId: '35l9BRT7MXmM8bv2WDQiyB', genre: 'Indie' },
];

/* -------------------------------------------------------------------- */
/*  Spotify auth                                                        */
/* -------------------------------------------------------------------- */

async function getAccessToken() {
  const creds = Buffer.from(`${SPOTIFY_CLIENT_ID}:${SPOTIFY_CLIENT_SECRET}`).toString('base64');
  const res = await fetch('https://accounts.spotify.com/api/token', {
    method: 'POST',
    headers: {
      'Authorization': `Basic ${creds}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: 'grant_type=client_credentials',
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Spotify token request failed: ${res.status} ${body}`);
  }
  const data = await res.json();
  return data.access_token;
}

async function fetchArtists(token) {
  // /v1/artists accepts up to 50 ids in one request — plenty of headroom.
  const ids = ARTISTS.map(a => a.spotifyId).join(',');
  const res = await fetch(`https://api.spotify.com/v1/artists?ids=${ids}`, {
    headers: { 'Authorization': `Bearer ${token}` },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Spotify artists request failed: ${res.status} ${body}`);
  }
  const data = await res.json();
  return data.artists;
}

/* -------------------------------------------------------------------- */
/*  Pricing                                                             */
/* -------------------------------------------------------------------- */

/**
 * Convert Spotify's raw metrics into a synthetic "fair price" in USD.
 *
 * The formula is deliberately simple — it should produce prices that
 * *feel* like a stock chart for a given artist. Tune the weights as you
 * watch how the market moves.
 *
 *   base           = 8 × popularity ^ 1.35     (dominates — popularity
 *                                               is Spotify's own
 *                                               recency-weighted score)
 *   followerBonus  = log10(followers + 1) × 5  (long-tail loyalty)
 *
 * Typical output range: ~$40 for niche artists up to ~$550 for
 * stadium-level pop stars.
 */
function computeFairPrice(artist) {
  const pop = artist.popularity || 0;
  const followers = artist.followers?.total || 0;
  // Spotify popularity is 0–100, but almost everyone we track is 60+.
  // Subtract a 30-point floor and exponentiate the headroom so mid-tier
  // artists land around $150–$300 and stadium acts land around $500–$700.
  const headroom = Math.max(0, pop - 30);
  const base = 0.8 * Math.pow(headroom, 1.5);
  const followerBonus = Math.log10(followers + 1) * 3;
  return Number((base + followerBonus).toFixed(2));
}

/**
 * Smooth today's fair price against yesterday's close so the market
 * doesn't leap every time we poll. 15% weight on new data ≈ 1-week
 * half-life. Tune downward for slower markets, upward for more drama.
 */
function blendPrice(fair, previous) {
  if (!previous) return fair;
  return Number((previous * 0.85 + fair * 0.15).toFixed(2));
}

/* -------------------------------------------------------------------- */
/*  Main                                                                */
/* -------------------------------------------------------------------- */

async function main() {
  console.log(`Fetching data for ${ARTISTS.length} artists from Spotify…`);
  const token = await getAccessToken();
  const spotifyArtists = await fetchArtists(token);
  const byId = Object.fromEntries(spotifyArtists.filter(Boolean).map(a => [a.id, a]));

  // Load previous prices (if any) for delta + smoothing.
  let previous = {};
  try {
    const prev = JSON.parse(await fs.readFile(PRICES_PATH, 'utf8'));
    previous = Object.fromEntries(prev.artists.map(a => [a.ticker, a]));
  } catch {
    console.log('No previous prices.json — treating as first run.');
  }

  const now = new Date().toISOString();
  const artists = ARTISTS.map(meta => {
    const sp = byId[meta.spotifyId];
    if (!sp) {
      console.warn(`  ⚠ missing Spotify data for ${meta.name} (${meta.ticker})`);
      return null;
    }
    const fair = computeFairPrice(sp);
    const prev = previous[meta.ticker];
    const price = blendPrice(fair, prev?.price);
    const chg = prev?.price ? ((price - prev.price) / prev.price) * 100 : 0;

    return {
      ticker: meta.ticker,
      name: meta.name,
      genre: meta.genre,
      spotifyId: meta.spotifyId,
      image: sp.images?.[0]?.url || null,
      followers: sp.followers?.total || 0,
      popularity: sp.popularity || 0,
      fairPrice: fair,
      price,
      chg24h: Number(chg.toFixed(2)),
      up: chg >= 0,
      prevPrice: prev?.price ?? null,
      lastUpdated: now,
    };
  }).filter(Boolean);

  // Add some helpful aggregates the frontend can render without recomputing.
  const gainers = [...artists].sort((a, b) => b.chg24h - a.chg24h).slice(0, 5).map(a => a.ticker);
  const losers  = [...artists].sort((a, b) => a.chg24h - b.chg24h).slice(0, 5).map(a => a.ticker);
  const marketIndex = Number(
    (artists.reduce((sum, a) => sum + a.price, 0) / artists.length).toFixed(2)
  );

  const output = {
    updatedAt: now,
    marketIndex,
    topGainers: gainers,
    topLosers: losers,
    artists,
  };

  await fs.writeFile(PRICES_PATH, JSON.stringify(output, null, 2));
  console.log(`✓ Wrote ${PRICES_PATH}`);
  console.log(`  ${artists.length} artists, market index $${marketIndex}`);
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
