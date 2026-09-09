#!/usr/bin/env python3
"""
lol_series_alert.py — сповіщає, коли на Polymarket складається ситуація для входу:
  BO5 тір-1, рахунок 0:1 або 0:2 з точки зору PRE-MATCH фаворита (тобто веде андердог),
  ціна лідера серії нижча за fair на ≥ EDGE, глибина ≥ MIN_DEPTH.

Логіка без жодних сторонніх фідів: рахунок серії читається з САМОГО Polymarket —
з ринків "Game N Winner", які вже resolved (closed + outcomePrices 1/0).

  pre-match фаворит  = сторона, дорожча на ринку серії ДО першої гри (запам'ятовується при першому
                       побаченні події і зберігається в state.json)
  рахунок            = скільки Game-N ринків resolved на чию користь
  fair(лідер)        = 1 − (0.5*realized_state + 0.5*independence(p_pre, рахунок))
                       realized_state за замовчуванням з нашого дослідження 2026 (0:1 → .344, 0:2 → .088)
  ціна лідера        = best ask на ринку серії
  глибина            = розмір на best ask

Запуск:
  python3 -u lol_series_alert.py --once            # один прохід, друкує все, що бачить (перевірка парсингу)
  python3 -u lol_series_alert.py --dry-run         # цикл без Telegram
  python3 -u lol_series_alert.py                   # робочий режим (шле в Telegram)

Env: TG_TOKEN, TG_CHAT (у .env). Без них шле тільки в stdout.
УВАГА: скрипт НІЧОГО не купує. Він тільки сповіщає. Рішення і ордер — вручну.
"""
import argparse, json, os, time
from datetime import datetime, timezone
from pathlib import Path
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
STATE = Path("alert_state.json")
UA = {"User-Agent": "lol-series-alert/0.1"}

# тір-1 ліги (у slug події); тір-2 виключено — там ефекту не знайдено
TIER1 = ("lck", "lpl", "lec", "lta", "lcs", "lcp", "msi", "worlds", "first-stand")
EXCLUDE = ("lck-cl", "challengers", "academy", "ldl", "lfl", "nlc", "tcl", "prime", "ultraliga", "hitpoint", "lit", "les", "lplol", "cblol-academy")
# реалізовані частоти перемоги ФАВОРИТА зі стану (дослідження Polymarket 2026, Primary)
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
        except Exception as e:
            print(f"[http] {url} {e}")
        time.sleep(2)
    return None

def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}

def save_state(s):
    STATE.write_text(json.dumps(s, indent=1))

def tg(msg, dry):
    print(msg, flush=True)
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if dry or not tok or not chat: return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
    except Exception as e:
        print(f"[tg] {e}")

def parse_prices(m):
    """outcomePrices/outcomes приходять як JSON-рядки"""
    def j(x):
        if isinstance(x, str):
            try: return json.loads(x)
            except Exception: return []
        return x or []
    return j(m.get("outcomes")), [float(p) for p in j(m.get("outcomePrices")) or []]

def book_ask(token_id):
    """best ask і розмір на ньому для token_id"""
    b = get(f"{CLOB}/book", token_id=token_id)
    if not b or not b.get("asks"): return None, None
    # asks відсортовані від гіршої до кращої на CLOB — беремо мінімальну ціну
    best = min(b["asks"], key=lambda a: float(a["price"]))
    return float(best["price"]), float(best["size"])

def series_state(ev):
    """повертає (markets_by_kind, score) — рахунок за resolved Game-N ринками"""
    ml, games = None, {}
    for m in ev.get("markets", []):
        q = (m.get("question") or "").lower(); sl = (m.get("slug") or "").lower()
        if "game" in q and "winner" in q:
            n = next((int(c) for c in q.replace("game", " ").split() if c.isdigit()), None)
            if n: games[n] = m
        elif any(k in q for k in ("win the series", "series winner", "moneyline")) or sl.endswith("-ml"):
            ml = m
        elif ml is None and "vs" in q and "handicap" not in q and "total" not in q:
            ml = m  # запасний варіант: основний ринок події
    score = {}
    for n, m in sorted(games.items()):
        outs, prices = parse_prices(m)
        if not m.get("closed") or not prices or max(prices) < 0.99: continue
        w = outs[prices.index(max(prices))]
        score[w] = score.get(w, 0) + 1
    return ml, games, score

