#!/usr/bin/env python3
"""
lol_series_alert.py — сповіщає, коли на Polymarket складається ситуація для входу за правилом
"фаворит відстає у BO5" (дослідження вересня 2026: Polymarket, Primary 2026, n=98, z≈-3.1).

Умови входу (усі мають збігтися):
  • BO5, тір-1 ліга (LCK, LPL, LEC, LTA, LCS, LCP, MSI, Worlds, First Stand) — не Challengers/Academy/ERL
  • рахунок 0:1 або 0:2 з точки зору PRE-MATCH фаворита (тобто веде андердог)
  • ask лідера серії нижчий за fair щонайменше на EDGE (за замовчуванням 8¢)
  • глибина на best ask ≥ MIN_DEPTH ($300), ціна в межах 0.15–0.85

Поля Gamma перевірені на живій події 10.09.2026:
  events?series_slug=league-of-legends&closed=false&order=startDate&ascending=false
  event.score = "000-000|1-1|Bo5" → середня частина = рахунок серії в порядку event.teams (== порядку outcomes)
  event.live / event.ended / event.period ("3/5")
  ринок серії: markets[].sportsMarketType == "moneyline" (groupItemTitle "Match Winner")
  clobTokenIds у порядку outcomes; ask з https://clob.polymarket.com/book?token_id=…

Запуск:
  python3 -u lol_series_alert.py --once --verbose   # перевірка: покаже всі live BO5 і розрахунок
  python3 -u lol_series_alert.py --dry-run          # цикл без Telegram
  python3 -u lol_series_alert.py                    # робочий режим (TG_TOKEN, TG_CHAT з .env)

Скрипт НІЧОГО не купує — тільки сповіщає.
"""
import argparse, json, os, re, time
from datetime import datetime, timezone
from pathlib import Path
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
STATE = Path("alert_state.json")
SIGNALS = Path("signals.csv")   # forward-test: усі ситуації 0:1/0:2 + результат серії
UA = {"User-Agent": "lol-series-alert/0.2"}

TIER1 = ("lck", "lpl", "lec", "lta", "lcs", "lcp", "msi", "worlds", "first stand", "ewc")
EXCLUDE = ("challengers", "academy", "ldl", "lfl", "nlc", "tcl", "prime league", "ultraliga", "hitpoint",
           "cblol", "lplol", "arabian", "circuito", "balkan", "elite series", "greek", "nacl",
           "regional", "rift legends", "master flow", "liga ")
# реалізована частота перемоги ФАВОРИТА з цього стану (Polymarket Primary 2026: 0:1 → 22/64, 0:2 → 3/34)
REALIZED = {"0:1": 0.344, "0:2": 0.088}
# вхід дозволений ЛИШЕ у вікні між іграми: N хвилин від моменту, коли ми вперше побачили новий рахунок.
# Поза цим вікном іде наступна гра, і ціна вже містить ін-гейм інформацію, якої модель не бачить.
WINDOW_MIN = 14

def p_series(p, w, l):
    if w >= 3: return 1.0
    if l >= 3: return 0.0
    return p * p_series(p, w + 1, l) + (1 - p) * p_series(p, w, l + 1)

def get(url, **params):
    for _ in range(3):
        try:
            r = requests.get(url, params=params or None, headers=UA, timeout=20)
            if r.status_code == 200: return r.json()
            print(f"[http] {r.status_code} {url}")
        except Exception as e:
            print(f"[http] {url} {e}")
        time.sleep(2)
    return None

def jload(x, d=None):
    if isinstance(x, str):
        try: return json.loads(x)
        except Exception: return d
    return x if x is not None else d

