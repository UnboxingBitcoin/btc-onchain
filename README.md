# BTC On-Chain

Live page: https://unboxingbitcoin.github.io/btc-onchain/

A phone-first Bitcoin on-chain dashboard with no node to run. It covers valuation, profit and loss, holder cohorts, flows, miners, network activity, derivatives and a live mempool view.

## How it works

- `.github/workflows/nightly.yml` runs twice a day (03:40 and 15:40 UTC). It runs `scripts/fetch.mjs` and force-pushes the results to the `data` branch, so `main` never grows.
- `index.html` reads `https://raw.githubusercontent.com/UnboxingBitcoin/btc-onchain/data/`. The Live mempool section calls mempool.space straight from the browser.
- To test against another data folder, add `?data=<url-with-trailing-slash>`.

## Sources (phase 1: free, no keys)

| Source | What | Limits |
|---|---|---|
| Coin Metrics Community API | Price, market and realized cap, MVRV, supply, active addresses, transactions, transfer volume, fees, hashrate, issuance, exchange flows and supply, 1-year active supply | 10 requests per 6 s; about 1 day lag |
| BGeometrics (bitcoin-data.com) | STH/LTH realized price and MVRV, SOPR, NUPL, % supply in profit, realized P/L, HODL waves, CDD/VDD, hashprice, ETF flows, stablecoins, funding, OI, liquidations, Fear & Greed, supply by wallet size | Free: 10 per hour, 15 per day, last 4 years. MVRV/SOPR/NUPL/realized-price series withhold the latest 7 days. The script fetches the next 7 endpoints on a rotating cursor each run. |
| mempool.space | Fees, mempool, difficulty adjustment, hashrate and difficulty history, latest blocks, pools | Public API |

Derived in the page: realized price, MVRV Z-score, Mayer multiple, 200-week MA, Puell multiple, hash ribbons, exchange net flow, dormant supply share, fee share of miner revenue.

## Phase 2 (planned): BigQuery full chain

`bigquery-public-data.crypto_bitcoin` holds every block, transaction, input and output, so cohort metrics can be computed independently: realized cap by age band, SOPR by cohort, HODL waves, LTH/STH supply, and the UTXO realized-price distribution (URPD).

Setup: create a Google Cloud project and a service account with the BigQuery Job User role. Save its JSON key as the repo secret `GCP_SA_KEY`. Run one backfill, then incremental daily queries to stay inside the 1 TB/month free query tier.

Not investment advice.
