"""Pure-numpy UTXO metrics from the (created day x spent day) cube. No BigQuery here, so it can be tested offline.
Day index 0 = 2009-01-03. Units: BTC, USD, days."""
import numpy as np

GENESIS_UNIX_DAY = 14247  # 2009-01-03 in days since 1970-01-01
STH_DAYS = 155
WAVES = [(0, 1), (1, 7), (7, 30), (30, 90), (90, 180), (180, 365), (365, 730), (730, 1095),
         (1095, 1825), (1825, 2555), (2555, 3650), (3650, 10 ** 6)]
WAVE_NAMES = ["lt1d", "1d_1w", "1w_1m", "1m_3m", "3m_6m", "6m_1y", "1y_2y", "2y_3y", "3y_5y", "5y_7y", "7y_10y", "gt10y"]
BAND_NAMES = ["lt0.1", "0.1_1", "1_10", "10_100", "100_1k", "gt1k"]
TYPE_NAMES = ["p2pk", "p2pkh", "p2sh", "segwit_v0", "taproot", "other"]


def core_metrics(P, created, cdi, sdi, v, urpd_days=()):
    """P: price per day (N). created: BTC created per creation day (N). cdi/sdi/v: spent pairs (BTC).
    Returns dict of daily arrays (len N) and urpd snapshots."""
    N = len(P)
    o = np.argsort(sdi, kind="stable")
    cdi, sdi, v = cdi[o], sdi[o], v[o]
    bounds = np.searchsorted(sdi, np.arange(N + 1))
    idx = np.arange(N)
    age = (sdi - cdi).astype(np.int64)
    Pc, Ps = P[cdi], P[sdi]
    cost = v * Pc
    val = v * Ps
    same = cdi == sdi
    sth = age < STH_DAYS

    def per_sd(w, mask=None):
        if mask is not None:
            return np.bincount(sdi[mask], weights=w[mask], minlength=N)[:N]
        return np.bincount(sdi, weights=w, minlength=N)[:N]

    def ratio(a, b):
        return np.divide(a, b, out=np.full(N, np.nan), where=b > 0)

    out = {}
    out["moved_btc"] = per_sd(v)
    out["sopr"] = ratio(per_sd(val), per_sd(cost))
    out["asopr"] = ratio(per_sd(val, ~same), per_sd(cost, ~same))
    out["sth_sopr"] = ratio(per_sd(val, sth), per_sd(cost, sth))
    out["lth_sopr"] = ratio(per_sd(val, ~sth), per_sd(cost, ~sth))
    d = val - cost
    out["realized_profit"] = per_sd(d, d > 0)
    out["realized_loss"] = -per_sd(d, d < 0)
    cdd = per_sd(v * age)
    out["cdd"] = cdd
    out["vdd"] = cdd * P
    out["dormancy"] = ratio(cdd, out["moved_btc"])

    # walk the days keeping the unspent amount per creation day
    U = np.zeros(N)
    keys = ["supply", "realized_cap", "sth_supply", "lth_supply", "sth_rp", "lth_rp", "profit_supply",
            "loss_supply", "avg_age"] + ["w_" + n for n in WAVE_NAMES] + ["rcw_" + n for n in WAVE_NAMES]
    R = {k: np.full(N, np.nan) for k in keys}
    snaps = {}
    want = set(urpd_days)
    edges = np.logspace(-2, 6, 161)
    for t in range(N):
        U[t] += created[t]
        a, b = bounds[t], bounds[t + 1]
        if b > a:
            np.subtract.at(U, cdi[a:b], v[a:b])
        u = U[: t + 1]
        p = P[: t + 1]
        sup = u.sum()
        R["supply"][t] = sup
        up = u * p
        rc = up.sum()
        R["realized_cap"][t] = rc
        lo = max(0, t - STH_DAYS + 1)
        s_sup, s_rc = u[lo:].sum(), up[lo:].sum()
        R["sth_supply"][t], R["lth_supply"][t] = s_sup, sup - s_sup
        R["sth_rp"][t] = s_rc / s_sup if s_sup > 1e-9 else np.nan
        R["lth_rp"][t] = (rc - s_rc) / (sup - s_sup) if sup - s_sup > 1e-9 else np.nan
        gain = p < P[t]
        R["profit_supply"][t] = u[gain].sum()
        R["loss_supply"][t] = u[p > P[t]].sum()
        R["avg_age"][t] = (u * (t - idx[: t + 1])).sum() / sup if sup > 0 else np.nan
        cs = np.concatenate(([0.0], np.cumsum(u)))
        cr = np.concatenate(([0.0], np.cumsum(up)))
        for (alo, ahi), name in zip(WAVES, WAVE_NAMES):
            l = max(0, t - ahi + 1)
            h = t - alo
            if h >= l:
                R["w_" + name][t] = cs[h + 1] - cs[l]
                R["rcw_" + name][t] = cr[h + 1] - cr[l]
            else:
                R["w_" + name][t] = 0.0
                R["rcw_" + name][t] = 0.0
        if t in want:
            snaps[t] = np.histogram(p, bins=edges, weights=np.maximum(u, 0))[0]
    out.update(R)
    out["price"] = P.copy()
    cum_cdd = np.cumsum(cdd)
    cum_sup_days = np.cumsum(R["supply"])
    out["liveliness"] = ratio(cum_cdd, cum_sup_days)
    return out, snaps, edges


