// Nightly on-chain data pull for the BTC On-Chain dashboard.
// Usage: node scripts/fetch.mjs <outDir>
// Writes <outDir>/meta.json and <outDir>/m/<id>.json ({cols:[...], rows:[[day, v1, v2...]]}, day = unix days).
// Sources (all free, no keys):
//   Coin Metrics Community API  — daily network data, fresh, generous limits
//   BGeometrics (bitcoin-data.com) — cohort metrics computed from a full node; free = 10/h, 15/day,
//                                    so each run fetches the next BG_PER_RUN endpoints in a rotating cursor
//   mempool.space               — mempool, fees, difficulty, hashrate, pools (snapshot)
import fs from "node:fs";
import path from "node:path";

const OUT = process.argv[2] || "out";
const BG_PER_RUN = Number(process.env.BG_PER_RUN || 7);
fs.mkdirSync(path.join(OUT, "m"), { recursive: true });
const metaPath = path.join(OUT, "meta.json");
const meta = fs.existsSync(metaPath) ? JSON.parse(fs.readFileSync(metaPath, "utf8")) : {};
meta.metrics ||= {};
meta.bgCursor ||= 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = new Date().toISOString();

const round = (v) => (v == null || !isFinite(v) ? null : Number(Number(v).toPrecision(7)));
const day = (t) => Math.floor((typeof t === "number" ? (t > 1e11 ? t : t * 1000) : Date.parse(t)) / 864e5);

