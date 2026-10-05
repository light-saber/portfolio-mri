#!/usr/bin/env python3
"""Portfolio MRI data generator.

Reads the latest Zerodha holdings snapshots (equity + MFs), computes the
Portfolio MRI metrics from refined-spec.md, and writes mr-data.json for the
dashboard. The data file is personal: it is gitignored and served behind nginx
basic auth. This script contains no portfolio numbers of its own.

Sources (canonical, refreshed by the morning cron / ad-hoc Kite pulls):
  - /root/.hermes/data/portfolio/zerodha-holdings-live.json
  - /root/.hermes/data/portfolio/mf-holdings-YYYY-MM-DD.json (newest)
Optional policy file (drives the Discipline grade):
  - /root/portfolio-mri/policy.json

Output:
  - /var/www/portfolio/mr-data.json  (what the dashboard fetches)
  - /root/portfolio-mri/mr-data.json  (local copy)
"""
import json
import math
import pathlib
import datetime
import sys

PORTFOLIO_DIR = pathlib.Path("/root/.hermes/data/portfolio")
POLICY_PATH = pathlib.Path("/root/portfolio-mri/policy.json")
OUT_PATH = pathlib.Path("/var/www/portfolio/mr-data.json")
LOCAL_OUT = pathlib.Path("/root/portfolio-mri/mr-data.json")
VESTED_POSITIONS = pathlib.Path("/root/.hermes/scripts/vested_tracker/positions.json")
VESTED_HISTORY = pathlib.Path("/root/.hermes/vested_tracker/history.jsonl")
USDINR_FALLBACK = 96.28

GOLD_SUFFIXES = ("GOLDBEES", "SILVERBEES", "SGB", "GOLDMONTHLY")
EQUITY_EXCHANGES = {"NSE", "BSE"}


def latest_mf_snapshot() -> pathlib.Path:
    hits = sorted(PORTFOLIO_DIR.glob("mf-holdings-*.json"))
    if not hits:
        raise FileNotFoundError("no mf-holdings snapshot found")
    return hits[-1]


def usd_inr_rate() -> tuple:
    """Live USD/INR from Yahoo, with the last known rate as fallback."""
    import urllib.request
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/INR=X"
           "?interval=1d&range=5d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            meta = json.loads(resp.read().decode())["chart"]["result"][0]["meta"]
        rate = meta.get("regularMarketPrice")
        if rate:
            return round(float(rate), 4), "live (Yahoo INR=X)"
    except Exception:
        pass
    return USDINR_FALLBACK, "fallback (last known)"


def load_vested():
    """US sleeve from the Vested tracker snapshot + latest priced history record."""
    if not (VESTED_POSITIONS.exists() and VESTED_HISTORY.exists()):
        return {"status": "needs-snapshot",
                "note": "Vested tracker snapshot or history missing; US sleeve not included."}
    pos = json.loads(VESTED_POSITIONS.read_text())
    records = [json.loads(l) for l in VESTED_HISTORY.read_text().splitlines() if l.strip()]
    if not records:
        return {"status": "needs-history",
                "note": "Vested history is empty; run the daily tracker to price the US sleeve."}
    rec = records[-1]
    prices = rec.get("holdings") or {}
    value_usd = float(rec.get("current_value") or 0.0)
    invested_usd = float(rec.get("invested") or 0.0)
    if not value_usd:
        return {"status": "needs-history",
                "note": "Latest Vested history record has no current_value."}

    rate, rate_src = usd_inr_rate()
    holdings = []
    for sym, info in sorted(prices.items()):
        v = float(info.get("value") or 0.0)
        holdings.append({
            "symbol": sym,
            "quantity": float(info.get("qty") or 0.0),
            "value_usd": round(v, 2),
            "value_inr": round(v * rate, 0),
            "weight_pct": round(v / value_usd * 100, 1) if value_usd else 0.0,
        })
    return {
        "status": "ready",
        "broker": "Vested Finance (DriveWealth)",
        "account": "US Stocks & ETFs",
        "as_of": rec.get("date"),
        "position_snapshot": pos.get("updated"),
        "quantity_snapshot": pos.get("source"),
        "usd_inr": rate,
        "usd_inr_source": rate_src,
        "buying_power_usd": round(float(pos.get("buying_power") or 0.0), 2),
        "flows_cum_usd": round(float(pos.get("flows_cum") or 0.0), 2),
        "value_usd": round(value_usd, 2),
        "invested_usd": round(invested_usd, 2),
        "pnl_usd": round(value_usd - invested_usd, 2),
        "pnl_pct": round((value_usd - invested_usd) / invested_usd * 100, 2) if invested_usd else None,
        "value_inr": round(value_usd * rate, 0),
        "invested_inr": round(invested_usd * rate, 0),
        "pnl_inr": round((value_usd - invested_usd) * rate, 0),
        "holdings": holdings,
        "note": ("Prices and quantities are as fresh as the Vested tracker: prices refresh "
                 "daily via Yahoo, quantities only when positions.json is updated after a "
                 "buy or sell. Per-symbol cost basis is not tracked, so per-holding return "
                 "is not shown."),
    }


