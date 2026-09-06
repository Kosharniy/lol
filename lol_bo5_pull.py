#!/usr/bin/env python3
"""
lol_bo5_pull.py — збирає дані для перевірки H4 (series-state) з ринковим prior.

  1) Leaguepedia (Special:CargoExport): усі ігри ScoreboardGames з 2022-06 → data/lp_games.csv
     + Tournaments (tier/league) → data/lp_tournaments.csv
  2) OddsPapi: фікстури LoL за період [--from, --to] → data/op_fixtures.csv
     + повна історія цін Pinnacle+Polymarket по кожній фікстурі → data/op_prices.csv
        (усі line moves з timestamp; для Polymarket це, ймовірно, включає in-play — перевірити)

Запуск:
  export ODDSPAPI_KEY=...            # ключ НЕ в коді і не в git
  pip install requests pandas rapidfuzz --break-system-packages
  python3 lol_bo5_pull.py --from 2025-01-01 --to 2026-09-06

Код НЕ тестований проти живого OddsPapi (у мене немає мережевого доступу до api.oddspapi.io) —
усі назви полів взяті з офіційного блогу/доків OddsPapi (v4): /sports, /fixtures (from/to ≤10 днів),
/markets?sportId, /historical-odds?fixtureId&bookmakers (≤3 букмекери). Якщо поле відрізняється —
скрипт пише raw JSON перших відповідей у data/raw/ для звірки.
"""
import argparse, csv, json, os, sys, time
from datetime import date, datetime, timedelta
from pathlib import Path
import requests

LP = "https://lol.fandom.com/wiki/Special:CargoExport"
OP = "https://api.oddspapi.io/v4"
DATA = Path("data"); RAW = DATA / "raw"; DATA.mkdir(exist_ok=True); RAW.mkdir(exist_ok=True)

