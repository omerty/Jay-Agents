"""Outreach eval harness — rubric scoring + pairwise LLM-as-judge.

Measure before changing prompts. Pairwise comparison is more reliable than
absolute scores (see G-Eval / "Who Validates the Validators?").
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent.parent / "agents"

RUBRIC_DIMS = (
    "specificity",          # concrete account fact, not flattery
    "evidence_grounding",   # traces to signal/brief/proof
    "no_unsupported_claims",
    "natural_voice",        # not machine-fluent filler
    "clear_ask",            # single question CTA
)

BANNED_PHRASES = (
    "hope this finds you well",
    "i came across",
    "revolutionize",
    "cutting-edge",
    "seamlessly",
    "just following up",
    "circling back to touch base",
)


def load_eval_set(agent: str = "woodway") -> list[dict]:
    path = AGENTS_DIR / agent / "eval_set.yaml"
    if not path.exists():
        return []
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("drafts") or [])


def deterministic_checks(body: str, *, subject: str | None = None) -> dict:
    """Cheap non-LLM gates — fail fast without burning tokens."""
    text = (body or "").strip()
    lower = text.lower()
    words = len(re.findall(r"\b\w+\b", text))
    issues = []
    if words > 140:
        issues.append(f"too long ({words} words; target ≤120)")
    if words < 25:
        issues.append(f"too short ({words} words)")
    for phrase in BANNED_PHRASES:
        if phrase in lower:
            issues.append(f"banned phrase: {phrase}")
    if "?" not in text:
        issues.append("no question CTA")
    if subject and len(subject.split()) > 8:
        issues.append(f"subject too long ({len(subject.split())} words)")
    # Unsupported ROI-style numbers (proof_library forbids these)
    if re.search(r"\b\d{1,3}%\b", lower):
        issues.append("numeric percentage claim (forbidden without proof_library stats)")
    if re.search(r"\b(fortune 500|regulatory approval)\b", lower):
        issues.append("unsupported authority/approval claim")
    # Machine-tell: stacked buzzwords
    buzz = sum(1 for w in ("leverage", "synergy", "unlock", "empower", "robust") if w in lower)
    if buzz >= 2:
        issues.append("buzzword cluster")
    return {
        "ok": not issues,
        "issues": issues,
        "word_count": words,
    }


def score_draft_rubric(
    body: str,
    *,
    subject: str | None = None,
    context: str | None = None,
    use_llm: bool = True,
) -> dict:
    """
    Score a draft on the 5-dim rubric.
    Returns {scores: {dim: 1-5}, mean, issues, mode}.
    """
    det = deterministic_checks(body, subject=subject)
    if not use_llm or os.getenv("OUTREACH_EVAL_LLM", "true").lower() in ("0", "false", "no"):
        # Heuristic stand-in when LLM off
        base = 4 if det["ok"] else 2
        scores = {d: base for d in RUBRIC_DIMS}
        if det["ok"] and context and any(
            tok.lower() in body.lower()
            for tok in re.findall(r"[A-Za-z]{5,}", context or "")[:8]
        ):
            scores["specificity"] = 5
            scores["evidence_grounding"] = 4
        return {
            "scores": scores,
            "mean": round(sum(scores.values()) / len(scores), 2),
            "issues": det["issues"],
            "mode": "heuristic",
            "deterministic": det,
        }

    from .llm import chat_json
    from .llm_optimize import task_defaults

    defaults = task_defaults("critique")
    prompt = f"""Score this B2B cold email on each dimension 1-5 (integer).
Be harsh on generic/fluent-but-empty copy. Reward short specific emails that cite a real account fact.

SUBJECT: {subject or '(none)'}
ACCOUNT CONTEXT (may be empty):
{(context or 'none')[:1200]}

DRAFT:
{body[:2000]}

Dimensions:
- specificity: concrete account fact in first 1-2 sentences
- evidence_grounding: claim clearly supported by context or known product proof
- no_unsupported_claims: no invented customers, ROI, or regulatory approvals
- natural_voice: reads human, not LLM filler
- clear_ask: exactly one soft question CTA

