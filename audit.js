const fs = require('fs');

// Read all files
const artists = JSON.parse(fs.readFileSync('./artists.json', 'utf8'));
const listenerRatios = JSON.parse(fs.readFileSync('./listener-ratios.json', 'utf8'));
const prices = JSON.parse(fs.readFileSync('./prices.json', 'utf8'));
const youtubeChannels = JSON.parse(fs.readFileSync('./youtube-channels.json', 'utf8'));

console.log("=== AUDIT REPORT ===\n");

// ARTISTS.JSON AUDIT
console.log("1. ARTISTS.JSON AUDIT");
console.log("-------------------");

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
    artistIssues.push(`Line ${lineNum}: Duplicate Spotify ID "${artist.spotifyId}" (also at line ${spotifyIds.get(artist.spotifyId)})`);
  } else {
    spotifyIds.set(artist.spotifyId, lineNum);
  }
  
  // Check for duplicate tickers
  if (tickers.has(artist.ticker)) {
    artistIssues.push(`Line ${lineNum}: Duplicate ticker "${artist.ticker}" (also at line ${tickers.get(artist.ticker)})`);
  } else {
    tickers.set(artist.ticker, lineNum);
  }
  
  // Check Spotify ID format (should be 22 chars alphanumeric)
  if (artist.spotifyId && (artist.spotifyId.length !== 22 || !/^[a-zA-Z0-9]+$/.test(artist.spotifyId))) {
    artistIssues.push(`Line ${lineNum}: Invalid Spotify ID format "${artist.spotifyId}" for ${artist.name}`);
  }
});

console.log(`Total artists: ${artists.length}`);
if (artistIssues.length === 0) {
  console.log("✓ No issues found");
} else {
  console.log(`✗ Found ${artistIssues.length} issue(s):`);
  artistIssues.forEach(issue => console.log(`  - ${issue}`));
}

// LISTENER-RATIOS.JSON AUDIT
console.log("\n2. LISTENER-RATIOS.JSON AUDIT");
console.log("-------------------------------");

const ratios = listenerRatios.ratios;
let ratioIssues = [];

// Check for artists in ratios but not in artists.json
const artistSpotifyIds = new Set(artists.map(a => a.spotifyId));

Object.entries(ratios).forEach(([spotifyId, ratio]) => {
  if (!artistSpotifyIds.has(spotifyId)) {
    ratioIssues.push(`Spotify ID "${spotifyId}" in ratios but not in artists.json`);
  }
  
  // Check if ratio is outside reasonable range (0.1 - 50)
  if (ratio < 0.1 || ratio > 50) {
    const artist = artists.find(a => a.spotifyId === spotifyId);
    const name = artist ? artist.name : 'Unknown';
    ratioIssues.push(`Spotify ID "${spotifyId}" (${name}) has suspicious ratio: ${ratio}`);
  }
});

// Check for artists in artists.json but not in ratios
artists.forEach(artist => {
  if (!ratios.hasOwnProperty(artist.spotifyId)) {
    ratioIssues.push(`Artist "${artist.name}" (${artist.spotifyId}) missing from ratios`);
  }
});

console.log(`Total ratio entries: ${Object.keys(ratios).length}`);
console.log(`Calibrated at: ${listenerRatios.calibratedAt}`);
console.log(`Default ratio: ${listenerRatios.defaultRatio}`);
if (ratioIssues.length === 0) {
  console.log("✓ No issues found");
} else {
  console.log(`✗ Found ${ratioIssues.length} issue(s):`);
  ratioIssues.forEach(issue => console.log(`  - ${issue}`));
}

// PRICES.JSON AUDIT
console.log("\n3. PRICES.JSON AUDIT");
console.log("-------------------");

let priceIssues = [];
let zeroOrNegPrices = [];
let missingFields = [];
let chg24hNonZero = [];

Object.entries(prices).forEach(([ticker, data]) => {
  // Check for missing fields
  if (typeof data !== 'object' || !data.price !== undefined) {
    missingFields.push(`Ticker "${ticker}": Missing or malformed data`);
    return;
  }
  
  // Check for zero or negative prices
  if (data.price <= 0) {
    zeroOrNegPrices.push(`Ticker "${ticker}" (${data.artist || 'unknown'}): price = ${data.price}`);
  }
  
  // Check chg24h (should be 0.0 after reset)
  if (data.chg24h && data.chg24h !== 0.0) {
    chg24hNonZero.push(`Ticker "${ticker}": chg24h = ${data.chg24h} (expected 0.0)`);
  }
});

console.log(`Total price entries: ${Object.keys(prices).length}`);
console.log(`Artists in artists.json: ${artists.length}`);
if (Object.keys(prices).length !== artists.length) {
  priceIssues.push(`Mismatch: ${Object.keys(prices).length} prices vs ${artists.length} artists`);
}

if (zeroOrNegPrices.length > 0) {
  console.log(`\n✗ Found ${zeroOrNegPrices.length} price(s) <= 0:`);
  zeroOrNegPrices.forEach(issue => console.log(`  - ${issue}`));
} else {
  console.log("✓ All prices are positive");
}

if (chg24hNonZero.length > 0) {
  console.log(`\n✗ Found ${chg24hNonZero.length} chg24h value(s) != 0.0:`);
  chg24hNonZero.forEach(issue => console.log(`  - ${issue}`));
} else {
  console.log("✓ All chg24h values are 0.0");
}

if (missingFields.length > 0) {
  console.log(`\n✗ Found ${missingFields.length} missing field(s):`);
  missingFields.forEach(issue => console.log(`  - ${issue}`));
} else {
  console.log("✓ No missing fields");
}

// YOUTUBE-CHANNELS.JSON AUDIT
console.log("\n4. YOUTUBE-CHANNELS.JSON AUDIT");
console.log("-------------------------------");

let youtubeIssues = [];
const youtubeChannelIds = new Map();

// Check for YouTube entries without corresponding artists
Object.entries(youtubeChannels).forEach(([ticker, channel]) => {
  const artist = artists.find(a => a.ticker === ticker);
  if (!artist) {
    youtubeIssues.push(`YouTube ticker "${ticker}" has no corresponding artist in artists.json`);
  }
  
  // Check for duplicate channel IDs
  if (youtubeChannelIds.has(channel.channelId)) {
    youtubeIssues.push(`Duplicate YouTube channel ID "${channel.channelId}" (ticker: ${youtubeChannelIds.get(channel.channelId)}, ${ticker})`);
  } else {
    youtubeChannelIds.set(channel.channelId, ticker);
  }
});

// Check for artists without YouTube entries
artists.forEach(artist => {
  if (!youtubeChannels.hasOwnProperty(artist.ticker)) {
    youtubeIssues.push(`Artist "${artist.name}" (${artist.ticker}) missing from youtube-channels.json`);
  }
});

console.log(`Total YouTube entries: ${Object.keys(youtubeChannels).length}`);
if (youtubeIssues.length === 0) {
  console.log("✓ No issues found");
} else {
  console.log(`✗ Found ${youtubeIssues.length} issue(s):`);
  youtubeIssues.forEach(issue => console.log(`  - ${issue}`));
}

// SUMMARY
console.log("\n=== SUMMARY ===");
const totalIssues = artistIssues.length + ratioIssues.length + zeroOrNegPrices.length + chg24hNonZero.length + missingFields.length + youtubeIssues.length;
if (totalIssues === 0) {
  console.log("✓ All audits passed - data integrity verified!");
} else {
  console.log(`✗ Total issues found: ${totalIssues}`);
}
