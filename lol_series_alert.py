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
UA = {"User-Agent": "lol-series-alert/0.2"}

TIER1 = ("lck", "lpl", "lec", "lta", "lcs", "lcp", "msi", "worlds", "first stand", "ewc")
EXCLUDE = ("challengers", "academy", "ldl", "lfl", "nlc", "tcl", "prime league", "ultraliga", "hitpoint",
           "cblol", "lplol", "arabian", "circuito", "balkan", "elite series", "greek", "nacl",
           "regional", "rift legends", "master flow", "liga ")
# реалізована частота перемоги ФАВОРИТА з цього стану (Polymarket Primary 2026: 0:1 → 22/64, 0:2 → 3/34)
REALIZED = {"0:1": 0.344, "0:2": 0.088}

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

def scan(a, st):
    evs = get(f"{GAMMA}/events", series_slug="league-of-legends", closed="false", limit=100,
              order="startDate", ascending="false") or []
    n_bo5 = 0
    for ev in evs:
        sc = parse_score(ev.get("score"))
        if not sc or not sc[2].lower().startswith("bo5") or ev.get("ended"): continue
        n_bo5 += 1
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
        if w0 == 0 and w1 == 0 and "pre" not in rec:     # фіксуємо pre-match фаворита до першої гри
            i = prices.index(max(prices))
            rec["pre"] = {"fav_idx": i, "fav": outs[i], "p": max(prices), "title": title, "slug": slug}
            print(f"[pre] {title[:70]}: фаворит {outs[i]} @ {max(prices):.2f}")
        pre = rec.get("pre")

        if a.verbose or a.once:
            print(f"  {'T1' if tier1 else 't2'} | {title[:66]} | {w0}-{w1} | live={ev.get('live')} | "
                  f"pre={pre['fav'] + ' ' + format(pre['p'], '.2f') if pre else '—'}")
        if not tier1 or not pre: continue

        fi = pre["fav_idx"]; fw = (w0, w1)[fi]; lw = (w0, w1)[1 - fi]
        state = f"{fw}:{lw}"
        if state not in REALIZED: continue               # торгуємо лише 0:1 і 0:2
        leader = outs[1 - fi]
        fair_leader = 1 - (0.5 * REALIZED[state] + 0.5 * p_series(pre["p"], fw, lw))
        ask, size = best_ask(toks[1 - fi])
        if ask is None: continue
        edge = fair_leader - ask; depth = ask * size
        ok = edge >= a.edge and depth >= a.min_depth and 0.15 <= ask <= 0.85
        print(f"  → {state} лідер={leader} ask={ask:.3f} fair={fair_leader:.3f} edge={edge*100:+.1f}¢ "
              f"depth=${depth:.0f} {'*** ALERT ***' if ok else ''}", flush=True)
        tag = f"{state}|{round(ask, 2)}"
        if ok and rec.get("alerted") != tag:
            rec["alerted"] = tag
            tg(f"<b>LoL BO5 — вхід за правилом</b>\n{title}\n"
               f"Рахунок {state}: веде <b>{leader}</b> (pre-match андердог; фаворит {pre['fav']} був {pre['p']:.0%})\n"
               f"Ask лідера <b>{ask:.2f}</b> | fair {fair_leader:.2f} | edge <b>{edge*100:+.0f}¢</b>\n"
               f"Глибина на ask ≈ <b>${depth:.0f}</b> → розмір ≤ ${depth*0.5:.0f}\n"
               f"Тримати до кінця серії. https://polymarket.com/event/{slug}", a.dry_run)
    return n_bo5

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", type=float, default=0.08)
    ap.add_argument("--min-depth", type=float, default=300)
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
            n = scan(a, st); STATE.write_text(json.dumps(st, indent=1))
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] BO5 у полі зору: {n}", flush=True)
        except Exception as e:
            print(f"[loop] {type(e).__name__}: {e}", flush=True)
        if a.once: break
        time.sleep(a.interval)