def tg(msg, dry):
    print("\n" + re.sub(r"</?b>", "", msg) + "\n", flush=True)
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if dry: return
    if not tok or not chat:
        print("[tg] нема TG_TOKEN/TG_CHAT у .env — сповіщення лише в лог"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": msg, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200: print(f"[tg] {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[tg] {e}")

def is_tier1(title):
    t = (title or "").lower()
    return any(k in t for k in TIER1) and not any(k in t for k in EXCLUDE)

def parse_score(score):
    """'000-000|1-1|Bo5' → (1, 1, 'Bo5') у порядку event.teams"""
    if not score or "|" not in score: return None
    parts = score.split("|")
    if len(parts) < 3: return None
    m = re.match(r"^(\d+)-(\d+)$", parts[1].strip())
    return (int(m.group(1)), int(m.group(2)), parts[2].strip()) if m else None

def best_ask(token_id):
    b = get(f"{CLOB}/book", token_id=token_id)
    if not b or not b.get("asks"): return None, None
    a = min(b["asks"], key=lambda x: float(x["price"]))
    return float(a["price"]), float(a["size"])

SIG_COLS = ["ts", "event_id", "slug", "title", "tier1", "state", "leader", "fav", "p_pre",
            "ask", "fair", "edge", "depth", "best_ask", "best_edge", "best_depth", "best_ts", "n_obs",
            "alerted", "leader_won", "final_score"]

def log_signal(row):
    """один рядок на (event, state): перше спостереження + НАЙКРАЩИЙ edge за весь час стану.
    Повертає 'new' для нового рядка, 'better' якщо оновили найкращий, '' якщо без змін."""
    import csv
    rows = []
    if SIGNALS.exists():
        with SIGNALS.open(encoding="utf-8") as f: rows = list(csv.DictReader(f))
    key = (row["event_id"], row["state"]); status = ""
    cur = next((r for r in rows if (r["event_id"], r["state"]) == key), None)
    if cur is None:
        row.update({"best_ask": row["ask"], "best_edge": row["edge"], "best_depth": row["depth"],
                    "best_ts": row["ts"], "n_obs": 1})
        rows.append({c: row.get(c, "") for c in SIG_COLS}); status = "new"
    else:
        if cur.get("leader_won"): return ""          # серія вже розв'язана — не чіпаємо
        cur["n_obs"] = int(cur.get("n_obs") or 1) + 1
        # "краще" = більший edge при достатній глибині (інакше це неторгована ціна на порожній книзі)
        if row.get("in_window") and float(row["edge"]) > float(cur.get("best_edge") or -9) and float(row["depth"]) >= 300:
            cur.update({"best_ask": row["ask"], "best_edge": row["edge"], "best_depth": row["depth"],
                        "best_ts": row["ts"]}); status = "better"
    with SIGNALS.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SIG_COLS); w.writeheader()
        w.writerows([{c: r.get(c, "") for c in SIG_COLS} for r in rows])
    return status

def resolve_signals():
    """дописує результат серії для записів без leader_won"""
    import csv
    if not SIGNALS.exists(): return
    with SIGNALS.open(encoding="utf-8") as f: rows = list(csv.DictReader(f))
    pend = [r for r in rows if not r.get("leader_won")]
    if not pend: return
    changed = 0
    for r in pend:
        ev = get(f"{GAMMA}/events", slug=r["slug"])
        ev = ev[0] if isinstance(ev, list) and ev else None
        if not ev or not (ev.get("ended") or ev.get("closed")): continue
        sc = parse_score(ev.get("score"))
        ml = next((m for m in (ev.get("markets") or []) if m.get("sportsMarketType") == "moneyline"), None)
        if not ml: continue
        outs = jload(ml.get("outcomes"), []) or []
        prices = [float(x) for x in (jload(ml.get("outcomePrices"), []) or [])]
        if len(outs) != 2 or len(prices) != 2 or max(prices) < 0.99: continue
        winner = outs[prices.index(max(prices))]
        r["leader_won"] = "1" if winner == r["leader"] else "0"
        r["final_score"] = f"{sc[0]}-{sc[1]}" if sc else ""
        changed += 1
        print(f"[resolve] {r['title'][:60]} {r['state']} лідер={r['leader']} → {'WIN' if r['leader_won']=='1' else 'LOSS'} ({r['final_score']})")
    if changed:
        with SIGNALS.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SIG_COLS); w.writeheader(); w.writerows(rows)
        done = [r for r in rows if r.get("leader_won")]
        t1 = [r for r in done if r.get("tier1") == "True"]
        for label, g in (("тір-1", t1), ("усі", done)):
            if g:
                wins = sum(int(r["leader_won"]) for r in g)
                avg_ask = sum(float(r["ask"]) for r in g) / len(g)
                traded = [r for r in g if r.get("alerted") == "True"]
                bb = [float(r.get("best_ask") or r["ask"]) for r in g]
                avg_best = sum(bb) / len(bb) if bb else float("nan")
                print(f"[forward-test {label}] n={len(g)} лідер виграв {wins} ({wins/len(g):.0%}) | "
                      f"перший ask {avg_ask:.2f} → PnL {(wins/len(g) - avg_ask)*100:+.1f}¢ | "
                      f"кращий ask {avg_best:.2f} → PnL {(wins/len(g) - avg_best)*100:+.1f}¢")
                if traded:
                    tw = sum(int(r["leader_won"]) for r in traded)
                    ta = sum(float(r.get("best_ask") or r["ask"]) for r in traded) / len(traded)
                    print(f"[forward-test {label} — ТІЛЬКИ сигнали з алертом] n={len(traded)} виграв {tw} ({tw/len(traded):.0%}), "
                          f"ask {ta:.2f}, PnL/контракт {(tw/len(traded) - ta)*100:+.1f}¢")

