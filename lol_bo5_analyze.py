#!/usr/bin/env python3
"""
lol_bo5_analyze.py — H4: чи несе рахунок серії інформацію понад pre-match prior, і чи ринок це вже закладає.

Вхід (з lol_bo5_pull.py): data/lp_games.csv, data/lp_tournaments.csv, data/op_fixtures.csv, data/op_prices.csv, data/raw/markets.json
Вихід: таблиці в stdout + results/states_elo.csv, results/states_market.csv, results/underdog_2_0.csv,
       results/polymarket_at_score.csv (ціна ML серії у момент, коли рахунок став 2:0/0:2/1:2/2:1/2:2)

pip install pandas rapidfuzz --break-system-packages
"""
import json, re
from collections import defaultdict
from pathlib import Path
import pandas as pd
try:
    from rapidfuzz import fuzz
except ImportError:
    from difflib import SequenceMatcher
    class fuzz:  # fallback
        @staticmethod
        def token_set_ratio(a, b): return 100 * SequenceMatcher(None, a, b).ratio()

DATA = Path("data"); RES = Path("results"); RES.mkdir(exist_ok=True)
STATES = ["1:0", "0:1", "2:0", "0:2", "1:1", "2:1", "1:2", "2:2"]

def p_series(p, w, l):
    if w >= 3: return 1.0
    if l >= 3: return 0.0
    return p * p_series(p, w + 1, l) + (1 - p) * p_series(p, w, l + 1)