def load_equity():
    p = PORTFOLIO_DIR / "zerodha-holdings-live.json"
    data = json.loads(p.read_text())
    return data.get("equity", [])


def load_mf():
    p = latest_mf_snapshot()
    data = json.loads(p.read_text())
    return data.get("mutual_funds", [])


def classify_eq(symbol: str) -> str:
    s = symbol.upper()
    if s.startswith("SGB") or s in ("GOLDBEES", "SILVERBEES"):
        return "gold"
    return "direct-equity"


def classify_mf(fund: str) -> str:
    f = fund.upper()
    if "INDEX" in f:
        return "index-mf"
    if "ELSS" in f:
        return "elss"
    if "BALANCED" in f or "HYBRID" in f:
        return "hybrid-mf"
    if "FOF" in f:
        return "fof-mf"
    return "active-mf"


def cv(values) -> float | None:
    vals = [v for v in values]
    n = len(vals)
    if n < 2:
        return None
    mean = sum(vals) / n
    if mean == 0:
        return None
    var = sum((v - mean) ** 2 for v in vals) / n
    return math.sqrt(var) / mean


def build_findings(summary, performance, us):
    """Plain-language diagnosis. The dashboard's job is to answer "so what",
    not just to display numbers. Each finding carries a severity so the page
    can lead with the one that matters.
    """
    t = summary["total"]
    sleeves = summary.get("sleeves") or {}
    contrib = summary.get("contribution") or {}
    conc = summary.get("concentration") or {}
    stress = summary.get("stress") or {}
    perf = performance if performance.get("status") == "ready" else {}
    beh = perf.get("behavior") or {}
    period = perf.get("period") or {}
    findings = []

    def sleeve_label(raw):
        out = raw.replace("-", " ")
        if out.endswith(" mf"):
            out = out[:-3].upper() + " MF"
        return out[:1].upper() + out[1:]

    def money(v):
        a = abs(v)
        return f"₹{a/1e5:,.2f} L" if a >= 1e5 else f"₹{a:,.0f}"

    ABBR = {
        "SGBJUN31I-GB": "SGB Jun-2031 (Sovereign Gold Bond)",
        "SGBFEB32IV": "SGB Feb-2032 (Sovereign Gold Bond)",
    }

    def friendly_name(n):
        return ABBR.get(n, n)

    # 1. headline
    since = period.get("since")
    findings.append({
        "id": "headline",
        "severity": "info",
        "title": f"You are up {money(t['pnl'])} on {money(t['invested'])} invested"
                  + (f", over {period.get('years')} years" if period.get("years") else ""),
        "detail": (
            f"That is {t['return_pct']}% on cost. Because your contributions arrived over "
            f"{period.get('years')} years rather than all at once, the figure that actually "
            f"compares against a benchmark is the annualised one, roughly "
            f"{perf.get('equity_xirr_pct')}% a year. The gap between the two is expected: the "
            "higher number ignores when each payment landed."
        ),
        "metric": t["return_pct"],
        "metric_kind": "return_pct",
    })

    # 2. what is carrying the result
    gainers = contrib.get("top_gainers") or []
    if gainers:
        top3 = gainers[:3]
        share = sum(g["pnl"] for g in top3) / t["pnl"] * 100 if t["pnl"] else 0
        findings.append({
            "id": "drivers",
            "severity": "info",
            "title": (f"The top {len(gainers)} holdings account for "
                        f"{share:.0f}% of your total gain"),
            "detail": (
                "Top contributors, in rupees: "
                + ", ".join(
                    f"{friendly_name(g['name'])} {money(g['pnl'])}" for g in top3)
                + "."
            ),
            "metric": round(share, 0),
            "metric_kind": "share_pct",
        })

    # 3. concentration risk, the actual MRI finding
    eff_n = conc.get("effective_n_direct_equity")
    biggest = sorted(
        (s for s in sleeves.values() if s["market"] > 0),
        key=lambda s: -s["weight"],
    )
    if biggest and biggest[0]["weight"] >= 35:
        findings.append({
            "id": "concentration",
            "severity": "watch",
            "title": f"{sleeve_label(biggest[0]['name'])} is {biggest[0]['weight']}% of the portfolio",
            "detail": (
                f"One bucket is carrying {biggest[0]['weight']}% of your portfolio. "
                + (f"Across your direct stocks the average holding behaves like roughly "
                   f"{round(eff_n)} equally weighted positions, so you hold fewer independent "
                   "bets than the count of tickers suggests. " if finite_or_none(eff_n) else "")
                + "Worth a rebalancing decision you make on purpose, rather than letting it happen by drift."
            ),
            "metric": biggest[0]["weight"],
            "metric_kind": "weight_pct",
        })
    elif finite_or_none(eff_n) and eff_n <= 8:
        findings.append({
            "id": "concentration",
            "severity": "watch",
            "title": f"Direct equity behaves like {eff_n} equal positions",
            "detail": "Lower is more concentrated. A handful of names is driving the sleeve.",
            "metric": eff_n,
            "metric_kind": "effective_n",
        })

    # 4. sleeve with the weakest return
    with_pnl = [s for s in sleeves.values() if s["invested"] > 0]
    if with_pnl:
        worst = min(with_pnl, key=lambda s: s["return_pct"])
        if worst["return_pct"] < 5:
            findings.append({
                "id": "weak-sleeve",
                "severity": "watch",
                "title": f"{sleeve_label(worst['name'])} has returned {worst['return_pct']}%",
                "detail": (
                    f"{money(worst['invested'])} went in, {money(worst['market'])} is there now. "
                    + ("This is the bucket to review: either the thesis needs revisiting or the "
                       "money is better deployed elsewhere."
                       if worst["pnl"] < 0 else
                       "Positive, but lagging every other bucket by a wide margin.")
                ),
                "metric": worst["return_pct"],
                "metric_kind": "return_pct",
            })

    # 4b. loss asymmetry: often the real message is "your losers are not the problem"
    losers_pnl = contrib.get("losers_pnl")
    winners_pnl = contrib.get("winners_pnl")
    if finite_or_none(losers_pnl) and finite_or_none(winners_pnl) and winners_pnl > 0:
        ratio = abs(losers_pnl) / winners_pnl * 100
        if ratio <= 5:
            findings.append({
                "id": "loss-asymmetry",
                "severity": "info",
                "title": (f"Your losing positions cost {money(abs(losers_pnl))} in total, "
                          f"about {ratio:.0f}% of what you gained"),
                "detail": (
                    f"{contrib.get('n_losers')} positions are below cost, but together they are a "
                    "rounding error next to the gains. The problem to solve here is not cutting "
                    "losers, it is concentration and cash drag."
                ),
                "metric": round(ratio, 1),
                "metric_kind": "rate_pct",
            })

    # 5. stress
    after = stress.get("est_portfolio_pnl_after")
    if finite_or_none(after) and finite_or_none(t["pnl"]):
        findings.append({
            "id": "stress",
            "severity": "info",
            "title": f"A 25% equity fall would take profit to {money(after)}",
            "detail": (
                f"That is {money(abs(after - t['pnl']))} below where you are now. Gold and cash "
                "are held flat in this scenario, so the defensive sleeves do less work here "
                "than they would in a real fall."
            ),
            "metric": round(after, 0),
            "metric_kind": "inr",
        })

    # 6. behaviour
    rate = beh.get("quick_sell_rate_pct")
    if finite_or_none(rate) and rate >= 40:
        findings.append({
            "id": "churn",
            "severity": "watch",
            "title": f"{rate}% of sold positions were closed within 90 days of buying",
            "detail": (
                f"{beh.get('sells_within_90d_of_first_buy')} of "
                f"{beh.get('symbols_ever_bought_and_sold')} symbols ever bought and sold. "
                "Short holds are the biggest drag on compounding after costs and taxes."
            ),
            "metric": rate,
            "metric_kind": "rate_pct",
        })

    # 7. uninvested cash, if the US sleeve is sitting on it
    if us and us.get("status") == "ready" and (us.get("buying_power_usd") or 0) > 500:
        findings.append({
            "id": "cash-drag",
            "severity": "watch",
            "title": f"${us['buying_power_usd']:,.0f} of your US sleeve is sitting in cash",
            "detail": (
                f"Roughly ₹{(us['buying_power_usd'] * (us.get('usd_inr') or 0)) / 1e5:,.2f} L "
                "is sitting in cash rather than working. Cash is not a portfolio bucket, it is "
                "a decision waiting to be made."
            ),
            "metric": round(us["buying_power_usd"], 0),
            "metric_kind": "usd",
        })

    order = {"watch": 0, "info": 1}
    findings.sort(key=lambda f: order.get(f["severity"], 2))
    return {
        "lead": findings[0] if findings else None,
        "watch_count": sum(1 for f in findings if f["severity"] == "watch"),
        "items": findings,
    }


