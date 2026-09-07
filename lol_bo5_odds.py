#!/usr/bin/env python3
"""
lol_bo5_odds.py — тягне історію коефіцієнтів ТІЛЬКИ для BO5-серій, що матчаться з Leaguepedia.

Вхід:  data/lp_games.csv, data/lp_tournaments.csv, data/op_fixtures.csv, data/raw/markets.json
Вихід: data/op_prices.csv (append, resumable), data/bo5_fixture_match.csv (як заматчилось)

Кроки:
  python3 -u lol_bo5_odds.py --show-markets            # показати кандидатів на ринок "match winner"
  python3 -u lol_bo5_odds.py --pinnacle                 # Pinnacle для всіх заматчених BO5 (~1,600 запитів)
  python3 -u lol_bo5_odds.py --polymarket --tier Primary   # Polymarket по одному outcome (2 запити/серія), лише тір-1
  опційно: --market-id 123 якщо автовибір ринку помилився; --since 2025-01-01

Rate limit free tier ≈ 1 запит / 5 с → адаптивна пауза: стартуємо з 3 с, на 429 +1 с (до 12), на успіх −0.2 с (до 2 с).
"""
import argparse, csv, json, os, re, sys, time
from collections import defaultdict
from pathlib import Path
import pandas as pd, requests
try:
    from rapidfuzz import fuzz
except ImportError:
    from difflib import SequenceMatcher
    class fuzz:
        @staticmethod
        def token_set_ratio(a, b): return 100 * SequenceMatcher(None, a, b).ratio()

OP = "https://api.oddspapi.io/v4"; DATA = Path("data"); RAW = DATA / "raw"
KEY = os.environ.get("ODDSPAPI_KEY")

