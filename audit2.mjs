import fs from 'fs';

// Read all files
const artists = JSON.parse(fs.readFileSync('./artists.json', 'utf8'));
const listenerRatios = JSON.parse(fs.readFileSync('./listener-ratios.json', 'utf8'));
const prices = JSON.parse(fs.readFileSync('./prices.json', 'utf8'));
const youtubeChannels = JSON.parse(fs.readFileSync('./youtube-channels.json', 'utf8'));

console.log("=== MUSE PRICES DATA AUDIT REPORT ===\n");

// ARTISTS.JSON AUDIT
console.log("1. ARTISTS.JSON AUDIT");
console.log("=====================");

const spotifyIds = new Map();
const tickers = new Map();
let artistIssues = [];

artists.forEach((artist, idx) => {
  const lineNum = idx + 2;
  
  // Check required fields
  if (!artist.name || !artist.ticker || !artist.spotifyId || !artist.genre) {
    artistIssues.push(`Line ${lineNum}: Missing required field(s) in ${artist.name || 'unknown'}`);
  }
  
  // Check for duplicate Spotify IDs
  if (spotifyIds.has(artist.spotifyId)) {
    artistIssues.push(`Line ${lineNum}: Duplicate Spotify ID "${artist.spotifyId}" for "${artist.name}" (also at line ${spotifyIds.get(artist.spotifyId).line} for "${spotifyIds.get(artist.spotifyId).name}")`);
  } else {
    spotifyIds.set(artist.spotifyId, { line: lineNum, name: artist.name });
  }
  
  // Check for duplicate tickers
  if (tickers.has(artist.ticker)) {
    artistIssues.push(`Line ${lineNum}: Duplicate ticker "${artist.ticker}" for "${artist.name}" (also at line ${tickers.get(artist.ticker).line} for "${tickers.get(artist.ticker).name}")`);
  } else {
    tickers.set(artist.ticker, { line: lineNum, name: artist.name });
  }
  
  // Check Spotify ID format (should be 22 chars alphanumeric)
  if (artist.spotifyId && (artist.spotifyId.length !== 22 || !/^[a-zA-Z0-9]+$/.test(artist.spotifyId))) {
    artistIssues.push(`Line ${lineNum}: Invalid Spotify ID format "${artist.spotifyId}" for "${artist.name}"`);
  }
});

console.log(`Total artists: ${artists.length}`);
if (artistIssues.length === 0) {
  console.log("✓ Status: CLEAN\n");
} else {
  console.log(`✗ Status: ${artistIssues.length} ISSUE(S) FOUND\n`);
  artistIssues.forEach(issue => console.log(`  ${issue}`));
  console.log();
}

// LISTENER-RATIOS.JSON AUDIT
console.log("2. LISTENER-RATIOS.JSON AUDIT");
console.log("==============================");

const ratios = listenerRatios.ratios;
let ratioIssues = [];

// Check for artists in ratios but not in artists.json
const artistSpotifyIds = new Set(artists.map(a => a.spotifyId));

Object.entries(ratios).forEach(([spotifyId, ratio]) => {
  if (!artistSpotifyIds.has(spotifyId)) {
    ratioIssues.push(`Spotify ID "${spotifyId}" in ratios but NOT in artists.json`);
  }
  
  // Check if ratio is outside reasonable range (0.1 - 50)
  if (ratio < 0.1 || ratio > 50) {
    const artist = artists.find(a => a.spotifyId === spotifyId);
    const name = artist ? artist.name : 'Unknown';
    ratioIssues.push(`Ratio ${ratio} for "${name}" (${spotifyId}) is outside normal range [0.1-50]`);
  }
});

// Check for artists in artists.json but not in ratios
const missingRatios = [];
artists.forEach(artist => {
  if (!ratios.hasOwnProperty(artist.spotifyId)) {
    missingRatios.push(`"${artist.name}" (${artist.spotifyId})`);
  }
});

if (missingRatios.length > 0) {
  ratioIssues.push(`Missing ratios for ${missingRatios.length} artist(s): ${missingRatios.join(', ')}`);
}

console.log(`Total ratio entries: ${Object.keys(ratios).length}`);
console.log(`Artists in artists.json: ${artists.length}`);
console.log(`Calibrated at: ${listenerRatios.calibratedAt}`);
console.log(`Default ratio: ${listenerRatios.defaultRatio}`);
if (ratioIssues.length === 0) {
  console.log("✓ Status: CLEAN\n");
} else {
  console.log(`✗ Status: ${ratioIssues.length} ISSUE(S) FOUND\n`);
  ratioIssues.forEach(issue => console.log(`  ${issue}`));
  console.log();
}

// PRICES.JSON AUDIT
console.log("3. PRICES.JSON AUDIT");
console.log("====================");

let priceIssues = [];
let zeroOrNegPrices = [];
let missingArtists = [];
let extraTickers = [];

// Get all tickers in prices (from all sections)
const priceTickersSet = new Set();

// Check topGainers and topLosers for issues
if (prices.topGainers && Array.isArray(prices.topGainers)) {
  prices.topGainers.forEach((item, idx) => {
    priceTickersSet.add(item.ticker);
    const artist = artists.find(a => a.ticker === item.ticker);
    if (!artist) {
      priceIssues.push(`topGainers[${idx}]: ticker "${item.ticker}" not found in artists.json`);
    }
  });
}