def finite_or_none(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def build_summary(equity, mf, as_of, us=None):
    eq_rows = [
        {
            "symbol": e["symbol"],
            "sleeve": classify_eq(e["symbol"]),
            "invested_value": float(e.get("invested_value", 0) or 0),
            "market_value": float(e.get("market_value", 0) or 0),
            "unrealized_pnl": float(e.get("unrealized_pnl", 0) or 0),
            "return_pct": (
                round((e.get("unrealized_pnl", 0) / e.get("invested_value", 1)) * 100, 1)
                if e.get("invested_value")
                else 0.0
            ),
        }
        for e in equity
    ]
    mf_rows = [
        {
            "fund": m["fund"],
            "key": m.get("key", m.get("fund", "?")),
            "sleeve": classify_mf(m.get("fund", "")),
            "invested_value": float(m.get("invested_value", 0) or 0),
            "market_value": float(m.get("market_value", 0) or 0),
            "unrealized_pnl": float(m.get("unrealized_pnl", 0) or 0),
            "return_pct": (
                round((m.get("unrealized_pnl", 0) / m.get("invested_value", 1)) * 100, 1)
                if m.get("invested_value")
                else 0.0
            ),
        }
        for m in mf
    ]

    us_rows = []
    if us and us.get("status") == "ready":
        for h in us["holdings"]:
            inv = 0.0  # per-symbol cost basis not tracked; sleeve-level only
            us_rows.append({
                "symbol": h["symbol"],
                "sleeve": "us-equity",
                "currency": "USD",
                "invested_value": 0.0,
                "market_value": float(h["value_inr"] or 0.0),
                "unrealized_pnl": 0.0,
                "return_pct": None,
                "value_usd": h["value_usd"],
                "weight_pct": h["weight_pct"],
            })

    all_rows = eq_rows + mf_rows + us_rows
    # sleeve-level US figures carry the real P&L; per-holding rows are value-only
    if us and us.get("status") == "ready":
        all_rows.append({
            "symbol": "US sleeve (Vested)",
            "sleeve": "us-equity",
            "currency": "USD",
            "invested_value": float(us["invested_inr"] or 0.0),
            "market_value": 0.0,
            "unrealized_pnl": float(us["pnl_inr"] or 0.0),
            "return_pct": float(us["pnl_pct"]) if us.get("pnl_pct") is not None else None,
            "summary_row": True,
        })
    total_inv = sum(r["invested_value"] for r in all_rows)
    total_mv = sum(r["market_value"] for r in all_rows)
    total_pnl = sum(r["unrealized_pnl"] for r in all_rows)

    # sleeve rollups
    sleeves = {}
    for r in all_rows:
        s = sleeves.setdefault(
            r["sleeve"],
            {"name": r["sleeve"], "invested": 0.0, "market": 0.0, "pnl": 0.0},
        )
        s["invested"] += r["invested_value"]
        s["market"] += r["market_value"]
        s["pnl"] += r["unrealized_pnl"]
    for s in sleeves.values():
        s["weight"] = round(s["market"] / total_mv * 100, 1) if total_mv else 0.0
        s["return_pct"] = round(s["pnl"] / s["invested"] * 100, 1) if s["invested"] else 0.0

    # contribution matrix: top +/- by INR pnl.
    # Value-only rows (US per-holding, zero invested/P&L) are excluded so they do not
    # dilute the win rate or appear as zero-P&L slivers.
    contrib_rows = [r for r in all_rows if r["invested_value"] or r["unrealized_pnl"]]
    sorted_rows = sorted(contrib_rows, key=lambda r: -r["unrealized_pnl"])

    def row_out(r):
        return {
            "name": r["symbol"] if "symbol" in r else r["fund"],
            "sleeve": r["sleeve"],
            "pnl": round(r["unrealized_pnl"], 0),
            "return_pct": r["return_pct"],
            "invested_value": round(r["invested_value"], 0),
            "market_value": round(r["market_value"], 0),
            "weight_pct": round(r["market_value"] / total_mv * 100, 1) if total_mv else 0.0,
        }

    positive = [(r, r["unrealized_pnl"]) for r in sorted_rows if r["unrealized_pnl"] > 0]
    # sort losers worst-first: sorted_rows is descending, so the tail must be reversed
    # or the "top losers" list shows the six *smallest* losses instead of the largest.
    negative = sorted(
        [(r, r["unrealized_pnl"]) for r in sorted_rows if r["unrealized_pnl"] < 0],
        key=lambda rv: rv[1],
    )
    contribution = {
        "top_gainers": [row_out(r) for r, _ in positive[:8]],
        "top_losers": [row_out(r) for r, _ in negative[:6]],
        "winners_pnl": round(sum(v for _, v in positive), 0),
        "losers_pnl": round(sum(v for _, v in negative), 0),
        "win_rate_pct": round(len(positive) / len(contrib_rows) * 100, 1) if contrib_rows else 0,
        "n_holdings": len(contrib_rows),
        "n_winners": len(positive),
        "n_losers": len(negative),
        "n_flat": len(contrib_rows) - len(positive) - len(negative),
    }

    # concentration on direct equity sleeve (MF look-through is partial: later)
    eq_total_mv = sum(r["market_value"] for r in eq_rows)
    big = [r for r in eq_rows if eq_total_mv and r["market_value"] / eq_total_mv > 0.05]
    weights = [r["market_value"] / eq_total_mv for r in eq_rows if eq_total_mv and r["market_value"] > 0]
    effective_n = round(sum(w * w for w in weights) ** -1, 1) if weights else None

    # stress: -25% on every non-gold equity row (direct equity + MFs)
    def shocked(row, drop_pct):
        shocked_mv = row["market_value"] * (1 - drop_pct / 100.0)
        return shocked_mv - row["invested_value"]

    shock_drop = 25.0
    # US per-holding rows carry no cost basis, so shock the sleeve as one unit instead.
    shock_rows = [r for r in all_rows
                  if r["sleeve"] != "gold" and not r.get("summary_row") and r["invested_value"] > 0]
    shock_pnl = sum(shocked(r, shock_drop) for r in shock_rows)
    post_shock_pnl = total_pnl + sum(
        (r["market_value"] * (1 - shock_drop / 100.0)) - r["invested_value"] - r["unrealized_pnl"]
        for r in shock_rows
    )

    return {
        "as_of": as_of,
        "total": {
            "invested": round(total_inv, 0),
            "market_value": round(total_mv, 0),
            "pnl": round(total_pnl, 0),
            "return_pct": round(total_pnl / total_inv * 100, 1) if total_inv else 0.0,
        },
        "sleeves": sleeves,
        "contribution": contribution,
        "concentration": {
            "above_5pct": [
                {
                    "name": r["symbol"],
                    "weight_pct": round(r["market_value"] / eq_total_mv * 100, 1),
                    "pnl": round(r["unrealized_pnl"], 0),
                }
                for r in big
            ],
            "effective_n_direct_equity": effective_n,
        },
        "stress": {
            "scenario": f"equity -{shock_drop}% (gold and cash flat)",
            "est_portfolio_pnl_after": round(post_shock_pnl, 0),
        },
    }


def build_discipline(policy, equity, mf):
    if not policy:
        return {
            "status": "needs-policy",
            "grade": None,
            "note": "Discipline grades only score against a prospectively recorded policy. Define contribution schedule, targets, exit rules, quarterly review date in /root/portfolio-mri/policy.json, then grades activate next month.",
            "components": None,
        }
    comps = {}
    for key, meta in (
        ("contributions", ("Contributions", 0.35)),
        ("holding", ("Holding", 0.25)),
        ("rebalancing", ("Rebalancing", 0.20)),
        ("risk", ("Risk", 0.20)),
    ):
        cfg = policy.get(key, {})
        rate = cfg.get("score_rate", None)
        if rate is None:
            comps[key] = {"label": meta[0], "weight": meta[1], "score": None}
            continue
        comps[key] = {
            "label": meta[0],
            "weight": meta[1],
            "score": min(10.0, max(0.0, rate)),
        }
    observed = [c["score"] for c in comps.values() if c["score"] is not None]
    eligible = len(comps) and len(observed) == len(comps)
    if not observed:
        return {"status": "provisional", "grade": None, "components": comps,
                "note": "No component scored yet."}
    if not eligible:
        return {"status": "provisional", "grade": None, "components": comps,
                "note": "Some components unscored; grade provisional."}
    grade = sum(c["score"] * c["weight"] for c in comps.values())
    letter = "A" if grade >= 9 else "B+" if grade >= 8 else "B" if grade >= 7 else "C" if grade >= 6 else "D" if grade >= 4 else "F"
    return {
        "status": "graded",
        "grade": letter,
        "score": round(grade, 1),
        "components": comps,
        "note": "Trailing 12 months, compliance / eligible observations (see refined-spec.md).",
    }


def build_performance(equity, mf, as_of):
    """XIRR + behavioral metrics from the tradebook CSVs (downloaded from Console)."""
    import csv as _csv
    from datetime import date as _date

    eq_csv = PORTFOLIO_DIR / "tradebook_eq_full.csv"
    mf_csv = PORTFOLIO_DIR / "tradebook_mf_full.csv"
    if not (eq_csv.exists() and mf_csv.exists()):
        return {
            "status": "needs-cas",
            "note": "Download tradebook CSVs from Console into ~/.hermes/data/portfolio/ (tradebook_eq_full.csv, tradebook_mf_full.csv), then rerun.",
        }

    today = _date.fromisoformat(as_of[:10])

    def xirr(flows, guess=0.05):
        flows = [(_date.fromisoformat(d), a) for d, a in flows if abs(a) > 1e-9]
        if not flows:
            return None
        flows.sort()
        start = flows[0][0]
        days = [(d - start).days for d, _ in flows]
        amts = [a for _, a in flows]
        scale = max(abs(a) for a in amts) or 1.0
        amts = [a / scale for a in amts]

        def npv(r):
            return sum(a / (1 + r) ** (dy / 365.0) for dy, a in zip(days, amts))

        def dnpv(r):
            return sum(-dy / 365.0 * a / (1 + r) ** (dy / 365.0 + 1) for dy, a in zip(days, amts))

        best = None
        for g in [0.02, 0.05, 0.10, 0.15, 0.25, 0.35]:
            r = g
            for _ in range(200):
                f, df = npv(r), dnpv(r)
                if abs(df) < 1e-12:
                    break
                nr = r - f / df
                if abs(nr - r) < 1e-12:
                    r = nr
                    break
                if nr < -0.999 or nr > 10:
                    break
                r = nr
            if -0.90 < r < 2.5 and best is None:
                best = r
        return best

    def load(path):
        with open(path) as f:
            return list(_csv.DictReader(f))

    eq_trades = load(eq_csv)
    mf_trades = load(mf_csv)

    SYM_OVERRIDES = {"MTARTECH": "MTARTECH-BE", "TMPV": "TMCV"}
    held = {e["symbol"] for e in equity}

    def norm_sym(s):
        s = SYM_OVERRIDES.get(s, s)
        return s if s in held else s

    # Equity flows: all realized + terminal value of holdings that have buy history
    realized = []
    for t in eq_trades:
        price, qty = float(t["price"]), float(t["quantity"])
        amt = (-qty * price) if t["trade_type"].lower() == "buy" else (qty * price)
        realized.append((t["trade_date"], amt))

    buys = {}
    for t in eq_trades:
        if t["trade_type"].lower() == "buy":
            s = norm_sym(t["symbol"])
            buys.setdefault(s, []).append(_date.fromisoformat(t["trade_date"]))

    held_mv = {e["symbol"]: float(e.get("market_value", 0)) for e in equity}
    no_ledger = [s for s in held if s not in buys]
    terminal = [
        (today.isoformat(), mv) for s, mv in held_mv.items() if s in buys
    ]
    eq_xirr = xirr(realized + terminal)

    # MF flows: realized + terminal value of all current funds
    mf_realized = []
    for t in mf_trades:
        price, qty = float(t["price"]), float(t["quantity"])
        amt = (-qty * price) if t["trade_type"].lower() == "buy" else (qty * price)
        mf_realized.append((t["trade_date"], amt))
    mf_mv = sum(float(m.get("market_value", 0)) for m in mf)
    mf_xirr = xirr(mf_realized + [(today.isoformat(), mf_mv)])

    # Behavioral
    ages = [((today - min(v)).days) / 365.0 for v in buys.values() if v]
    sells = {}
    for t in eq_trades:
        if t["trade_type"].lower() == "sell":
            s = norm_sym(t["symbol"])
            sells.setdefault(s, []).append(_date.fromisoformat(t["trade_date"]))
    quick_sells = 0
    closed_loops = 0
    for s, sd in sells.items():
        if s in buys and buys[s]:
            fb, fs = min(buys[s]), min(sd)
            if (fs - fb).days <= 90:
                quick_sells += 1
            if fb <= fs:
                closed_loops += 1
    round_trips = sum(1 for s, sd in sells.items() if s in held and sd)
    # denominators, so the counts can be read as rates rather than as alarms
    symbols_with_both = sum(1 for s in sells if s in buys and buys[s])
    all_dates = [t["trade_date"] for t in eq_trades] + [t["trade_date"] for t in mf_trades]

    gross_buy = sum(-a for _, a in realized if a < 0)
    gross_sell = sum(a for _, a in realized if a > 0)

    return {
        "status": "ready",
        "period": {
            "since": min(all_dates) if all_dates else None,
            "until": max(all_dates) if all_dates else None,
            "years": round((today - _date.fromisoformat(min(all_dates))).days / 365.0, 1)
            if all_dates else None,
        },
        "equity_xirr_pct": round(eq_xirr * 100, 2) if eq_xirr else None,
        "mf_xirr_pct": round(mf_xirr * 100, 2) if mf_xirr else None,
        "benchmark": {
            "status": "needs-benchmark",
            "note": ("No benchmark series is wired up, so the XIRRs above cannot be "
                     "called above or below market. Add dated benchmark closes to "
                     "compare on the same window."),
        },
        "behavior": {
            "avg_holding_years": round(sum(ages) / len(ages), 2) if ages else None,
            "held_with_sell_history": round_trips,
            "n_held_symbols": len(held),
            "sells_within_90d_of_first_buy": quick_sells,
            "symbols_ever_bought_and_sold": symbols_with_both,
            "quick_sell_rate_pct": round(quick_sells / symbols_with_both * 100, 1)
            if symbols_with_both else None,
            "gross_bought": round(gross_buy, 0),
            "gross_sold": round(gross_sell, 0),
            "turnover_ratio": round(gross_sell / gross_buy, 2) if gross_buy else None,
        },
        "notes": [
            "SGBJUN31I (INR 3.85L) excluded from XIRR: its buys predate the 2020-2026 tradebook window.",
            "Return on cost (21.4%) and XIRR (~13%) measure different things: the first ignores "
            "the timing and size of each contribution, the second weights them. XIRR is the "
            "comparable number.",
        ] + ([f"No equity trade rows for: {', '.join(no_ledger)}"] if no_ledger else []),
    }


def build_goals(policy, summary):
    """Goal funding from policy.goals: dated required-corpus targets mapped to sleeves."""
    goals = (policy or {}).get("goals") or []
    sleeves = summary.get("sleeves") or {}
    if not goals:
        return {"status": "needs-baseline",
                "note": "Goal funding needs a dated required-corpus baseline per goal (retirement, son's education, home loan). Add goals in the Policy & Goals editor."}
    rows = []
    for g in goals:
        linked = [str(s) for s in (g.get("sleeves") or [])]
        corpus = sum(
            float(s.get("market") or 0)
            for name, s in sleeves.items()
            if name in linked or not linked
        )
        rows.append({
            "name": g.get("name"),
            "target_inr": g.get("target_inr"),
            "target_date": g.get("target_date"),
            "sleeves": linked,
            "current_corpus_inr": corpus,
            "funded_pct": round(corpus / g["target_inr"] * 100, 1) if g.get("target_inr") else None,
        })
    return {"status": "ready", "goals": rows,
            "note": "Funding progress = current market value of linked sleeves vs target corpus. Not a forecast."}


def main():
    equity = load_equity()
    mf = load_mf()
    us = load_vested()

    as_of_raw = None
    try:
        as_of_raw = json.loads((PORTFOLIO_DIR / "zerodha-holdings-live.json").read_text()).get("fetched_at")
    except Exception:
        pass
    as_of = as_of_raw or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat()

    summary = build_summary(equity, mf, as_of, us)
    performance = build_performance(equity, mf, as_of)

    policy = None
    if POLICY_PATH.exists():
        policy = json.loads(POLICY_PATH.read_text())
    discipline = build_discipline(policy, equity, mf)

    perf_ready = performance.get("status") == "ready"
    data = {
        "meta": {
            "title": "Portfolio MRI",
            "generated_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
            "as_of": as_of,
            "version": "v1",
        },
        "summary": summary,
        "findings": build_findings(summary, performance, us),
        "us_equity": us,
        "performance": performance,
        "discipline": discipline,
        "goals": build_goals(policy, summary),
        "data_quality": {
            "xirr": "ready" if perf_ready else "needs-cas",
            "behavior_ledger": "ready" if perf_ready else "needs-trade-history",
            "benchmark_relative": "needs-trade-dates",
            "us_sleeve": us.get("status", "needs-snapshot"),
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=1))
    LOCAL_OUT.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_OUT.write_text(json.dumps(data, indent=1))
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")
    print(f"total: {data['summary']['total']['market_value']:,.0f} INR, "
          f"pnl {data['summary']['total']['pnl']:+,.0f}")
    print("discipline:", discipline.get("status"))
    print("us sleeve:", us.get("status"),
          f"${us.get('value_usd'):,.2f}" if us.get("status") == "ready" else us.get("note"))
    return 0


if __name__ == "__main__":
    sys.exit(main())