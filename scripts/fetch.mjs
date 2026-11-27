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
const CM = [
  "PriceUSD", "CapMrktCurUSD", "CapRealUSD", "CapMVRVCur", "SplyCur", "AdrActCnt", "TxCnt", "TxTfrValAdjUSD",
  "FeeTotUSD", "FeeTotNtv", "HashRate", "DiffLast", "IssTotNtv", "IssTotUSD", "RevUSD", "NVTAdj90",
  "FlowInExUSD", "FlowOutExUSD", "SplyExNtv", "BlkCnt", "SplyAct1yr", "SplyAct10yr", "CapAct1yrUSD",
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
  // valuation & cohorts
  "sth-mvrv", "sth-realized-price", "lth-realized-price", "sopr", "sth-sopr", "nupl", "profit-loss",
  "mvrv-zscore", "lth-mvrv", "lth-sopr", "nupl-sth", "nupl-lth", "true-market-mean", "investor-price",
  "realized-profit", "realized-loss", "hodl-waves-supply", "puell-multiple", "aviv",
  // behaviour, miners, flows, derivatives
  "cdd", "vdd-multiple", "hashribbons", "hashprice", "etf-flow-btc", "etf-btc-total", "stablecoin-supply",
  "funding-rate", "open-interest-futures", "btc-liquidations-1d", "fear-greed",
  // supply distribution
  "coins-addr-1-BTC", "coins-addr-10-1-BTC", "coins-addr-100-10-BTC", "coins-addr-1K-100-BTC",
  "coins-addr-10K-1K-BTC", "coins-addr-10K-BTC",
];
function normalizeBG(arr) {
  if (!Array.isArray(arr)) arr = arr?.data || [];
  if (!arr.length) throw new Error("empty");
  const skip = new Set(["d", "date", "day", "unixTs", "unixts", "timestamp", "time", "id"]);
  const cols = [];
  for (const r of arr.slice(-50)) for (const [k, v] of Object.entries(r))
    if (!skip.has(k) && !cols.includes(k) && v !== null && v !== "" && !isNaN(Number(v))) cols.push(k);
  const rows = arr.map((r) => [day(r.unixTs ?? r.d ?? r.date ?? r.time), ...cols.map((c) => round(r[c] == null || r[c] === "" ? NaN : Number(r[c])))]);
  return { cols, rows };
}
async function bgeometrics() {
  const n = Math.min(BG_PER_RUN, BG.length);
  console.log(`BGeometrics: ${n} endpoints from cursor ${meta.bgCursor}`);
  for (let i = 0; i < n; i++) {
    const ep = BG[(meta.bgCursor + i) % BG.length];
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
  meta.bgCursor = (meta.bgCursor + n) % BG.length;
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
await bgeometrics();
await mempool();
meta.updated = now;
fs.writeFileSync(metaPath, JSON.stringify(meta, null, 1));
const ok = Object.values(meta.metrics).filter((m) => m.ok).length;
console.log(`Done: ${ok}/${Object.keys(meta.metrics).length} metrics OK`);
