#!/usr/bin/env python3
"""
lol_bo5_slices.py — стрес-тест ефекту "фаворит відстає" на Polymarket-цінах між іграми.

Вхід: results/polymarket_at_score.csv, results/pinnacle_at_score.csv (з lol_bo5_analyze.py)
Виводить:
  1) зріз по силі pre-match фаворита (0.50-0.60 / 0.60-0.70 / 0.70+)
  2) зріз по місяцях (чи ефект не сидить в одному тижні)
  3) зріз по тому, наскільки ринок уже здисконтував (market vs indep)
  4) backtest простого правила з fee/spread: продаємо фаворита (= купуємо лідера) при 0:1 і 0:2
  5) bootstrap CI на PnL

Запуск: python3 -u lol_bo5_slices.py [--book polymarket] [--edge 0.08] [--spread 0.02]
"""
import argparse
import numpy as np, pandas as pd
from pathlib import Path

RES = Path("results")

def wald(n, k, p_exp):
    """z для реалізованої частоти k/n проти очікуваної суми ймовірностей p_exp (сума, не середнє)"""
    var = None
    return (k - p_exp) / np.sqrt(max(var if var else 1e-9, 1e-9))

def block(df, by, label):
    rows = []
    for key, g in df.groupby(by):
        n = len(g); real = g.fav_won.mean(); mkt = g.p_fav_market.mean(); indep = g.p_fav_indep.mean()
        exp = g.p_fav_market.sum(); k = g.fav_won.sum()
        sd = np.sqrt((g.p_fav_market * (1 - g.p_fav_market)).sum())
        z = (k - exp) / sd if sd > 0 else np.nan
        rows.append({by if isinstance(by, str) else "key": key, "n": n, "realized": round(real, 3),
                     "market": round(mkt, 3), "indep": round(indep, 3), "edge_pp": round(100 * (mkt - real), 1),
                     "z": round(z, 2), "depth_med": round(g.limit_leader.median(), 0) if "limit_leader" in g else None})
    out = pd.DataFrame(rows)
    print(f"\n=== {label} ===");  print(out.to_string(index=False))
    return out

