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

def backtest(df, edge_min, spread, fee_rate=0.05, stake=500):
    """Правило: у станах, де фаворит відстає, купуємо ЛІДЕРА серії, якщо модельна перевага ≥ edge_min.
    fair(лідер) = 1 - shrunk_fair(фаворит); ціна входу = (1 - p_fav_market) + spread/2 (платимо ask).
    fee Polymarket sports: C * fee_rate * p(1-p) на share."""
    t = df.copy()
    # усадка до незалежності (регуляризація малої вибірки): fair = 0.5*realized_state + 0.5*indep — консервативно
    state_real = t.groupby("state").fav_won.mean()
    t["fair_fav"] = t.apply(lambda r: 0.5 * state_real[r.state] + 0.5 * r.p_fav_indep, axis=1)
    t["p_leader_fair"] = 1 - t.fair_fav
    t["price_leader"] = (1 - t.p_fav_market) + spread / 2
    t["model_edge"] = t.p_leader_fair - t.price_leader
    t["leader_won"] = 1 - t.fav_won
    sel = t[(t.model_edge >= edge_min) & (t.price_leader > 0.02) & (t.price_leader < 0.98)].copy()
    if sel.empty: print("\n[backtest] жодної події не пройшло поріг"); return None
    shares = stake / sel.price_leader
    fee = shares * fee_rate * sel.price_leader * (1 - sel.price_leader)
    sel["pnl"] = shares * (sel.leader_won - sel.price_leader) - fee
    sel["roi"] = sel.pnl / stake
    print(f"\n=== backtest: купуємо лідера, edge≥{edge_min:.0%}, spread={spread:.0%}, stake=${stake} ===")
    print(f"подій: {len(sel)}  виграно: {int(sel.leader_won.sum())}  середня ціна входу: {sel.price_leader.mean():.3f}")
    print(f"PnL: ${sel.pnl.sum():.0f}  ROI/подію: {sel.roi.mean():.1%}  медіанна глибина лідера: ${sel.limit_leader.median():.0f}")
    boot = [np.random.choice(sel.roi, len(sel), replace=True).mean() for _ in range(5000)]
    lo, hi = np.percentile(boot, [5, 95])
    print(f"bootstrap 90% CI на ROI/подію: [{lo:.1%}, {hi:.1%}]  → {'GO' if lo > 0 else 'NO-GO (нижня межа ≤ 0)'}")
    print("\nпо станах:"); print(sel.groupby("state").agg(n=("pnl", "size"), win=("leader_won", "sum"),
          price=("price_leader", "mean"), roi=("roi", "mean")).round(3).to_string())
    print("\nпо місяцях:"); print(sel.assign(m=pd.to_datetime(sel.date).dt.to_period("M")).groupby("m").agg(
          n=("pnl", "size"), pnl=("pnl", "sum"), roi=("roi", "mean")).round(2).to_string())
    sel.sort_values("date").to_csv(RES / "backtest_trades.csv", index=False)
    print(f"\nугоди → results/backtest_trades.csv")
    return sel

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="polymarket"); ap.add_argument("--edge", type=float, default=0.08)
    ap.add_argument("--spread", type=float, default=0.02); ap.add_argument("--stake", type=float, default=500)
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

    backtest(behind, a.edge, a.spread, stake=a.stake)