def derive(P, o, start_idx=0):
    """Ratios built on top of core_metrics output."""
    N = len(P)
    sup, rc = o["supply"], o["realized_cap"]
    mc = sup * P
    o["market_cap"] = mc
    o["realized_price"] = np.divide(rc, sup, out=np.full(N, np.nan), where=sup > 0)
    o["mvrv"] = np.divide(mc, rc, out=np.full(N, np.nan), where=rc > 0)
    # MVRV Z-score: (mc - rc) / expanding std of mc
    c1 = np.cumsum(mc); c2 = np.cumsum(mc ** 2); n = np.arange(1, N + 1)
    var = c2 / n - (c1 / n) ** 2
    sd = np.sqrt(np.maximum(var, 0))
    o["mvrv_z"] = np.divide(mc - rc, sd, out=np.full(N, np.nan), where=sd > 0)
    o["nupl"] = np.divide(mc - rc, mc, out=np.full(N, np.nan), where=mc > 0)
    o["sth_mvrv"] = P / o["sth_rp"]
    o["lth_mvrv"] = P / o["lth_rp"]
    o["sth_nupl"] = 1 - o["sth_rp"] / P
    o["lth_nupl"] = 1 - o["lth_rp"] / P
    tot = o["profit_supply"] + o["loss_supply"]
    o["pct_in_profit"] = np.divide(o["profit_supply"], sup, out=np.full(N, np.nan), where=sup > 0) * 100
    o["pct_in_loss"] = np.divide(o["loss_supply"], sup, out=np.full(N, np.nan), where=sup > 0) * 100
    o["net_realized_pl"] = o["realized_profit"] - o["realized_loss"]
    o["sa_cdd"] = np.divide(o["cdd"], sup, out=np.full(N, np.nan), where=sup > 0)
    for nm in [k for k in list(o) if k.startswith("w_")]:
        o["wp_" + nm[2:]] = np.divide(o[nm], sup, out=np.full(N, np.nan), where=sup > 0) * 100
    return o


def label_series(P, created_L, spent_L):
    """created_L: dict label -> array(N) BTC created. spent_L: dict label -> dict(v, cost, val, cds) arrays(N).
    Returns per-label supply and realized cap series plus spent stats."""
    out = {}
    for L in created_L:
        c = created_L[L]
        s = spent_L.get(L)
        zero = np.zeros(len(P))
        sv = s["v"] if s else zero
        sc = s["cost"] if s else zero
        out[L] = {
            "supply": np.cumsum(c) - np.cumsum(sv),
            "rcap": np.cumsum(c * P) - np.cumsum(sc),
            "spent": sv,
            "sopr": np.divide(s["val"], s["cost"], out=np.full(len(P), np.nan), where=s["cost"] > 0) if s else np.full(len(P), np.nan),
            "cdd": (s["cds"] if s else zero),
        }
    return out