def months(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
        yield cur, nxt
        cur = nxt

# ---------------------------------------------------------------- Leaguepedia
def pull_leaguepedia(start="2022-06-01"):
    out = DATA / "lp_games.csv"
    if out.exists():
        print("[LP] exists, skip:", out); return
    rows = []
    s = datetime.strptime(start, "%Y-%m-%d").date()
    for a, b in months(s, date.today()):
        offset = 0
        while True:
            params = {
                "tables": "ScoreboardGames=SG",
                "fields": "SG.MatchId,SG.N_GameInMatch,SG.Winner,SG.Team1,SG.Team2,SG.DateTime_UTC,SG.OverviewPage,SG.Patch",
                "where": f"SG.DateTime_UTC>='{a}' AND SG.DateTime_UTC<'{b}'",
                "order_by": "SG.DateTime_UTC,SG.MatchId", "limit": 5000, "offset": offset, "format": "json",
            }
            r = requests.get(LP, params=params, timeout=60, headers={"User-Agent": "bo5-research/0.1"})
            if r.status_code != 200:
                print("[LP] http", r.status_code, a, offset); time.sleep(10); continue
            j = r.json()
            for x in j:
                rows.append({"match_id": x["MatchId"], "game_n": x["N GameInMatch"], "winner": x["Winner"],
                             "team1": x["Team1"], "team2": x["Team2"], "dt_utc": x["DateTime UTC"],
                             "page": x["OverviewPage"], "patch": x.get("Patch")})
            print(f"[LP] {a} offset={offset} got={len(j)} total={len(rows)}")
            if len(j) < 5000: break
            offset += 5000; time.sleep(2)
        time.sleep(2)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    # tournaments meta
    params = {"tables": "Tournaments=T",
              "fields": "T.OverviewPage,T.TournamentLevel,T.Region,T.League,T.IsPlayoffs,T.DateStart",
              "where": "T.DateStart>='2022-05-01'", "limit": 5000, "format": "json"}
    j = requests.get(LP, params=params, timeout=60, headers={"User-Agent": "bo5-research/0.1"}).json()
    with (DATA / "lp_tournaments.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["page", "level", "region", "league", "playoffs", "date_start"]); w.writeheader()
        for t in j:
            w.writerow({"page": t["OverviewPage"], "level": t.get("TournamentLevel"), "region": t.get("Region"),
                        "league": t.get("League"), "playoffs": t.get("IsPlayoffs"), "date_start": t.get("DateStart")})
    print("[LP] done", len(rows))

# ---------------------------------------------------------------- OddsPapi
def op_get(path, key, **params):
    params["apiKey"] = key
    for attempt in range(6):
        r = requests.get(f"{OP}/{path}", params=params, timeout=60)
        if r.status_code == 429:
            wait = 5 * (attempt + 1); print(f"[OP] 429 on {path}, sleep {wait}s"); time.sleep(wait); continue
        if r.status_code != 200:
            print(f"[OP] http {r.status_code} {path} {params.get('fixtureId','')} {r.text[:200]}"); return None
        return r.json()
    return None

def pull_oddspapi(key, d_from, d_to, bookmakers="pinnacle,polymarket"):
    sports = op_get("sports", key)
    (RAW / "sports.json").write_text(json.dumps(sports, indent=1))
    lol = [s for s in sports if "league of legends" in (s.get("sportName", "") + s.get("sportSlug", "")).lower()
           or "lol" == s.get("sportSlug", "").lower()]
    if not lol:
        print("[OP] LoL sportId not found; see data/raw/sports.json and pass --sport-id"); return
    sport_id = lol[0]["sportId"]; print("[OP] LoL sportId =", sport_id, lol[0].get("sportName"))
    markets = op_get("markets", key, sportId=sport_id) or []
    (RAW / "markets.json").write_text(json.dumps(markets, indent=1))
    # fixtures in ≤10-day windows
    fx_out = DATA / "op_fixtures.csv"; fixtures = []
    a = datetime.strptime(d_from, "%Y-%m-%d").date(); end = datetime.strptime(d_to, "%Y-%m-%d").date()
    while a <= end:
        b = min(a + timedelta(days=9), end)
        j = op_get("fixtures", key, sportId=sport_id, **{"from": str(a), "to": str(b)}) or []
        if not (RAW / "fixtures_sample.json").exists() and j:
            (RAW / "fixtures_sample.json").write_text(json.dumps(j[:3], indent=1))
        fixtures.extend(j); print(f"[OP] fixtures {a}..{b}: {len(j)} (total {len(fixtures)})")
        a = b + timedelta(days=1); time.sleep(0.3)
    keys = ["fixtureId", "startTime", "tournamentId", "tournamentName", "categoryName",
            "participant1Id", "participant1Name", "participant2Id", "participant2Name", "statusId"]
    with fx_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore"); w.writeheader(); w.writerows(fixtures)
    # historical odds per fixture (append mode → можна перезапускати)
    pr_out = DATA / "op_prices.csv"; done = set()
    if pr_out.exists():
        with pr_out.open(encoding="utf-8") as f:
            for row in csv.DictReader(f): done.add(row["fixture_id"])
    newfile = not pr_out.exists()
    with pr_out.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fixture_id", "bookmaker", "market_id", "outcome_id", "player", "price", "limit", "recorded_at"])
        if newfile: w.writeheader()
        for i, fx in enumerate(fixtures):
            fid = fx["fixtureId"]
            if fid in done: continue
            j = op_get("historical-odds", key, fixtureId=fid, bookmakers=bookmakers)
            if j is None: continue
            if not (RAW / "hist_sample.json").exists():
                (RAW / "hist_sample.json").write_text(json.dumps(j, indent=1)[:200000])
            n = 0
            for bm, bd in (j.get("bookmakers") or {}).items():
                for mid, md in (bd.get("markets") or {}).items():
                    for oid, od in (md.get("outcomes") or {}).items():
                        for pl, hist in (od.get("players") or {}).items():
                            for s in hist:
                                w.writerow({"fixture_id": fid, "bookmaker": bm, "market_id": mid, "outcome_id": oid,
                                            "player": pl, "price": s.get("price"), "limit": s.get("limit"),
                                            "recorded_at": s.get("createdAt")}); n += 1
            if i % 20 == 0: print(f"[OP] {i}/{len(fixtures)} {fx.get('participant1Name')} vs {fx.get('participant2Name')} rows={n}")
            time.sleep(0.3)
    print("[OP] done")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default="2025-01-01")
    ap.add_argument("--to", dest="d_to", default=str(date.today()))
    ap.add_argument("--skip-lp", action="store_true"); ap.add_argument("--skip-op", action="store_true")
    a = ap.parse_args()
    if not a.skip_lp: pull_leaguepedia()
    if not a.skip_op:
        key = os.environ.get("ODDSPAPI_KEY")
        if not key: sys.exit("set ODDSPAPI_KEY env var")
        pull_oddspapi(key, a.d_from, a.d_to)