def norm(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\b(esports?|e-sports|gaming|team|club|academy|blue|red|ltd)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

# ---------------------------------------------------------------- series + Elo
g = pd.read_csv(DATA / "lp_games.csv")
g = g[g.winner.isin([1, 2]) & g.team1.notna() & g.team2.notna()].copy()
g["dt"] = pd.to_datetime(g.dt_utc)
g = g.sort_values(["dt", "match_id", "game_n"]).drop_duplicates(["match_id", "game_n"])
t = pd.read_csv(DATA / "lp_tournaments.csv").set_index("page")

elo = defaultdict(lambda: 1500.0); K = 32
series = {}
for r in g.itertuples(index=False):
    s = series.get(r.match_id)
    if s is None:
        s = series[r.match_id] = {"tA": r.team1, "tB": r.team2, "date": r.dt, "page": r.page,
                                  "eloA": elo[r.team1], "eloB": elo[r.team2], "games": []}
    e1, e2 = elo[r.team1], elo[r.team2]; p1 = 1 / (1 + 10 ** ((e2 - e1) / 400))
    win = r.team1 if r.winner == 1 else r.team2
    s["games"].append((r.game_n, win, r.dt))
    s1 = 1.0 if r.winner == 1 else 0.0
    elo[r.team1] = e1 + K * (s1 - p1); elo[r.team2] = e2 + K * ((1 - s1) - (1 - p1))

bo5 = []
for mid, s in series.items():
    games = sorted(s["games"]); wA = wB = 0; seq = []; ends = []
    for n, win, dt in games:
        if win == s["tA"]: wA += 1
        elif win == s["tB"]: wB += 1
        else: break
        seq.append("A" if win == s["tA"] else "B"); ends.append(dt)
        if wA == 3 or wB == 3: break
    if not (wA == 3 or wB == 3): continue
    if [x[0] for x in games[:len(seq)]] != list(range(1, len(seq) + 1)): continue
    meta = t.loc[s["page"]] if s["page"] in t.index else None
    level = (meta.level if meta is not None else None) or "?"
    bo5.append({"match_id": mid, "date": s["date"], "page": s["page"], "level": "Primary" if level == "Primary" else ("Secondary" if level == "Secondary" else "Other"),
                "league": (meta.league if meta is not None else "?"), "tA": s["tA"], "tB": s["tB"],
                "eloA": s["eloA"], "eloB": s["eloB"], "seq": "".join(seq), "ends": ends, "winner": "A" if wA == 3 else "B"})
S = pd.DataFrame(bo5); print(f"BO5 series: {len(S)}  (Primary {sum(S.level=='Primary')}, Secondary {sum(S.level=='Secondary')})")

# ---------------------------------------------------------------- market prior (Pinnacle closing) + Polymarket timeline
fx = pd.read_csv(DATA / "op_fixtures.csv") if (DATA / "op_fixtures.csv").exists() else pd.DataFrame()
pr = pd.read_csv(DATA / "op_prices.csv") if (DATA / "op_prices.csv").exists() else pd.DataFrame()
winner_mid = None
if (DATA / "raw/markets.json").exists():
    ms = json.loads((DATA / "raw/markets.json").read_text())
    present = set(pr.market_id.astype(str)) if not pr.empty and "market_id" in pr.columns else set()
    # 1) точна назва "Winner" з 2 outcomes, яка реально є в op_prices; 2) будь-який winner-подібний ринок, що є в даних; 3) fallback
    for pick in (lambda m: m.get("marketName", "").strip().lower() == "winner" and len(m.get("outcomes", [])) == 2 and str(m["marketId"]) in present,
                 lambda m: re.search(r"winner|moneyline", m.get("marketName", ""), re.I) and len(m.get("outcomes", [])) == 2 and str(m["marketId"]) in present,
                 lambda m: m.get("marketName", "").strip().lower() == "winner" and len(m.get("outcomes", [])) == 2):
        for m in ms:
            if pick(m): winner_mid = m["marketId"]; print("[market] winner market:", m["marketName"], winner_mid, [(o.get("outcomeId"), o.get("outcomeName")) for o in m["outcomes"]]); break
        if winner_mid is not None: break
    if winner_mid is None: print("[market] WINNER MARKET NOT FOUND — see data/raw/markets.json, set winner_mid manually")

def match_fixture(row):
    """знайти фікстуру OddsPapi по датi (±1 день) і назвах команд"""
    if fx.empty: return None
    d0 = row.date.tz_localize(None) if row.date.tzinfo else row.date
    cand = fx[(pd.to_datetime(fx.startTime, utc=True).dt.tz_localize(None) - d0).abs() <= pd.Timedelta(hours=30)]
    best, bs = None, 0
    for c in cand.itertuples(index=False):
        a = fuzz.token_set_ratio(norm(row.tA), norm(str(c.participant1Name))) + fuzz.token_set_ratio(norm(row.tB), norm(str(c.participant2Name)))
        b = fuzz.token_set_ratio(norm(row.tA), norm(str(c.participant2Name))) + fuzz.token_set_ratio(norm(row.tB), norm(str(c.participant1Name)))
        sc, flip = (a, False) if a >= b else (b, True)
        if sc > bs: bs, best = sc, (c.fixtureId, flip, c.startTime)
    return best if bs >= 150 else None

S["fixture_id"] = None; S["p_pin_A"] = None
if not fx.empty and winner_mid is not None and not pr.empty:
    pr["recorded_at"] = pd.to_datetime(pr.recorded_at, utc=True); pr["market_id"] = pr.market_id.astype(str)
    prw = pr[pr.market_id == str(winner_mid)]
    for i, row in S.iterrows():
        mf = match_fixture(row)
        if not mf: continue
        fid, flip, start = mf; S.at[i, "fixture_id"] = fid
        start = pd.to_datetime(start, utc=True)
        h = prw[(prw.fixture_id == fid) & (prw.bookmaker == "pinnacle") & (prw.recorded_at < start)].sort_values("recorded_at")
        if h.empty: continue
        last = h.groupby("outcome_id").tail(1).set_index("outcome_id").price
        if len(last) < 2: continue
        oids = sorted(last.index); o1, o2 = last[oids[0]], last[oids[1]]  # для ринку 121: outcome 121='1'=participant1, 122='2'=participant2
        q1, q2 = 1 / o1, 1 / o2; p1 = q1 / (q1 + q2)
        S.at[i, "p_pin_A"] = (1 - p1) if flip else p1
    print("matched fixtures:", S.fixture_id.notna().sum(), " with Pinnacle close:", S.p_pin_A.notna().sum())

# ---------------------------------------------------------------- state tables
def state_table(df, prior_col, label):
    rows = []
    for (lvl, yr), grp in df.groupby([df.level, df.date.dt.year]):
        acc = {k: [0, 0, 0.0] for k in STATES}; und20 = [0, 0]; g02 = [0, 0, 0.0]
        for r in grp.itertuples(index=False):
            pA = getattr(r, prior_col)
            if pA is None or pd.isna(pA): continue
            fav = "A" if pA >= 0.5 else "B"; pf = max(pA, 1 - pA); favWon = (r.winner == fav)
            fw = fl = 0
            for ch in r.seq:
                if fw == 0 and fl == 2: g02[0] += 1; g02[1] += (ch == fav); g02[2] += pf
                if ch == fav: fw += 1
                else: fl += 1
                k = f"{fw}:{fl}"
                if k in acc: acc[k][0] += 1; acc[k][1] += favWon; acc[k][2] += p_series(pf, fw, fl)
                if fw == 0 and fl == 2: und20[0] += 1; und20[1] += favWon
        base = {"level": lvl, "year": yr, "n_series": len(grp), "und_leads_2_0": und20[0], "reverse_sweeps": und20[1],
                "rev_pct": round(100 * und20[1] / und20[0], 1) if und20[0] else None,
                "fav_next_game_after_0_2": round(g02[1] / g02[0], 3) if g02[0] else None, "fav_next_game_pred": round(g02[2] / g02[0], 3) if g02[0] else None}
        for k, (n, w, p) in acc.items():
            base[f"{k}_n"] = n; base[f"{k}_real"] = round(w / n, 3) if n else None; base[f"{k}_indep"] = round(p / n, 3) if n else None
        rows.append(base)
    out = pd.DataFrame(rows); out.to_csv(RES / f"states_{label}.csv", index=False)
    print(f"\n=== {label} ===")
    print(out[["level", "year", "n_series", "und_leads_2_0", "reverse_sweeps", "rev_pct", "fav_next_game_after_0_2", "fav_next_game_pred",
               "0:2_n", "0:2_real", "0:2_indep", "1:2_real", "1:2_indep", "2:2_real", "2:2_indep"]].to_string(index=False))
    return out

S["p_elo_A"] = 1 / (1 + 10 ** ((S.eloB - S.eloA) / 400))
state_table(S[S.level != "Other"], "p_elo_A", "elo")
if S.p_pin_A.notna().any(): state_table(S[S.p_pin_A.notna()], "p_pin_A", "market")

# ---------------------------------------------------------------- Polymarket price at score changes (ML серії)
if not pr.empty and winner_mid is not None and S.fixture_id.notna().any():
    pm = pr[(pr.bookmaker == "polymarket") & (pr.market_id == str(winner_mid))]
    rows = []
    for r in S[S.fixture_id.notna()].itertuples(index=False):
        h = pm[pm.fixture_id == r.fixture_id]
        if h.empty: continue
        fw = fl = 0; pA = r.p_pin_A if r.p_pin_A is not None and not pd.isna(r.p_pin_A) else r.p_elo_A
        fav = "A" if pA >= 0.5 else "B"; pf = max(pA, 1 - pA)
        for k, ch in enumerate(r.seq):
            fw += (ch == fav); fl += (ch != fav); st = f"{fw}:{fl}"
            if st not in ("2:0", "0:2", "2:1", "1:2", "2:2", "1:0", "0:1", "1:1"): continue
            t_end = pd.Timestamp(r.ends[k]).tz_localize("UTC") + pd.Timedelta(minutes=40)  # ~кінець гри k (dt = початок гри) — уточнити
            after = h[(h.recorded_at >= t_end) & (h.recorded_at <= t_end + pd.Timedelta(minutes=12))].sort_values("recorded_at")
            if after.empty: continue
            last = after.groupby("outcome_id").tail(1).set_index("outcome_id").price
            if len(last) < 2: continue
            oids = sorted(last.index); q = [1 / last[o] for o in oids]; p_fav_mkt = q[0] / sum(q) if fav == "A" else q[1] / sum(q)
            rows.append({"match_id": r.match_id, "date": r.date, "level": r.level, "state": st, "p_fav_prior": pf,
                         "p_fav_indep": p_series(pf, fw, fl), "p_fav_market": p_fav_mkt, "fav_won": int(r.winner == fav)})
    if rows:
        M = pd.DataFrame(rows); M.to_csv(RES / "polymarket_at_score.csv", index=False)
        print("\n=== Polymarket ML price at score change (favorite perspective) ===")
        print(M.groupby("state").agg(n=("fav_won", "size"), realized=("fav_won", "mean"), market=("p_fav_market", "mean"), indep=("p_fav_indep", "mean")).round(3).to_string())
    else:
        print("\n[polymarket] no in-play price points found in window — перевірити, чи OddsPapi зберігає in-play рухи Polymarket (data/raw/hist_sample.json)")

# underdog 2:0 cases list
cases = []
for r in S[S.level != "Other"].itertuples(index=False):
    pA = r.p_pin_A if r.p_pin_A is not None and not pd.isna(r.p_pin_A) else r.p_elo_A
    fav = "A" if pA >= 0.5 else "B"; fw = fl = 0; hit = False
    for ch in r.seq:
        fw += (ch == fav); fl += (ch != fav)
        if fw == 0 and fl == 2: hit = True
    if hit: cases.append({"date": r.date.date(), "league": r.league, "level": r.level, "favorite": r.tA if fav == "A" else r.tB,
                          "underdog": r.tB if fav == "A" else r.tA, "p_fav": round(max(pA, 1 - pA), 3), "prior": "pinnacle" if r.p_pin_A is not None and not pd.isna(r.p_pin_A) else "elo",
                          "result": "REVERSE_SWEEP" if r.winner == fav else "underdog_closed", "seq": r.seq})
pd.DataFrame(cases).to_csv(RES / "underdog_2_0.csv", index=False)
print(f"\nunderdog-led-2:0 cases written: {len(cases)} → results/underdog_2_0.csv")
