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

GOLD_SUFFIXES = ("GOLDBEES", "SILVERBEES", "SGB", "GOLDMONTHLY")
EQUITY_EXCHANGES = {"NSE", "BSE"}


def latest_mf_snapshot() -> pathlib.Path:
    hits = sorted(PORTFOLIO_DIR.glob("mf-holdings-*.json"))
    if not hits:
        raise FileNotFoundError("no mf-holdings snapshot found")
    return hits[-1]


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


def build_summary(equity, mf, as_of):
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

    all_rows = eq_rows + mf_rows
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

    # contribution matrix: top +/- by INR pnl
    sorted_rows = sorted(all_rows, key=lambda r: -r["unrealized_pnl"])
    positive = [(r, r["unrealized_pnl"]) for r in sorted_rows if r["unrealized_pnl"] > 0]
    negative = [(r, r["unrealized_pnl"]) for r in sorted_rows if r["unrealized_pnl"] < 0]
    contribution = {
        "top_gainers": [
            {
                "name": r["symbol"] if "symbol" in r else r["fund"],
                "sleeve": r["sleeve"],
                "pnl": round(v, 0),
                "return_pct": r["return_pct"],
                "weight_pct": round(r["market_value"] / total_mv * 100, 1) if total_mv else 0.0,
            }
            for r, v in positive[:8]
        ],
        "top_losers": [
            {
                "name": r["symbol"] if "symbol" in r else r["fund"],
                "sleeve": r["sleeve"],
                "pnl": round(v, 0),
                "return_pct": r["return_pct"],
                "weight_pct": round(r["market_value"] / total_mv * 100, 1) if total_mv else 0.0,
            }
            for r, v in negative[:6]
        ],
        "winners_pnl": round(sum(v for _, v in positive), 0),
        "losers_pnl": round(sum(v for _, v in negative), 0),
        "win_rate_pct": round(len(positive) / len(all_rows) * 100, 1) if all_rows else 0,
        "n_holdings": len(all_rows),
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
    shock_rows = [r for r in all_rows if r["sleeve"] not in ("gold",)]
    shock_pnl = sum(shocked(r, shock_drop) for r in shock_rows if r["sleeve"] != "gold")
    post_shock_pnl = total_pnl + sum(
        (r["market_value"] * (1 - shock_drop / 100.0)) - r["invested_value"] - r["unrealized_pnl"]
        for r in all_rows
        if r["sleeve"] != "gold"
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


def main():
    equity = load_equity()
    mf = load_mf()

    as_of_raw = None
    try:
        as_of_raw = json.loads((PORTFOLIO_DIR / "zerodha-holdings-live.json").read_text()).get("fetched_at")
    except Exception:
        pass
    as_of = as_of_raw or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat()

    summary = build_summary(equity, mf, as_of)

    policy = None
    if POLICY_PATH.exists():
        policy = json.loads(POLICY_PATH.read_text())
    discipline = build_discipline(policy, equity, mf)

    data = {
        "meta": {
            "title": "Portfolio MRI",
            "generated_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
            "as_of": as_of,
            "version": "v1",
        },
        "summary": summary,
        "discipline": discipline,
        "goals": {"status": "needs-baseline",
                  "note": "Goal funding needs a dated required-corpus baseline per sleeve (retirement, son's education, home loan). Add to policy.json."},
        "data_quality": {
            "xirr": "needs-cas",
            "behavior_ledger": "needs-trade-history",
            "benchmark_relative": "needs-trade-dates",
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
    return 0


if __name__ == "__main__":
    sys.exit(main())