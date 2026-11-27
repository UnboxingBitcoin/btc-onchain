"""UTXO cube backfill. Usage: python backfill.py query | compute [outdir]
query   : ONE guarded BigQuery scan of the public Bitcoin tables -> onchain.cube (kept in your sandbox project).
compute : reads onchain.cube (tiny scans) + Coin Metrics price -> metric series, validation, CSV, parquet."""
import datetime as dt, json, os, sys
import numpy as np, pandas as pd, requests
from google.cloud import bigquery
import metrics as M

PROJECT = os.environ["GCP_PROJECT"]
LOC = "US"
DS = f"{PROJECT}.onchain"
CUBE = f"{DS}.cube"
GUARD = 900e9                      # bytes: refuse if this month's usage + estimate would pass it
FORCE = os.environ.get("FORCE") == "1"
G0 = dt.date(2009, 1, 3)
GS = "DATE '2009-01-03'"
P = "`bigquery-public-data.crypto_bitcoin"

def cube_sql(cb=True, ty=True):
    cb_col = "IF(t.hash IS NOT NULL, 1, 0)" if cb else "0"
    ty_col = """CASE o.type WHEN 'pubkey' THEN 0 WHEN 'pubkeyhash' THEN 1 WHEN 'scripthash' THEN 2
       WHEN 'witness_v0_keyhash' THEN 3 WHEN 'witness_v0_scripthash' THEN 3 WHEN 'witness_v1_taproot' THEN 4 ELSE 5 END""" if ty else "5"
    cb_join = f"LEFT JOIN (SELECT hash FROM {P}.transactions` WHERE is_coinbase) t ON t.hash = o.transaction_hash" if cb else ""
    return f"""
SELECT
  DATE(o.block_timestamp) AS cd,
  DATE(i.block_timestamp) AS sd,
  CASE WHEN o.value < 10000000 THEN 0 WHEN o.value < 100000000 THEN 1 WHEN o.value < 1000000000 THEN 2
       WHEN o.value < 10000000000 THEN 3 WHEN o.value < 100000000000 THEN 4 ELSE 5 END AS band,
  {cb_col} AS cb,
  {ty_col} AS ty,
  SUM(CAST(o.value AS FLOAT64)) AS v,
  COUNT(*) AS n
FROM {P}.outputs` o
LEFT JOIN {P}.inputs` i
  ON i.spent_transaction_hash = o.transaction_hash AND i.spent_output_index = o.index
{cb_join}
WHERE o.type != 'nulldata'
GROUP BY cd, sd, band, cb, ty
"""


_v = os.environ.get("CUBE_VARIANT", "both")
CUBE_SQL = cube_sql(cb=_v in ("both", "cb"), ty=_v in ("both", "ty"))



def client():
    return bigquery.Client(project=PROJECT, location=LOC)


def month_used(c):
    try:
        r = list(c.query("""SELECT COALESCE(SUM(total_bytes_billed),0) AS b
            FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_USER
            WHERE creation_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
              AND job_type = 'QUERY' AND error_result IS NULL""").result())
        return float(r[0].b)
    except Exception as e:
        print("WARN could not read month usage:", str(e)[:200])
        return 0.0


def cmd_query():
    c = client()
    ds = bigquery.Dataset(DS); ds.location = LOC
    c.create_dataset(ds, exists_ok=True)
    try:
        t = c.get_table(CUBE)
        if t.num_rows and not FORCE:
            print(f"cube already exists with {t.num_rows:,} rows - skipping (set FORCE=1 to rebuild)")
            return
    except Exception:
        pass
    est = c.query(CUBE_SQL, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)).total_bytes_processed
    used = month_used(c)
    print(f"estimate {est/1e9:.1f} GB ({est/2**40:.3f} TiB); used this month {used/1e9:.1f} GB; guard {GUARD/1e9:.0f} GB")
    if used + est > GUARD:
        sys.exit("ABORT: would pass the monthly guard")
    cfg = bigquery.QueryJobConfig(destination=CUBE, write_disposition="WRITE_TRUNCATE",
                                  maximum_bytes_billed=int(est * 1.05), use_query_cache=False)
    job = c.query(CUBE_SQL, job_config=cfg)
    job.result()
    t = c.get_table(CUBE)
    print(f"DONE billed {job.total_bytes_billed/1e9:.1f} GB ({job.total_bytes_billed/2**40:.3f} TiB); cube rows {t.num_rows:,}; table {t.num_bytes/1e6:.0f} MB")