if (prices.topLosers && Array.isArray(prices.topLosers)) {
  prices.topLosers.forEach((item, idx) => {
    priceTickersSet.add(item.ticker);
    const artist = artists.find(a => a.ticker === item.ticker);
    if (!artist) {
      priceIssues.push(`topLosers[${idx}]: ticker "${item.ticker}" not found in artists.json`);
    }
  });
}

// Check sectorIndices for artist membership issues
if (prices.sectorIndices && Array.isArray(prices.sectorIndices)) {
  prices.sectorIndices.forEach((sector, idx) => {
    if (sector.members && Array.isArray(sector.members)) {
      sector.members.forEach(ticker => {
        priceTickersSet.add(ticker);
        const artist = artists.find(a => a.ticker === ticker);
        if (!artist) {
          priceIssues.push(`sectorIndices[${idx}] (${sector.sector}): member ticker "${ticker}" not found in artists.json`);
        }
      });
    }
    
    // Check for negative or zero avgPrice
    if (sector.avgPrice <= 0) {
      priceIssues.push(`sectorIndices[${idx}] (${sector.sector}): avgPrice = ${sector.avgPrice} (expected positive)`);
    }
  });
}

// Check if all artists have an entry somewhere
const artistTickersSet = new Set(artists.map(a => a.ticker));
for (const ticker of priceTickersSet) {
  if (!artistTickersSet.has(ticker)) {
    extraTickers.push(ticker);
  }
}

// Check for missing artists
for (const ticker of artistTickersSet) {
  if (!priceTickersSet.has(ticker)) {
    const artist = artists.find(a => a.ticker === ticker);
    missingArtists.push(`"${artist.name}" (${ticker})`);
  }
}

console.log(`Updated at: ${prices.updatedAt}`);
console.log(`Market index: ${prices.marketIndex}`);
console.log(`Raw average price: ${prices.rawAveragePrice}`);
console.log(`Total artists in dataset: ${artists.length}`);
console.log(`Unique tickers in prices: ${priceTickersSet.size}`);

if (missingArtists.length > 0) {
  priceIssues.push(`Missing ${missingArtists.length} artist(s) from prices: ${missingArtists.slice(0, 5).join(', ')}${missingArtists.length > 5 ? '...' : ''}`);
}

if (extraTickers.length > 0) {
  priceIssues.push(`Found ${extraTickers.length} extra ticker(s) in prices not in artists.json: ${extraTickers.join(', ')}`);
}

if (priceIssues.length === 0) {
  console.log("✓ Status: CLEAN\n");
} else {
  console.log(`✗ Status: ${priceIssues.length} ISSUE(S) FOUND\n`);
  priceIssues.forEach(issue => console.log(`  ${issue}`));
  console.log();
}

// YOUTUBE-CHANNELS.JSON AUDIT
console.log("4. YOUTUBE-CHANNELS.JSON AUDIT");
console.log("===============================");

let youtubeIssues = [];
const youtubeChannelIds = new Map();
let missingYoutube = [];
let extraYoutube = [];

// Check for YouTube entries without corresponding artists
Object.entries(youtubeChannels).forEach(([ticker, channel]) => {
  const artist = artists.find(a => a.ticker === ticker);
  if (!artist) {
    youtubeIssues.push(`YouTube ticker "${ticker}" has no corresponding artist in artists.json`);
  }
  
  // Check for duplicate channel IDs
  if (youtubeChannelIds.has(channel.channelId)) {
    youtubeIssues.push(`Duplicate YouTube channel ID "${channel.channelId}" for tickers ${youtubeChannelIds.get(channel.channelId)} and ${ticker}`);
  } else {
    youtubeChannelIds.set(channel.channelId, ticker);
  }
});

// Check for artists without YouTube entries
artists.forEach(artist => {
  if (!youtubeChannels.hasOwnProperty(artist.ticker)) {
    missingYoutube.push(`"${artist.name}" (${artist.ticker})`);
  }
});

console.log(`Total YouTube entries: ${Object.keys(youtubeChannels).length}`);
console.log(`Artists in artists.json: ${artists.length}`);

if (missingYoutube.length > 0) {
  youtubeIssues.push(`Missing YouTube entries for ${missingYoutube.length} artist(s): ${missingYoutube.slice(0, 3).join(', ')}${missingYoutube.length > 3 ? '...' : ''}`);
}

if (youtubeIssues.length === 0) {
  console.log("✓ Status: CLEAN\n");
} else {
  console.log(`✗ Status: ${youtubeIssues.length} ISSUE(S) FOUND\n`);
  youtubeIssues.forEach(issue => console.log(`  ${issue}`));
  console.log();
}

// SUMMARY
console.log("=== FINAL SUMMARY ===");
const totalIssues = artistIssues.length + ratioIssues.length + priceIssues.length + youtubeIssues.length;
if (totalIssues === 0) {
  console.log("✓ ALL AUDITS PASSED - DATA INTEGRITY VERIFIED!\n");
} else {
  console.log(`✗ TOTAL ISSUES FOUND: ${totalIssues}\n`);
  console.log("Issue breakdown:");
  if (artistIssues.length > 0) console.log(`  - Artists.json: ${artistIssues.length} issue(s)`);
  if (ratioIssues.length > 0) console.log(`  - Listener-ratios.json: ${ratioIssues.length} issue(s)`);
  if (priceIssues.length > 0) console.log(`  - Prices.json: ${priceIssues.length} issue(s)`);
  if (youtubeIssues.length > 0) console.log(`  - YouTube-channels.json: ${youtubeIssues.length} issue(s)`);
}