def backtest(df, edge_min, spread, fee_rate=0.05, stake=500, min_depth=300, pmin=0.15, pmax=0.85, min_hist=10):
    """Правило: при 0:1 / 0:2 купуємо ЛІДЕРА серії.
    fair рахується EXPANDING-вікном: для події в момент t беруться ТІЛЬКИ події до t (без look-ahead).
    Ціна входу = ask лідера = (1 - p_fav_market) + spread/2. Fee Polymarket sports = shares*rate*p*(1-p)."""
    t = df.sort_values("date").copy()
    fair, used = [], []
    for i, r in enumerate(t.itertuples(index=False)):
        hist = t.iloc[:i]; hs = hist[hist.state == r.state]
        if len(hs) < min_hist:
            fair.append(np.nan); used.append(0); continue
        # усадка: 50% реалізована частота цього стану в минулому + 50% незалежність
        fair.append(0.5 * hs.fav_won.mean() + 0.5 * r.p_fav_indep); used.append(len(hs))
    t["fair_fav"] = fair; t["hist_n"] = used
    t = t[t.fair_fav.notna()].copy()
    t["p_leader_fair"] = 1 - t.fair_fav
    t["price_leader"] = (1 - t.p_fav_market) + spread / 2
    t["model_edge"] = t.p_leader_fair - t.price_leader
    t["leader_won"] = 1 - t.fav_won
    sel = t[(t.model_edge >= edge_min) & t.price_leader.between(pmin, pmax) & (t.limit_leader >= min_depth)].copy()
    print(f"\n=== backtest (expanding fair, no look-ahead): edge≥{edge_min:.0%}, spread={spread:.0%}, "
          f"depth≥${min_depth}, ціна {pmin}-{pmax}, stake=${stake} ===")
    print(f"подій із порахованим fair: {len(t)} (перші {min_hist} на стан пропущено як розігрів)")
    if sel.empty: print("[backtest] жодної події не пройшло фільтри"); return None
    shares = np.minimum(stake / sel.price_leader, sel.limit_leader * 0.5)  # не більше половини видимої глибини
    cost = shares * sel.price_leader
    fee = shares * fee_rate * sel.price_leader * (1 - sel.price_leader)
    sel["pnl"] = shares * (sel.leader_won - sel.price_leader) - fee
    sel["roi"] = sel.pnl / cost
    print(f"подій: {len(sel)}  лідер виграв: {int(sel.leader_won.sum())}  середня ціна входу: {sel.price_leader.mean():.3f}  "
          f"середній вкладений: ${cost.mean():.0f}")
    print(f"PnL: ${sel.pnl.sum():.0f}  ROI/подію: {sel.roi.mean():.1%}  медіанна глибина лідера: ${sel.limit_leader.median():.0f}")
    boot = [np.random.choice(sel.roi.values, len(sel), replace=True).mean() for _ in range(5000)]
    lo, hi = np.percentile(boot, [5, 95])
    print(f"bootstrap 90% CI на ROI/подію: [{lo:.1%}, {hi:.1%}]  → {'GO' if lo > 0 else 'NO-GO (нижня межа ≤ 0)'}")
    print("\nпо станах:"); print(sel.groupby("state").agg(n=("pnl", "size"), win=("leader_won", "sum"),
          price=("price_leader", "mean"), roi=("roi", "mean")).round(3).to_string())
    print("\nпо місяцях:"); print(sel.assign(m=pd.to_datetime(sel.date).dt.to_period("M")).groupby("m").agg(
          n=("pnl", "size"), pnl=("pnl", "sum"), roi=("roi", "mean")).round(2).to_string())
    sel.sort_values("date").to_csv(RES / "backtest_trades.csv", index=False)
    print("\nугоди → results/backtest_trades.csv")
    return sel

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="polymarket"); ap.add_argument("--edge", type=float, default=0.08)
    ap.add_argument("--spread", type=float, default=0.02); ap.add_argument("--stake", type=float, default=500)
    ap.add_argument("--min-depth", type=float, default=300); ap.add_argument("--min-hist", type=int, default=10)
    a = ap.parse_args()
    f = RES / f"{a.book}_at_score.csv"
    if not f.exists(): raise SystemExit(f"немає {f} — спочатку lol_bo5_analyze.py")
    df = pd.read_csv(f)
    df = df[df.level == "Primary"].copy()
    df["date"] = pd.to_datetime(df.date)
    behind = df[df.state.isin(["0:1", "0:2"])].copy()
    print(f"[{a.book}] Primary: {len(df)} точок, з них 'фаворит відстає' (0:1, 0:2): {len(behind)}")

    block(df, "state", "усі стани (контроль)")
    behind["fav_strength"] = pd.cut(behind.p_fav_prior, [0.5, 0.6, 0.7, 1.0], labels=["0.50-0.60", "0.60-0.70", "0.70+"])
    block(behind, "fav_strength", "0:1+0:2 за силою pre-match фаворита")
    behind["month"] = behind.date.dt.to_period("M").astype(str)
    block(behind, "month", "0:1+0:2 за місяцями")
    behind["mkt_vs_indep"] = pd.cut(behind.p_fav_market - behind.p_fav_indep, [-1, -0.15, -0.05, 1],
                                    labels=["ринок сильно здисконтував", "помірно", "майже не здисконтував"])
    block(behind, "mkt_vs_indep", "0:1+0:2 за тим, наскільки ринок уже врахував рахунок")
    behind["depth_bucket"] = pd.cut(behind.limit_leader, [0, 300, 1000, 1e9], labels=["<$300", "$300-1k", ">$1k"])
    block(behind, "depth_bucket", "0:1+0:2 за глибиною на стороні лідера")

    backtest(behind, a.edge, a.spread, stake=a.stake, min_depth=a.min_depth, min_hist=a.min_hist)
