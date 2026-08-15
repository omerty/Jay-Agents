"""Keira qualification eval harness — regression metrics for M&A gates.

Run:  python -m src.keira_eval
CI:   asserts minimum precision floors (heuristic, no LLM required).
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import yaml

from .keira_intelligence import build_company_intelligence, qualify_keira_intelligence

logger = logging.getLogger(__name__)
AGENTS_DIR = Path(__file__).parent.parent / "agents"

LABEL_KEYS = (
    "geo_correct",
    "owner_correct",
    "private_company",
    "size_correct",
    "succession_signal",
    "overall_fit",
)

# Floor metrics for CI (heuristic mode). Tune only when intentionally changing gates.
DEFAULT_FLOORS = {
    "geo_precision": 0.85,
    "owner_precision": 0.80,
    "private_company_precision": 0.85,
    "size_precision": 0.80,
    "signal_precision": 0.70,
    "overall_qualification": 0.80,
    "max_false_positive_rate": 0.20,
}


def load_eval_set(agent: str = "keira") -> list[dict]:
    path = AGENTS_DIR / agent / "eval_set.yaml"
    if not path.exists():
        return []
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("cases") or [])


def expand_synthetic_cases() -> list[dict]:
    """Programmatic variants for volume regression (~200 cases)."""
    cities_pos = [
        "Ottawa", "Kingston", "Cornwall", "Kanata", "Belleville",
        "Pembroke", "Brockville", "Arnprior", "Nepean", "Orleans",
    ]
    cities_neg = [
        "Toronto", "Mississauga", "Brampton", "Markham", "Vaughan",
        "Hamilton", "Kitchener", "London", "Calgary", "Vancouver",
    ]
    good_titles = [
        "Founder & President", "Owner", "CEO & Founder", "President", "Owner / Operator",
    ]
    bad_titles = [
        "VP Sales", "Regional President", "Director of Marketing", "Country Manager", "Broker",
    ]
    cases: list[dict] = []

    for i, city in enumerate(cities_pos):
        for j, title in enumerate(good_titles):
            cases.append({
                "id": f"syn_pos_{city.lower()}_{j}",
                "expect": "qualify",
                "synthetic": True,
                "labels": {
                    "geo_correct": True,
                    "owner_correct": True,
                    "private_company": True,
                    "size_correct": True,
                    "succession_signal": True,
                    "overall_fit": True,
                },
                "lead": {
                    "company": f"{city} Industrial Works {i}{j} Ltd",
                    "contact_name": f"Owner {i}{j}",
                    "contact_title": title,
                    "company_city": city,
                    "employee_count": 40 + (i * 7 + j * 3) % 200,
                    "signal": (
                        "Founded in 1988. Succession planning underway. "
                        "Next chapter and transitioning leadership."
                    ),
                },
            })

    for i, city in enumerate(cities_neg):
        cases.append({
            "id": f"syn_neg_geo_{city.lower()}",
            "expect": "reject",
            "synthetic": True,
            "labels": {
                "geo_correct": False,
                "owner_correct": True,
                "private_company": True,
                "size_correct": True,
                "succession_signal": True,
                "overall_fit": False,
            },
            "lead": {
                "company": f"{city} Holdings Co {i}",
                "contact_name": f"Founder {i}",
                "contact_title": "Founder & President",
                "company_city": city,
                "employee_count": 80,
                "signal": "Succession planning underway after 40 years.",
            },
        })

    for i, title in enumerate(bad_titles):
        for city in cities_pos[:4]:
            cases.append({
                "id": f"syn_neg_owner_{i}_{city.lower()}",
                "expect": "reject",
                "synthetic": True,
                "labels": {
                    "geo_correct": True,
                    "owner_correct": False,
                    "private_company": True,
                    "size_correct": True,
                    "succession_signal": True,
                    "overall_fit": False,
                },
                "lead": {
                    "company": f"{city} Bad Title Co {i}",
                    "contact_name": f"Exec {i}",
                    "contact_title": title,
                    "company_city": city,
                    "employee_count": 90,
                    "signal": "Succession planning underway. Founded in 1985.",
                },
            })

    # Ownership type negatives
    for i, (blob, otype) in enumerate((
        ("Portfolio company of Apex Private Equity", "pe"),
        ("Publicly traded manufacturer NYSE: ABCD", "public"),
        ("A wholly owned subsidiary of Global Parent Inc", "sub"),
        ("Non-profit charity serving the community", "nonprofit"),
    )):
        cases.append({
            "id": f"syn_neg_ownertype_{otype}",
            "expect": "reject",
            "synthetic": True,
            "labels": {
                "geo_correct": True,
                "owner_correct": True,
                "private_company": False,
                "size_correct": True,
                "succession_signal": False,
                "overall_fit": False,
            },
            "lead": {
                "company": f"Ottawa TypeFail {i} Inc",
                "contact_name": f"CEO {i}",
                "contact_title": "CEO",
                "company_city": "Ottawa",
                "employee_count": 100,
                "signal": blob,
            },
        })

    # Size extremes
    for emp in (5, 8, 12, 14, 2500, 6000):
        cases.append({
            "id": f"syn_neg_size_{emp}",
            "expect": "reject",
            "synthetic": True,
            "labels": {
                "geo_correct": True,
                "owner_correct": True,
                "private_company": True,
                "size_correct": False,
                "succession_signal": True,
                "overall_fit": False,
            },
            "lead": {
                "company": f"Ottawa SizeFail {emp}",
                "contact_name": "Size Owner",
                "contact_title": "Founder & President",
                "company_city": "Ottawa",
                "employee_count": emp,
                "signal": "Succession planning underway. Founded in 1980.",
            },
        })

    # Family-only → research_required
    for i, city in enumerate(cities_pos):
        cases.append({
            "id": f"syn_research_family_{city.lower()}",
            "expect": "research_required",
            "synthetic": True,
            "labels": {
                "geo_correct": True,
                "owner_correct": True,
                "private_company": True,
                "size_correct": True,
                "succession_signal": False,
                "overall_fit": False,
            },
            "lead": {
                "company": f"{city} Family Co {i}",
                "contact_name": f"Family Owner {i}",
                "contact_title": "Owner",
                "company_city": city,
                "employee_count": 50,
                "signal": "Family-owned privately held business.",
            },
        })

    return cases


def _pred_labels(intel: dict, q: dict) -> dict:
    geo = intel.get("geo") or {}
    own = intel.get("ownership") or {}
    size = intel.get("size") or {}
    succ = intel.get("succession") or {}
    return {
        "geo_correct": bool(geo.get("pass")) and not geo.get("reject"),
        "owner_correct": (
            not own.get("reject")
            and float(own.get("ownership_confidence") or 0) >= 0.70
            and not own.get("needs_owner_resolution")
        ),
        "private_company": not own.get("reject") and (own.get("ownership_type") or "") not in (
            "public", "subsidiary", "private_equity", "nonprofit", "government", "franchisor",
        ),
        "size_correct": bool(size.get("pass", True)),
        "succession_signal": int(succ.get("max_tier") or 0) >= 2,
        "overall_fit": bool(q.get("outreach_ok")),
    }


def evaluate_case(case: dict, *, config: dict | None = None) -> dict:
    lead = case.get("lead") or {}
    intel = build_company_intelligence(lead)
    q = qualify_keira_intelligence(intel, config=config)
    status = q.get("status") or ("reject" if q.get("rejected") else "research_required")
    if q.get("outreach_ok"):
        status = "qualify"
    elif q.get("rejected"):
        status = "reject"
    else:
        status = "research_required"

    pred = _pred_labels(intel, q)
    gold = case.get("labels") or {}
    label_hits = {}
    for k in LABEL_KEYS:
        if k not in gold:
            continue
        label_hits[k] = bool(pred[k]) == bool(gold[k])

    expect = case.get("expect")
    status_ok = status == expect
    # Soft: research_required gold can accept reject if overall_fit false? Keep strict.
    return {
        "id": case.get("id"),
        "expect": expect,
        "got_status": status,
        "status_ok": status_ok,
        "pred": pred,
        "gold": gold,
        "label_hits": label_hits,
        "score": q.get("score"),
        "rejected": q.get("rejected"),
        "outreach_ok": q.get("outreach_ok"),
        "synthetic": bool(case.get("synthetic")),
    }


def run_eval(
    *,
    include_synthetic: bool = True,
    holdout_only: bool = False,
    config: dict | None = None,
) -> dict:
    cases = load_eval_set("keira")
    if include_synthetic and not holdout_only:
        cases = cases + expand_synthetic_cases()

    results = [evaluate_case(c, config=config) for c in cases]
    holdout = [r for r in results if not r.get("synthetic")]
    all_rows = results

    def _precision(rows: list[dict], key: str) -> float | None:
        # Among gold-positive for key, how often pred matches (or classic precision on pred+)
        # Use label agreement rate where gold defines the truth for that dimension.
        relevant = [r for r in rows if key in (r.get("gold") or {})]
        if not relevant:
            return None
        hits = sum(1 for r in relevant if r["label_hits"].get(key))
        return round(hits / len(relevant), 3)

    def _fp_rate(rows: list[dict]) -> float:
        # Predicted qualify when overall_fit gold is false
        negs = [r for r in rows if (r.get("gold") or {}).get("overall_fit") is False]
        if not negs:
            return 0.0
        fps = sum(1 for r in negs if r.get("outreach_ok"))
        return round(fps / len(negs), 3)

    def _overall(rows: list[dict]) -> float:
        if not rows:
            return 0.0
        return round(sum(1 for r in rows if r["status_ok"]) / len(rows), 3)

    metrics = {
        "n_total": len(all_rows),
        "n_holdout": len(holdout),
        "n_synthetic": len(all_rows) - len(holdout),
        "geo_precision": _precision(holdout or all_rows, "geo_correct"),
        "owner_precision": _precision(holdout or all_rows, "owner_correct"),
        "private_company_precision": _precision(holdout or all_rows, "private_company"),
        "size_precision": _precision(holdout or all_rows, "size_correct"),
        "signal_precision": _precision(holdout or all_rows, "succession_signal"),
        "overall_qualification": _overall(holdout or all_rows),
        "overall_qualification_all": _overall(all_rows),
        "false_positive_rate": _fp_rate(holdout or all_rows),
        "false_positive_rate_all": _fp_rate(all_rows),
    }

    floors = dict(DEFAULT_FLOORS)
    failures = []
    check_map = {
        "geo_precision": metrics["geo_precision"],
        "owner_precision": metrics["owner_precision"],
        "private_company_precision": metrics["private_company_precision"],
        "size_precision": metrics["size_precision"],
        "signal_precision": metrics["signal_precision"],
        "overall_qualification": metrics["overall_qualification"],
    }
    for name, val in check_map.items():
        floor = floors[name]
        if val is not None and val < floor:
            failures.append(f"{name}={val} < floor {floor}")
    if metrics["false_positive_rate"] > floors["max_false_positive_rate"]:
        failures.append(
            f"false_positive_rate={metrics['false_positive_rate']} > "
            f"{floors['max_false_positive_rate']}"
        )

    return {
        "metrics": metrics,
        "floors": floors,
        "pass": not failures,
        "failures": failures,
        "results": results,
        "failed_ids": [r["id"] for r in holdout if not r["status_ok"]],
    }


def format_report(summary: dict) -> str:
    m = summary["metrics"]
    lines = [
        "KEIRA EVALUATION",
        "──────────────────────────────",
        f"Holdout cases             {m['n_holdout']}",
        f"Synthetic cases           {m['n_synthetic']}",
        "",
        f"Geo precision             {100 * (m['geo_precision'] or 0):.0f}%",
        f"Owner precision           {100 * (m['owner_precision'] or 0):.0f}%",
        f"Private-company precision {100 * (m['private_company_precision'] or 0):.0f}%",
        f"Size precision            {100 * (m['size_precision'] or 0):.0f}%",
        f"Signal precision          {100 * (m['signal_precision'] or 0):.0f}%",
        "",
        f"Overall qualification     {100 * (m['overall_qualification'] or 0):.0f}%",
        f"False-positive rate       {100 * (m['false_positive_rate'] or 0):.0f}%",
        "",
        f"Status: {'PASS' if summary['pass'] else 'FAIL'}",
    ]
    if summary.get("failures"):
        lines.append("Failures:")
        for f in summary["failures"]:
            lines.append(f"  - {f}")
    if summary.get("failed_ids"):
        lines.append(f"Holdout misses: {', '.join(summary['failed_ids'][:12])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keira M&A qualification eval")
    parser.add_argument("--holdout-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="exit 1 on floor miss")
    args = parser.parse_args(argv)
    summary = run_eval(holdout_only=args.holdout_only, include_synthetic=not args.holdout_only)
    if args.json:
        print(json.dumps({
            "metrics": summary["metrics"],
            "pass": summary["pass"],
            "failures": summary["failures"],
            "failed_ids": summary["failed_ids"],
        }, indent=2))
    else:
        print(format_report(summary))
    if args.strict and not summary["pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