JSON only:
{{"scores": {{"specificity": n, "evidence_grounding": n, "no_unsupported_claims": n, "natural_voice": n, "clear_ask": n}}, "issues": ["..."]}}"""

    try:
        result = chat_json(
            prompt,
            system="You evaluate privacy/governance B2B outreach. Strict. JSON only.",
            max_tokens=defaults["max_tokens"],
            temperature=0.1,
            task="critique",
        )
        raw = result.get("scores") or {}
        scores = {}
        for dim in RUBRIC_DIMS:
            try:
                scores[dim] = max(1, min(5, int(raw.get(dim, 3))))
            except (TypeError, ValueError):
                scores[dim] = 3
        issues = list(result.get("issues") or []) + det["issues"]
        return {
            "scores": scores,
            "mean": round(sum(scores.values()) / len(scores), 2),
            "issues": issues,
            "mode": "llm",
            "deterministic": det,
        }
    except Exception as e:
        logger.warning("Rubric LLM failed: %s", e)
        return score_draft_rubric(body, subject=subject, context=context, use_llm=False)


def pairwise_prefer(
    draft_a: str,
    draft_b: str,
    *,
    context: str | None = None,
    use_llm: bool = True,
) -> dict:
    """
    Which draft is better for reply likelihood?
    Returns {winner: "A"|"B"|"tie", reason, mode}.
    """
    if not use_llm:
        sa = deterministic_checks(draft_a)
        sb = deterministic_checks(draft_b)
        if sa["ok"] and not sb["ok"]:
            return {"winner": "A", "reason": "B fails deterministic gates", "mode": "heuristic"}
        if sb["ok"] and not sa["ok"]:
            return {"winner": "B", "reason": "A fails deterministic gates", "mode": "heuristic"}
        # Prefer shorter specific
        wa, wb = sa["word_count"], sb["word_count"]
        if wa < wb - 20:
            return {"winner": "A", "reason": "shorter", "mode": "heuristic"}
        if wb < wa - 20:
            return {"winner": "B", "reason": "shorter", "mode": "heuristic"}
        return {"winner": "tie", "reason": "indistinguishable without LLM", "mode": "heuristic"}

    from .llm import chat_json

    try:
        result = chat_json(
            f"""Compare two cold emails for the SAME account. Pick which is more likely to get a reply from a privacy/governance buyer.
Prefer: specific account evidence, short, one clear ask, no hype.
Penalize: generic fluency, unsupported claims, long windups.

CONTEXT:
{(context or 'none')[:1000]}

DRAFT A:
{draft_a[:1500]}

DRAFT B:
{draft_b[:1500]}

JSON: {{"winner": "A"|"B"|"tie", "reason": "one sentence"}}""",
            system="You compare B2B outreach drafts. JSON only.",
            max_tokens=200,
            temperature=0.1,
            task="critique",
        )
        w = (result.get("winner") or "tie").upper()
        if w not in ("A", "B", "TIE"):
            w = "TIE"
        return {"winner": w if w != "TIE" else "tie", "reason": result.get("reason") or "", "mode": "llm"}
    except Exception as e:
        logger.warning("Pairwise LLM failed: %s", e)
        return pairwise_prefer(draft_a, draft_b, context=context, use_llm=False)


def run_eval_set(agent: str = "woodway", *, use_llm: bool = False) -> dict:
    """Score all holdout drafts; return summary + per-item results."""
    drafts = load_eval_set(agent)
    results = []
    for item in drafts:
        body = item.get("body") or ""
        subject = item.get("subject")
        scored = score_draft_rubric(
            body,
            subject=subject,
            context=item.get("context"),
            use_llm=use_llm,
        )
        expected = item.get("expect")  # "good" | "bad"
        mean = scored["mean"]
        pass_ok = (expected != "bad" and mean >= 3.5) or (expected == "bad" and mean < 3.5)
        if expected == "bad" and mean < 3.5:
            pass_ok = True
        elif expected == "good" and mean >= 3.5:
            pass_ok = True
        elif expected in ("good", "bad"):
            pass_ok = False
        else:
            pass_ok = scored["deterministic"]["ok"]
        results.append({
            "id": item.get("id"),
            "expect": expected,
            "mean": mean,
            "pass": pass_ok,
            "scores": scored["scores"],
            "issues": scored["issues"],
            "mode": scored["mode"],
        })

    passed = sum(1 for r in results if r["pass"])
    return {
        "agent": agent,
        "n": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 3) if results else None,
        "results": results,
    }


def main():
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Woodway outreach eval harness")
    parser.add_argument("--agent", default="woodway")
    parser.add_argument("--llm", action="store_true", help="Use LLM judge (costs tokens)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    summary = run_eval_set(args.agent, use_llm=args.llm)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{summary['agent']}: {summary['passed']}/{summary['n']} passed "
              f"(rate={summary['pass_rate']})")
        for r in summary["results"]:
            flag = "OK" if r["pass"] else "FAIL"
            print(f"  [{flag}] {r['id']} mean={r['mean']} expect={r['expect']} issues={r['issues'][:2]}")


if __name__ == "__main__":
    main()
