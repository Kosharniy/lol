#!/usr/bin/env python3
"""tier_test.py — регресійні тести фільтра тір-1/тір-2. Запускати після КОЖНОЇ зміни TIER1/EXCLUDE:
    python3 tier_test.py
Причина існування: 18.09.2026 слово "regional" у EXCLUDE перекрило "LPL Regional Finals" —
тір-1 матч (Team WE vs JD Gaming, 0:1, ціна 0.63, глибина $12.9k) мовчки пішов у тір-2 без алерту.
"""
import sys
from pathlib import Path
ns = {}
exec(compile(Path(__file__).with_name("lol_series_alert.py").read_text().split("if __name__")[0], "a", "exec"), ns)

CASES = [
    # тір-1
    ("LoL: Team WE vs JD Gaming (BO5) - LPL Regional Finals Playoffs", True),
    ("LoL: Invictus Gaming vs JD Gaming (BO5) - LPL Regional Finals Playoffs", True),
    ("LoL: Shopify Rebellion vs Sentinels (BO5) - LCS Playoffs", True),
    ("LoL: Natus Vincere vs Movistar KOI (BO5) - LEC Playoffs", True),
    ("LoL: Gen.G vs Hanwha Life Esports (BO5) - LCK Playoffs", True),
    ("LoL: Bilibili Gaming vs Anyone's Legend (BO5) - LPL Playoffs", True),
    ("LoL: X vs Y (BO5) - World Championship Play-In", True),
    ("LoL: X vs Y (BO5) - Worlds Quarterfinals", True),
    ("LoL: X vs Y (BO5) - Mid-Season Invitational", True),
    ("LoL: X vs Y (BO5) - LTA North Playoffs", True),
    ("LoL: X vs Y (BO5) - LCP Playoffs", True),
    # тір-2
    ("LoL: T1 Academy vs Dplus KIA Challengers (BO5) - LCK Challengers League Playoffs", False),
    ("LoL: Cupid vs Maryville (BO5) - North American Challengers League Playoffs", False),
    ("LoL: A vs B (BO5) - Prime League 1st Division Promotion Playoffs", False),
    ("LoL: Esprit Shonen vs Skillcamp (BO5) - LFL Promotion Playoffs", False),
    ("LoL: BIG vs Movistar KOI Fenix (BO5) - World Star Challengers Invitational EMEA Qualifier Playoffs", False),
    ("LoL: Winthrop University vs Maryville University (BO5) - North America", False),
    ("LoL: Pyramid IV vs Baam (BO5) - Arabian League Playoffs", False),
    ("LoL: The Ruddy Sack vs Verdant (BO5) - NLC Playoffs", False),
    ("LoL: LOUD vs paiN Gaming (BO5) - CBLOL Playoffs", False),
    ("LoL: A vs B (BO5) - LDL Playoffs", False),
    ("LoL: A vs B (BO5) - Hitpoint Masters Playoffs", False),
    ("LoL: A vs B (BO5) - Rift Legends Playoffs", False),
    ("LoL: A vs B (BO5) - LPLOL Playoffs", False),
]

bad = [(t, ns["is_tier1"](t), e) for t, e in CASES if ns["is_tier1"](t) != e]
for t, got, exp in bad:
    print(f"FAIL got={got} expected={exp} | {t}")
print(f"{len(CASES) - len(bad)}/{len(CASES)} пройдено")
sys.exit(1 if bad else 0)