def coinmetrics():
    url = ("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc&metrics=PriceUSD,CapMVRVCur,SplyCur"
           "&frequency=1d&start_time=2009-01-03&page_size=10000")
    rows = []
    while url:
        r = requests.get(url, timeout=60); r.raise_for_status()
        j = r.json(); rows += j["data"]; url = j.get("next_page_url")
    df = pd.DataFrame(rows)
    df["d"] = pd.to_datetime(df["time"].str[:10])
    for k in ("PriceUSD", "CapMVRVCur", "SplyCur"):
        df[k] = pd.to_numeric(df.get(k), errors="coerce")
    return df


def q(c, sql, dest=None):
    cfg = bigquery.QueryJobConfig(use_query_cache=False)
    if dest:
        cfg.destination = dest; cfg.write_disposition = "WRITE_TRUNCATE"
    j = c.query(sql, job_config=cfg); r = j.result()
    return j, r


def cmd_compute(out="out_u"):
    c = client()
    cm = coinmetrics()
    ext = list(c.query(f"SELECT DATE_DIFF(MAX(cd),{GS},DAY) a, DATE_DIFF(MAX(sd),{GS},DAY) b FROM `{CUBE}`").result())[0]
    last_cube = max(ext.a, ext.b)
    pr = cm.dropna(subset=["PriceUSD"])
    last_price = (pr["d"].max().date() - G0).days
    N = min(last_cube, last_price) + 1                # last COMPLETE day
    days = pd.date_range(G0, periods=N)
    ps = pr.set_index("d")["PriceUSD"].reindex(days).ffill().bfill()
    Pr = ps.to_numpy(float)
    print(f"days {N}, last day {days[-1].date()}, price {Pr[-1]:.0f}")
    pt = pd.DataFrame({"d": days.date, "p": Pr})
    c.load_table_from_dataframe(pt, f"{DS}.price", job_config=bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",
        schema=[bigquery.SchemaField("d", "DATE"), bigquery.SchemaField("p", "FLOAT64")])).result()

    scan = 0
    def dl(sql):
        nonlocal scan
        j, r = q(c, sql); scan += j.total_bytes_billed or 0
        return r.to_dataframe()

    created = dl(f"SELECT DATE_DIFF(cd,{GS},DAY) cdi, SUM(v) v FROM `{CUBE}` GROUP BY 1")
    cr = np.zeros(N); m = created.cdi.to_numpy() < N
    cr[created.cdi.to_numpy()[m]] = created.v.to_numpy()[m] / 1e8
    core_t = f"{DS}.core"
    j, _ = q(c, f"SELECT DATE_DIFF(cd,{GS},DAY) cdi, DATE_DIFF(sd,{GS},DAY) sdi, SUM(v) v FROM `{CUBE}` WHERE sd IS NOT NULL GROUP BY 1,2", dest=core_t)
    scan += j.total_bytes_billed or 0
    try:
        tb = c.list_rows(core_t).to_arrow(create_bqstorage_client=True)
    except Exception as e:
        print("bqstorage unavailable, falling back:", str(e)[:120])
        tb = c.list_rows(core_t).to_arrow(create_bqstorage_client=False)
    cdi = tb["cdi"].to_numpy().astype(np.int64); sdi = tb["sdi"].to_numpy().astype(np.int64); v = tb["v"].to_numpy() / 1e8
    keep = (cdi < N) & (sdi < N)
    cdi, sdi, v = cdi[keep], sdi[keep], v[keep]
    print(f"core pairs {len(v):,}")

    created_L, spent_L = {}, {}
    for lab, names in (("band", M.BAND_NAMES), ("cb", ["noncoinbase", "coinbase"]), ("ty", M.TYPE_NAMES)):
        cL = dl(f"SELECT {lab} l, DATE_DIFF(cd,{GS},DAY) cdi, SUM(v) v FROM `{CUBE}` GROUP BY 1,2")
        sL = dl(f"""SELECT {lab} l, DATE_DIFF(sd,{GS},DAY) sdi, SUM(v) v, SUM(v*pc.p) cost, SUM(v*ps.p) val,
                    SUM(v*DATE_DIFF(sd,cd,DAY)) cds FROM `{CUBE}` c JOIN `{DS}.price` pc ON pc.d=c.cd JOIN `{DS}.price` ps ON ps.d=c.sd
                    WHERE sd IS NOT NULL GROUP BY 1,2""")
        for li, nm in enumerate(names):
            a = np.zeros(N); x = cL[(cL.l == li) & (cL.cdi < N)]; a[x.cdi.to_numpy()] = x.v.to_numpy() / 1e8
            created_L[f"{lab}:{nm}"] = a
            y = sL[(sL.l == li) & (sL.sdi < N)]
            d = {k: np.zeros(N) for k in ("v", "cost", "val", "cds")}
            for k in d:
                d[k][y.sdi.to_numpy()] = y[k].to_numpy() / 1e8
            spent_L[f"{lab}:{nm}"] = d
    print(f"derived-table scans {scan/1e9:.1f} GB")

    urpd_days = [N - 1 - 30 * k for k in range(0, 25) if N - 1 - 30 * k > 0]
    o, snaps, edges = M.core_metrics(Pr, cr, cdi, sdi, v, urpd_days=urpd_days)
    o = M.derive(Pr, o)
    lab = M.label_series(Pr, created_L, spent_L)

    GENESIS = M.GENESIS_UNIX_DAY
    ud = lambda i: int(GENESIS + i)
    first = int(np.argmax(o["supply"] > 0))
    idx = np.arange(first, N)

    def rows(cols_arrays):
        names = list(cols_arrays)
        arrs = [np.asarray(cols_arrays[k], float)[idx] for k in names]
        out_rows = []
        for r, i in enumerate(idx):
            row = [ud(i)]
            for a in arrs:
                x = a[r]; row.append(float(f"{x:.7g}") if np.isfinite(x) else None)
            out_rows.append(row)
        return names, out_rows

    groups = {
        "val": {k: o[k] for k in ["price", "market_cap", "supply", "realized_cap", "realized_price", "mvrv", "mvrv_z", "nupl"]},
        "holders": {k: o[k] for k in ["sth_supply", "lth_supply", "sth_rp", "lth_rp", "sth_mvrv", "lth_mvrv", "sth_nupl",
                                      "lth_nupl", "pct_in_profit", "pct_in_loss", "avg_age", "liveliness"]},
        "spent": {k: o[k] for k in ["sopr", "asopr", "sth_sopr", "lth_sopr", "realized_profit", "realized_loss",
                                    "net_realized_pl", "cdd", "vdd", "sa_cdd", "dormancy", "moved_btc"]},
        "waves": {"w_" + n: o["wp_" + n] for n in M.WAVE_NAMES},
        "rcwaves": {"rcw_" + n: np.divide(o["rcw_" + n], o["realized_cap"], out=np.full(N, np.nan), where=o["realized_cap"] > 0) * 100 for n in M.WAVE_NAMES},
        "bands": {**{"sup_" + n: lab[f"band:{n}"]["supply"] for n in M.BAND_NAMES},
                  **{"rcap_" + n: lab[f"band:{n}"]["rcap"] for n in M.BAND_NAMES}},
        "types": {"sup_" + n: lab[f"ty:{n}"]["supply"] for n in M.TYPE_NAMES},
        "miners": {"cb_supply": lab["cb:coinbase"]["supply"], "cb_spent": lab["cb:coinbase"]["spent"],
                   "cb_sopr": lab["cb:coinbase"]["sopr"], "cb_cdd": lab["cb:coinbase"]["cdd"]},
    }
    os.makedirs(os.path.join(out, "u"), exist_ok=True); os.makedirs(os.path.join(out, "csv"), exist_ok=True)
    allcols = {}
    for g, ca in groups.items():
        names, rr = rows(ca)
        json.dump({"cols": names, "rows": rr}, open(os.path.join(out, "u", g + ".json"), "w"), separators=(",", ":"))
        if g in ("val", "holders", "spent"):
            allcols.update(ca)
    names, rr = rows(allcols)
    df = pd.DataFrame(rr, columns=["unix_day"] + names)
    df.insert(0, "date", pd.to_datetime(df.unix_day * 86400, unit="s").dt.strftime("%Y-%m-%d"))
    df.drop(columns="unix_day").to_csv(os.path.join(out, "csv", "utxo_daily.csv"), index=False)
    json.dump({"edges": [float(f"{e:.6g}") for e in edges],
               "snaps": [{"day": ud(t), "v": [float(f"{x:.5g}") for x in s]} for t, s in sorted(snaps.items())]},
              open(os.path.join(out, "u", "urpd.json"), "w"), separators=(",", ":"))

    # validation vs Coin Metrics (same UTXO-realized-cap method)
    cmi = cm.set_index("d").reindex(days)
    start = max(first, N - 4 * 365)
    sl = slice(start, N)
    mv = np.abs(o["mvrv"][sl] / cmi["CapMVRVCur"].to_numpy()[sl] - 1) * 100
    sp = np.abs(o["supply"][sl] / cmi["SplyCur"].to_numpy()[sl] - 1) * 100
    val = {"window": "last 4y", "mvrv_median_abs_pct": float(np.nanmedian(mv)), "mvrv_p95_abs_pct": float(np.nanpercentile(mv, 95)),
           "supply_median_abs_pct": float(np.nanmedian(sp)), "supply_p95_abs_pct": float(np.nanpercentile(sp, 95)),
           "mine_mvrv_last": float(o["mvrv"][-1]), "cm_mvrv_last": float(cmi["CapMVRVCur"].iloc[-1]) if np.isfinite(cmi["CapMVRVCur"].iloc[-1]) else None,
           "mine_supply_last": float(o["supply"][-1]), "cm_supply_last": float(cmi["SplyCur"].iloc[-1]) if np.isfinite(cmi["SplyCur"].iloc[-1]) else None}
    meta = {"updated": dt.datetime.utcnow().isoformat() + "Z", "last_day": ud(N - 1), "n_days": int(N),
            "sth_days": M.STH_DAYS, "validation": val, "groups": {g: list(ca) for g, ca in groups.items()},
            "note": "Self-computed from public BigQuery bitcoin tables; no entity clustering."}
    json.dump(meta, open(os.path.join(out, "u", "meta.json"), "w"), indent=1)
    import pyarrow as pa, pyarrow.parquet as pq
    pq.write_table(pa.table({"cdi": cdi.astype(np.int16), "sdi": sdi.astype(np.int16), "btc": v}), os.path.join(out, "core.parquet"), compression="zstd")
    print(json.dumps(val, indent=1))
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        open(s, "a").write("## UTXO validation vs Coin Metrics\n```\n" + json.dumps(val, indent=1) + "\n```\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "query": cmd_query()
    elif cmd == "compute": cmd_compute(*(sys.argv[2:3]))
    else: sys.exit(__doc__)