def scan(args, st):
    evs = get(f"{GAMMA}/events", closed="false", limit=200, tag_slug="esports") or []
    evs += get(f"{GAMMA}/events", closed="false", limit=200, series_slug="lol") or []
    seen = set(); out = []
    for ev in evs:
        slug = (ev.get("slug") or "").lower()
        if ev.get("id") in seen: continue
        seen.add(ev.get("id"))
        if not slug.startswith("lol-") and "league-of-legends" not in slug: continue
        title = ev.get("title") or slug
        tier1 = any(k in slug or k in title.lower() for k in TIER1) and not any(k in slug or k in title.lower() for k in EXCLUDE)
        ml, games, score = series_state(ev)
        if ml is None or len(games) < 3:  # не BO5
            continue
        outs, prices = parse_prices(ml)
        if len(outs) != 2 or len(prices) != 2: continue
        key = str(ev.get("id"))
        rec = st.setdefault(key, {})
        # pre-match фаворит: фіксуємо, доки жодна гра не resolved
        if not score and "pre" not in rec:
            rec["pre"] = {"fav": outs[prices.index(max(prices))], "p": max(prices), "title": title, "slug": slug}
            print(f"[pre] {title}: фаворит {rec['pre']['fav']} @ {rec['pre']['p']:.2f}")
        pre = rec.get("pre")
        line = f"{title} | tier1={tier1} | score={score or '0:0'} | pre={pre['fav'] + ' ' + format(pre['p'], '.2f') if pre else '—'}"
        out.append(line)
        if args.once: print("  " + line)
        if not (tier1 and pre and score): continue

        fav, und = pre["fav"], [o for o in outs if o != pre["fav"]]
        if not und: continue
        und = und[0]
        fw, lw = score.get(fav, 0), score.get(und, 0)
        state = f"{fw}:{lw}"
        if state not in REALIZED:      # торгуємо лише 0:1 і 0:2
            continue
        p_pre = pre["p"]
        fair_fav = 0.5 * REALIZED[state] + 0.5 * p_series(p_pre, fw, lw)
        fair_leader = 1 - fair_fav
        # ціна і глибина на стороні лідера (андердога, що веде)
        toks = ml.get("clobTokenIds")
        toks = json.loads(toks) if isinstance(toks, str) else toks
        if not toks or len(toks) != 2: continue
        tok_leader = toks[outs.index(und)]
        ask, size = book_ask(tok_leader)
        if ask is None: continue
        edge = fair_leader - ask
        cost = size * ask
        ok = edge >= args.edge and cost >= args.min_depth and 0.15 <= ask <= 0.85
        tag = f"{title}|{state}"
        if args.once or args.verbose:
            print(f"  → {state} лідер={und} ask={ask:.3f} fair={fair_leader:.3f} edge={edge:+.3f} depth=${cost:.0f} {'ALERT' if ok else ''}")
        if ok and rec.get("alerted") != tag:
            rec["alerted"] = tag
            tg(f"<b>LoL series entry</b>\n{title}\nРахунок {state} — веде <b>{und}</b> (pre-match андердог, фаворит {fav} був {p_pre:.0%})\n"
               f"Ask лідера <b>{ask:.2f}</b>, fair {fair_leader:.2f}, edge <b>{edge*100:+.0f}¢</b>\n"
               f"Глибина на ask ≈ ${cost:.0f} → розмір ≤ ${cost*0.5:.0f}\n"
               f"https://polymarket.com/event/{pre['slug']}", args.dry_run)
    return out

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", type=float, default=0.08, help="мінімальна перевага в ціні лідера")
    ap.add_argument("--min-depth", type=float, default=300, help="мінімальний $ на best ask лідера")
    ap.add_argument("--interval", type=int, default=120, help="секунд між проходами")
    ap.add_argument("--once", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if Path(".env").exists():
        for ln in Path(".env").read_text().splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
    st = load_state()
    while True:
        try:
            rows = scan(a, st); save_state(st)
            print(f"[{datetime.now(timezone.utc):%H:%M:%S}] BO5-подій у полі зору: {len(rows)}", flush=True)
        except Exception as e:
            print(f"[loop] {type(e).__name__}: {e}", flush=True)
        if a.once: break
        time.sleep(a.interval)