def norm(name):
    s = str(name).lower()
    s = re.sub(r"\b(esports?|e-sports|gaming|team|club|academy|blue|red|ltd|youth)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

# ---------------------------------------------------------------- BO5 from Leaguepedia
def load_bo5(since):
    g = pd.read_csv(DATA / "lp_games.csv")
    g = g[g.winner.isin([1, 2]) & g.team1.notna() & g.team2.notna()].copy()
    g["dt"] = pd.to_datetime(g.dt_utc)
    g = g.sort_values(["dt", "match_id", "game_n"]).drop_duplicates(["match_id", "game_n"])
    t = pd.read_csv(DATA / "lp_tournaments.csv").set_index("page")
    series = {}
    for r in g.itertuples(index=False):
        s = series.setdefault(r.match_id, {"tA": r.team1, "tB": r.team2, "date": r.dt, "page": r.page, "games": []})
        s["games"].append((r.game_n, r.team1 if r.winner == 1 else r.team2, r.dt))
    rows = []
    for mid, s in series.items():
        if s["date"] < pd.Timestamp(since): continue
        wA = wB = 0
        for n, win, dt in sorted(s["games"]):
            if win == s["tA"]: wA += 1
            elif win == s["tB"]: wB += 1
            if wA == 3 or wB == 3: break
        if not (wA == 3 or wB == 3): continue
        lvl = t.level.get(s["page"]) if s["page"] in t.index else None
        rows.append({"match_id": mid, "date": s["date"], "tA": s["tA"], "tB": s["tB"],
                     "level": "Primary" if lvl == "Primary" else ("Secondary" if lvl == "Secondary" else "Other")})
    return pd.DataFrame(rows)

# ---------------------------------------------------------------- match fixtures
def match_fixtures(bo5, fx):
    fx = fx.copy(); fx["start"] = pd.to_datetime(fx.startTime, utc=True).dt.tz_localize(None)
    fx["n1"] = fx.participant1Name.map(norm); fx["n2"] = fx.participant2Name.map(norm)
    out = []
    for r in bo5.itertuples(index=False):
        d0 = r.date.tz_localize(None) if r.date.tzinfo else r.date
        cand = fx[(fx.start - d0).abs() <= pd.Timedelta(hours=30)]
        nA, nB = norm(r.tA), norm(r.tB); best, bs, flip = None, 0, False
        for c in cand.itertuples(index=False):
            a = fuzz.token_set_ratio(nA, c.n1) + fuzz.token_set_ratio(nB, c.n2)
            b = fuzz.token_set_ratio(nA, c.n2) + fuzz.token_set_ratio(nB, c.n1)
            sc, fl = (a, False) if a >= b else (b, True)
            if sc > bs: bs, best, flip = sc, c, fl
        out.append({**r._asdict(), "fixture_id": best.fixtureId if best is not None and bs >= 150 else None,
                    "score": bs, "flip": flip, "op_p1": best.participant1Name if best is not None else None,
                    "op_p2": best.participant2Name if best is not None else None})
    m = pd.DataFrame(out); m.to_csv(DATA / "bo5_fixture_match.csv", index=False)
    print(f"BO5 series: {len(m)}  matched: {m.fixture_id.notna().sum()}  "
          f"(Primary {m[m.level=='Primary'].fixture_id.notna().sum()}/{(m.level=='Primary').sum()}, "
          f"Secondary {m[m.level=='Secondary'].fixture_id.notna().sum()}/{(m.level=='Secondary').sum()})")
    return m

# ---------------------------------------------------------------- winner market
def winner_market(market_id=None, show=False):
    ms = json.loads((RAW / "markets.json").read_text())
    if show:
        for x in ms:
            nm = str(x.get("marketName", "")); outs = x.get("outcomes", [])
            if re.search(r"win|moneyline|1x2|result|head", nm, re.I) and 2 <= len(outs) <= 3:
                print(x.get("marketId"), "|", nm, "|", [(o.get("outcomeId"), o.get("outcomeName")) for o in outs])
        return None
    if market_id:
        for x in ms:
            if str(x.get("marketId")) == str(market_id): return x
        sys.exit("market id not found")
    for pat in (r"^(match )?winner$", r"^moneyline", r"winner", r"^1x2|match result|head.?to.?head"):
        for x in ms:
            if re.search(pat, str(x.get("marketName", "")), re.I) and 2 <= len(x.get("outcomes", [])) <= 3:
                return x
    sys.exit("winner market not found — run --show-markets and pass --market-id")

# ---------------------------------------------------------------- HTTP with adaptive rate limit
class Client:
    def __init__(s): s.sleep = 3.0; s.ok = s.err429 = 0
    def get(s, path, **params):
        params["apiKey"] = KEY
        for _ in range(8):
            time.sleep(s.sleep)
            r = requests.get(f"{OP}/{path}", params=params, timeout=60)
            if r.status_code == 429:
                s.err429 += 1; s.sleep = min(s.sleep + 1.0, 12.0); continue
            s.ok += 1; s.sleep = max(s.sleep - 0.2, 2.0)
            if r.status_code != 200:
                return {"_http": r.status_code, "_text": r.text[:200]}
            return r.json()
        return {"_http": 429, "_text": "gave up"}

def flatten(fid, j, w):
    n = 0
    for bm, bd in (j.get("bookmakers") or {}).items():
        for mid, md in (bd.get("markets") or {}).items():
            for oid, od in (md.get("outcomes") or {}).items():
                for pl, hist in (od.get("players") or {}).items():
                    for snap in hist:
                        w.writerow({"fixture_id": fid, "bookmaker": bm, "market_id": mid, "outcome_id": oid, "player": pl,
                                    "price": snap.get("price"), "limit": snap.get("limit"), "recorded_at": snap.get("createdAt")}); n += 1
    return n

def pull(m, mode, tier, market):
    pr_out = DATA / "op_prices.csv"; done = set()
    if pr_out.exists():
        with pr_out.open(encoding="utf-8") as f:
            for row in csv.DictReader(f): done.add((row["fixture_id"], row["bookmaker"]))
    newfile = not pr_out.exists()
    todo = m[m.fixture_id.notna()]
    if tier: todo = todo[todo.level == tier]
    todo = todo.sort_values("date", ascending=False)  # свіжі спершу
    cl = Client(); t0 = time.time()
    with pr_out.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fixture_id", "bookmaker", "market_id", "outcome_id", "player", "price", "limit", "recorded_at"])
        if newfile: w.writeheader()
        for i, r in enumerate(todo.itertuples(index=False)):
            fid = r.fixture_id
            if mode == "pinnacle":
                if (fid, "pinnacle") in done: continue
                j = cl.get("historical-odds", fixtureId=fid, bookmakers="pinnacle")
                if "_http" in j: print(f"[{i}] {fid} http {j['_http']} {j['_text']}"); continue
                n = flatten(fid, j, w)
                if i < 3 and not (RAW / "hist_pinnacle_sample.json").exists(): (RAW / "hist_pinnacle_sample.json").write_text(json.dumps(j)[:100000])
            else:  # polymarket: one outcome per call
                if (fid, "polymarket") in done: continue
                n = 0
                for o in market["outcomes"][:2]:
                    j = cl.get("historical-odds", fixtureId=fid, bookmakers="polymarket", outcomeId=o["outcomeId"])
                    if "_http" in j: print(f"[{i}] {fid} pm outcome {o['outcomeId']} http {j['_http']} {j['_text']}"); continue
                    if not (RAW / "hist_polymarket_sample.json").exists(): (RAW / "hist_polymarket_sample.json").write_text(json.dumps(j)[:100000])
                    n += flatten(fid, j, w)
            f.flush()
            if i % 10 == 0:
                print(f"[{i}/{len(todo)}] {r.date.date()} {r.tA} vs {r.tB} rows={n} sleep={cl.sleep:.1f}s ok={cl.ok} 429={cl.err429} "
                      f"elapsed={int(time.time()-t0)}s eta={int((time.time()-t0)/(i+1)*(len(todo)-i-1))}s")
    print("done")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-markets", action="store_true"); ap.add_argument("--market-id")
    ap.add_argument("--pinnacle", action="store_true"); ap.add_argument("--polymarket", action="store_true")
    ap.add_argument("--tier", choices=["Primary", "Secondary"]); ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--refresh-fixtures", action="store_true", help="перетягнути /fixtures з усіма полями (hasOdds тощо)")
    ap.add_argument("--skip-no-odds", action="store_true", help="пропускати фікстури з hasOdds=false")
    ap.add_argument("--probe", type=int, default=0, help="N: зробити N запитів по hasOdds=false фікстурах і показати, чи є там дані")
    a = ap.parse_args()
    if a.show_markets: winner_market(show=True); sys.exit()
    if not KEY: sys.exit("set ODDSPAPI_KEY")
    if a.refresh_fixtures:
        from datetime import date, datetime, timedelta
        cl = Client(); allf = []; d = datetime.strptime(a.since, "%Y-%m-%d").date(); end = date.today()
        while d <= end:
            b = min(d + timedelta(days=9), end)
            j = cl.get("fixtures", sportId=18, **{"from": str(d), "to": str(b)})
            if isinstance(j, list): allf.extend(j)
            print(f"[fixtures] {d}..{b}: {len(j) if isinstance(j, list) else j}")
            d = b + timedelta(days=1)
        flat = [{k: v for k, v in x.items() if not isinstance(v, (dict, list))} for x in allf]
        pd.DataFrame(flat).to_csv(DATA / "op_fixtures.csv", index=False); print("saved", len(flat))
    fx = pd.read_csv(DATA / "op_fixtures.csv")
    if "hasOdds" in fx.columns:
        print(f"[fixtures] hasOdds: true={int((fx.hasOdds==True).sum())} false={int((fx.hasOdds==False).sum())}")
    need = {"fixtureId", "startTime", "participant1Name", "participant2Name"}
    if not need.issubset(fx.columns) or fx.participant1Name.isna().all():
        sys.exit(f"op_fixtures.csv не має потрібних полів {need}; подивись data/raw/fixtures_sample.json і скажи мені назви полів")
    market = winner_market(a.market_id)
    print("[market]", market.get("marketId"), market.get("marketName"), [(o.get("outcomeId"), o.get("outcomeName")) for o in market.get("outcomes", [])])
    m = match_fixtures(load_bo5(a.since), fx)
    if "hasOdds" in fx.columns:
        m = m.merge(fx[["fixtureId", "hasOdds"]], left_on="fixture_id", right_on="fixtureId", how="left")
        mm = m[m.fixture_id.notna()]
        print(f"[match] matched BO5 with hasOdds=true: {int((mm.hasOdds==True).sum())}, false: {int((mm.hasOdds==False).sum())}")
        if a.probe:
            cl = Client(); hits = 0
            for r in mm[mm.hasOdds == False].head(a.probe).itertuples():
                j = cl.get("historical-odds", fixtureId=r.fixture_id, bookmakers="pinnacle")
                n = sum(len(h) for bd in (j.get("bookmakers") or {}).values() for md in (bd.get("markets") or {}).values()
                        for od in (md.get("outcomes") or {}).values() for h in (od.get("players") or {}).values()) if "_http" not in j else -1
                print(f"[probe] {r.date.date()} {r.tA} vs {r.tB} hasOdds=false → rows={n}"); hits += n > 0
            print(f"[probe] {hits}/{a.probe} фікстур з hasOdds=false мають історію → {'НЕ можна' if hits else 'можна'} пропускати"); sys.exit()
        if a.skip_no_odds: m = m[m.hasOdds != False]
    if a.pinnacle: pull(m, "pinnacle", a.tier, market)
    if a.polymarket: pull(m, "polymarket", a.tier, market)