function save(id, cols, rows, src, note) {
  rows = rows.filter((r) => r.slice(1).some((v) => v != null)).sort((a, b) => a[0] - b[0]);
  const dedup = [];
  for (const r of rows) (dedup.length && dedup.at(-1)[0] === r[0]) ? (dedup[dedup.length - 1] = r) : dedup.push(r);
  if (!dedup.length) throw new Error("no rows");
  fs.writeFileSync(path.join(OUT, "m", id + ".json"), JSON.stringify({ cols, rows: dedup }));
  meta.metrics[id] = { src, ok: true, at: now, last: dedup.at(-1)[0], n: dedup.length, ...(note ? { note } : {}) };
}
function fail(id, src, e) {
  const prev = meta.metrics[id] || {};
  meta.metrics[id] = { ...prev, src, ok: false, err: String(e.message || e).slice(0, 160), tried: now };
  console.log(`  ✕ ${id}: ${e.message || e}`);
}
async function getJSON(url, tries = 3) {
  for (let i = 0; ; i++) {
    try {
      const r = await fetch(url, { headers: { "user-agent": "btc-onchain-dashboard (github actions)" } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) {
      if (i >= tries - 1 || /HTTP 4(0[0-4]|29)/.test(e.message)) throw e;
      await sleep(2000 * (i + 1));
    }
  }
}

// ---------- Coin Metrics Community ----------
// Community-tier metrics only (CapRealUSD, FeeTotUSD, RevUSD, NVTAdj90, TxTfrValAdjUSD, SplyAct* return 403;
// realized cap, USD fees and miner revenue are derived in the page instead).
const CM = [
  "PriceUSD", "CapMrktCurUSD", "CapMVRVCur", "SplyCur", "AdrActCnt", "TxCnt", "FeeTotNtv", "HashRate",
  "IssTotNtv", "IssTotUSD", "FlowInExUSD", "FlowOutExUSD", "SplyExNtv", "BlkCnt",
];
async function coinMetrics() {
  console.log("Coin Metrics");
  for (const m of CM) {
    const id = "cm-" + m;
    try {
      let url = `https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc&metrics=${m}&frequency=1d&start_time=2011-01-01&page_size=10000`;
      const rows = [];
      for (let p = 0; url && p < 5; p++) {
        const j = await getJSON(url);
        for (const d of j.data || []) rows.push([day(d.time), round(parseFloat(d[m]))]);
        url = j.next_page_url;
        await sleep(700);
      }
      save(id, [m], rows, "coinmetrics");
      console.log(`  ✓ ${id} (${rows.length})`);
    } catch (e) { fail(id, "coinmetrics", e); }
    await sleep(700);
  }
}

// ---------- BGeometrics (rotating cursor) ----------
export const BG = [
  // ETF + derivatives first (no other free source), then wallet bands, then extras
  "etf-btc-total", "etf-flow-btc", "funding-rate", "open-interest-futures", "btc-liquidations-1d",
  "coins-addr-1-BTC", "coins-addr-10-1-BTC", "coins-addr-100-10-BTC", "coins-addr-1K-100-BTC",
  "coins-addr-10K-1K-BTC", "coins-addr-10K-BTC",
  "true-market-mean", "investor-price", "hashprice", "nvts",
];  // cohort metrics (STH/LTH, SOPR, NUPL, HODL, CDD) are self-computed from BigQuery now
// Date key differs by endpoint (d, theDay, unixTs, ...): pick it by name or by value shape.
const DATE_RE = /^\d{4}-\d{2}-\d{2}/;
function dateKey(r) {
  const ks = Object.keys(r);
  return ks.find((k) => /^(unixts|timestamp|time|ts)$/i.test(k) && Number(r[k]) > 1e9)
    || ks.find((k) => typeof r[k] === "string" && DATE_RE.test(r[k]))
    || ks.find((k) => /date|day|time/i.test(k));
}
function normalizeBG(arr) {
  if (!Array.isArray(arr)) arr = arr?.data || [];
  if (!arr.length) throw new Error("empty");
  const dk = dateKey(arr.at(-1));
  if (!dk) throw new Error("no date key in " + Object.keys(arr.at(-1)).join(","));
  const skip = new Set(["id", dk]);
  for (const k of Object.keys(arr.at(-1))) if (typeof arr.at(-1)[k] === "string" && DATE_RE.test(arr.at(-1)[k])) skip.add(k);
  if ("unixTs" in arr.at(-1)) skip.add("unixTs");
  const cols = [];
  for (const r of arr.slice(-50)) for (const [k, v] of Object.entries(r))
    if (!skip.has(k) && !cols.includes(k) && v !== null && v !== "" && typeof v !== "boolean" && !isNaN(Number(v))) cols.push(k);
  const rows = arr.map((r) => {
    const t = r[dk], dd = typeof t === "string" && !DATE_RE.test(t) ? Number(t) : t;
    return [day(dd), ...cols.map((c) => round(r[c] == null || r[c] === "" ? NaN : Number(r[c])))];
  }).filter((r) => Number.isFinite(r[0]));
  if (!rows.length) throw new Error("dates unparsed via " + dk);
  return { cols, rows };
}
async function bgeometrics() {
  const n = Math.min(BG_PER_RUN, BG.length);
  // Repair first: series saved without usable dates, or never fetched successfully.
  const repair = [...BG.filter((ep) => !meta.metrics["bg-" + ep]?.ok), ...BG.filter((ep) => { const m = meta.metrics["bg-" + ep]; return m?.ok && m.last == null; })].slice(0, n);
  const rot = [];
  for (let i = 0; rot.length < n - repair.length && i < BG.length; i++) {
    const ep = BG[(meta.bgCursor + i) % BG.length];
    if (!repair.includes(ep)) rot.push(ep);
  }
  const queue = [...repair, ...rot];
  console.log(`BGeometrics: repair [${repair.join(", ")}] + ${rot.length} from cursor ${meta.bgCursor}`);
  for (let i = 0; i < queue.length; i++) {
    const ep = queue[i];
    const id = "bg-" + ep;
    try {
      const j = await getJSON(`https://bitcoin-data.com/v1/${ep}`, 2);
      const { cols, rows } = normalizeBG(j);
      save(id, cols, rows, "bgeometrics");
      console.log(`  ✓ ${id} (${rows.length}) cols=${cols.join(",")}`);
    } catch (e) {
      fail(id, "bgeometrics", e);
      if (/HTTP 429/.test(e.message)) { console.log("  rate limited — stopping BG for this run"); break; }
    }
    await sleep(1500);
  }
  meta.bgCursor = (meta.bgCursor + rot.length) % BG.length;
}


async function getText(url) {
  const r = await fetch(url, { headers: { "user-agent": "Mozilla/5.0 (btc-onchain dashboard; github actions)", accept: "text/html" } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.text();
}

// ---------- Fear & Greed (alternative.me, full history) ----------
async function fearGreed() {
  try {
    const j = await getJSON("https://api.alternative.me/fng/?limit=0&format=json");
    save("fng", ["value"], (j.data || []).map((d) => [day(Number(d.timestamp)), Number(d.value)]), "alternative.me");
    console.log("  ✓ fng");
  } catch (e) { fail("fng", "alternative.me", e); }
}

// ---------- Stablecoin supply (DefiLlama) ----------
async function stablecoins() {
  try {
    const j = await getJSON("https://stablecoins.llama.fi/stablecoincharts/all");
    save("llama-stables", ["usd"], j.map((d) => [day(Number(d.date)), round(d.totalCirculatingUSD?.peggedUSD ?? d.totalCirculating?.peggedUSD)]), "defillama");
    console.log("  ✓ llama-stables");
  } catch (e) { fail("llama-stables", "defillama", e); }
}

// ---------- Spot ETF daily flows (Farside, US$m) ----------
const MON = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
async function farside() {
  try {
    const html = await getText("https://farside.co.uk/bitcoin-etf-flow-all-data/");
    const rows = [];
    for (const tr of html.split(/<tr[\s>]/i).slice(1)) {
      const cells = [...tr.split(/<\/tr>/i)[0].matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi)].map((m) => m[1].replace(/<[^>]+>/g, "").replace(/&nbsp;|,/g, "").trim());
      const m = cells[0]?.match(/^(\d{1,2}) (\w{3}) (\d{4})$/);
      if (!m || MON[m[2]] == null) continue;
      const tot = cells.at(-1);
      if (!tot || tot === "-") continue;
      const v = /^\(.*\)$/.test(tot) ? -parseFloat(tot.slice(1, -1)) : parseFloat(tot);
      if (!isFinite(v)) continue;
      rows.push([Math.floor(Date.UTC(+m[3], MON[m[2]], +m[1]) / 864e5), v]);
    }
    if (rows.length < 50) throw new Error(`parsed only ${rows.length} rows`);
    save("farside-etf", ["flow_usd_m"], rows, "farside");
    console.log(`  ✓ farside-etf (${rows.length})`);
  } catch (e) { fail("farside-etf", "farside", e); }
}

// ---------- Coinbase premium (Coinbase BTC-USD vs Binance BTC-USDT daily close) ----------
async function coinbasePremium() {
  try {
    const cb = new Map(), bn = new Map(), end = Date.now(), start = end - 1500 * 864e5;
    for (let t = start; t < end; t += 299 * 864e5) {
      const a = new Date(t).toISOString(), b = new Date(Math.min(t + 299 * 864e5, end)).toISOString();
      const j = await getJSON(`https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400&start=${a}&end=${b}`);
      for (const c of j) cb.set(Math.floor(c[0] / 86400), c[4]);
      await sleep(400);
    }
    for (let t = start; t < end; t += 1000 * 864e5) {
      const j = await getJSON(`https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=1000&startTime=${t}`);
      for (const k of j) bn.set(Math.floor(k[0] / 864e5), +k[4]);
      await sleep(400);
    }
    const rows = [...cb].filter(([d]) => bn.has(d)).map(([d, c]) => [d, round((c / bn.get(d) - 1) * 100)]);
    save("cb-premium", ["pct"], rows, "coinbase/binance");
    console.log(`  ✓ cb-premium (${rows.length})`);
  } catch (e) { fail("cb-premium", "coinbase/binance", e); }
}

// ---------- mempool.space snapshot ----------
async function mempool() {
  console.log("mempool.space");
  const base = "https://mempool.space/api";
  const snap = {};
  for (const [k, u] of Object.entries({
    fees: "/v1/fees/recommended", mempool: "/mempool", diff: "/v1/difficulty-adjustment",
    pools: "/v1/mining/pools/1w", tip: "/blocks/tip/height",
  })) {
    try { snap[k] = await getJSON(base + u); } catch (e) { console.log(`  ✕ ${k}: ${e.message}`); }
    await sleep(400);
  }
  if (snap.mempool) delete snap.mempool.fee_histogram;
  if (snap.pools?.pools) snap.pools.pools = snap.pools.pools.slice(0, 10).map(({ name, blockCount }) => ({ name, blockCount }));
  fs.writeFileSync(path.join(OUT, "mempool.json"), JSON.stringify({ at: now, ...snap }));
  try {
    const h = await getJSON(base + "/v1/mining/hashrate/all");
    save("mp-hashrate", ["hashrate"], (h.hashrates || []).map((x) => [day(x.timestamp), round(x.avgHashrate)]), "mempool");
    save("mp-difficulty", ["difficulty"], (h.difficulty || []).map((x) => [day(x.time), round(x.difficulty)]), "mempool");
    console.log("  ✓ hashrate/difficulty history");
  } catch (e) { fail("mp-hashrate", "mempool", e); }
}

await coinMetrics();
console.log("extra sources");
await fearGreed();
await stablecoins();
await farside();
await coinbasePremium();
await bgeometrics();
await mempool();
meta.updated = now;
// drop status entries for series no longer fetched (keeps Data status clean)
for (const k of Object.keys(meta.metrics)) {
  if ((k.startsWith("cm-") && !CM.includes(k.slice(3))) || (k.startsWith("bg-") && !BG.includes(k.slice(3)) && !meta.metrics[k].ok)) delete meta.metrics[k];
}
fs.writeFileSync(metaPath, JSON.stringify(meta, null, 1));
const ok = Object.values(meta.metrics).filter((m) => m.ok).length;
console.log(`Done: ${ok}/${Object.keys(meta.metrics).length} metrics OK`);