def scan(a, st):
    evs = get(f"{GAMMA}/events", series_slug="league-of-legends", closed="false", limit=100,
              order="startDate", ascending="false") or []
    n_bo5 = 0; seen_states = {}; n_live = 0
    for ev in evs:
        sc = parse_score(ev.get("score"))
        if not sc or not sc[2].lower().startswith("bo5") or ev.get("ended"): continue
        n_bo5 += 1
        seen_states[f"{sc[0]}-{sc[1]}"] = seen_states.get(f"{sc[0]}-{sc[1]}", 0) + 1
        if ev.get("live"): n_live += 1
        title = ev.get("title") or ev.get("slug"); slug = ev.get("slug")
        teams = [t.get("name") for t in (ev.get("teams") or [])]
        ml = next((m for m in (ev.get("markets") or []) if m.get("sportsMarketType") == "moneyline"), None)
        if ml is None or len(teams) != 2: continue
        outs = jload(ml.get("outcomes"), []) or []
        prices = [float(x) for x in (jload(ml.get("outcomePrices"), []) or [])]
        toks = jload(ml.get("clobTokenIds"), []) or []
        if len(outs) != 2 or len(prices) != 2 or len(toks) != 2: continue
        w0, w1, _ = sc                                   # порядок event.teams == порядок outcomes
        tier1 = is_tier1(title)

        rec = st.setdefault(str(ev.get("id")), {})
        now = datetime.now(timezone.utc)
        seen = rec.setdefault("seen", {})            # коли вперше побачили кожен рахунок
        skey = f"{w0}-{w1}"
        if skey not in seen:
            # якщо ми ВПЕРШЕ бачимо цю подію і рахунок уже не 0-0 (рестарт посеред серії) —
            # ми не спостерігали переходу, тож вікно вважаємо давно закритим
            witnessed = bool(seen) or skey == "0-0"
            seen[skey] = now.isoformat(timespec="seconds") if witnessed else "1970-01-01T00:00:00+00:00"
            if not witnessed: print(f"  [warn] {title[:50]}: побачили {skey} без переходу — вікно закрите", flush=True)
        if w0 == 0 and w1 == 0 and "pre" not in rec:     # фіксуємо pre-match фаворита до першої гри
            i = prices.index(max(prices))
            rec["pre"] = {"fav_idx": i, "fav": outs[i], "p": max(prices), "title": title, "slug": slug}
            print(f"[pre] {title[:70]}: фаворит {outs[i]} @ {max(prices):.2f}")
        pre = rec.get("pre")

        if a.verbose or a.once:
            print(f"  {'T1' if tier1 else 't2'} | {title[:66]} | {w0}-{w1} | live={ev.get('live')} | "
                  f"pre={pre['fav'] + ' ' + format(pre['p'], '.2f') if pre else '—'}")
        if not pre: continue

        fi = pre["fav_idx"]; fw = (w0, w1)[fi]; lw = (w0, w1)[1 - fi]
        state = f"{fw}:{lw}"
        if state not in REALIZED: continue               # торгуємо лише 0:1 і 0:2
        leader = outs[1 - fi]
        fair_leader = 1 - (0.5 * REALIZED[state] + 0.5 * p_series(pre["p"], fw, lw))
        ask, size = best_ask(toks[1 - fi])
        if ask is None: continue
        edge = fair_leader - ask; depth = ask * size
        age_min = (now - datetime.fromisoformat(seen[skey])).total_seconds() / 60
        in_window = age_min <= a.window
        ok = (tier1 and in_window and edge >= a.edge and depth >= a.min_depth and 0.15 <= ask <= 0.85)
        why = "*** ALERT ***" if ok else ("(тір-2, лог)" if not tier1 else
              (f"(поза вікном: {age_min:.0f} хв від зміни рахунку — гра вже йде)" if not in_window else ""))
        print(f"  → {state} лідер={leader} ask={ask:.3f} fair={fair_leader:.3f} edge={edge*100:+.1f}¢ "
              f"depth=${depth:.0f} t+{age_min:.0f}хв {why}", flush=True)
        stt = log_signal({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event_id": str(ev.get("id")),
                          "slug": slug, "title": title, "tier1": tier1, "state": state, "leader": leader,
                          "fav": pre["fav"], "p_pre": round(pre["p"], 3), "ask": round(ask, 3),
                          "fair": round(fair_leader, 3), "edge": round(edge, 3), "depth": round(depth),
                          "in_window": in_window, "alerted": ok})
        if stt == "new": print("  [log] новий запис у signals.csv", flush=True)
        elif stt == "better": print(f"  [log] кращий edge за час стану: {edge*100:+.1f}¢ @ {ask:.3f}", flush=True)
        if not tier1: continue
        tag = state
        if ok and rec.get("alerted") != tag:
            rec["alerted"] = tag
            tg(f"<b>LoL BO5 — вхід за правилом</b>\n{title}\n"
               f"<i>Вікно між іграми: {age_min:.0f} хв від зміни рахунку</i>\n"
               f"Рахунок {state}: веде <b>{leader}</b> (pre-match андердог; фаворит {pre['fav']} був {pre['p']:.0%})\n"
               f"Ask лідера <b>{ask:.2f}</b> | fair {fair_leader:.2f} | edge <b>{edge*100:+.0f}¢</b>\n"
               f"Глибина на ask ≈ <b>${depth:.0f}</b> → розмір ≤ ${depth*0.5:.0f}\n"
               f"Тримати до кінця серії. https://polymarket.com/event/{slug}", a.dry_run)
    return n_bo5, n_live, seen_states

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", type=float, default=0.08)
    ap.add_argument("--min-depth", type=float, default=300)
    ap.add_argument("--window", type=float, default=WINDOW_MIN, help="хв від зміни рахунку, коли вхід дозволений")
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--once", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if Path(".env").exists():
        for ln in Path(".env").read_text().splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    while True:
        try:
            n, nlive, states = scan(a, st); STATE.write_text(json.dumps(st, indent=1)); resolve_signals()
            ss = ", ".join(f"{k}×{v}" for k, v in sorted(states.items())) or "—"
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] BO5: {n} (live {nlive}) | стани: {ss}", flush=True)
        except Exception as e:
            print(f"[loop] {type(e).__name__}: {e}", flush=True)
        if a.once: break
        time.sleep(a.interval)
