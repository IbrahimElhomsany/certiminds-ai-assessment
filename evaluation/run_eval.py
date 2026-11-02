"""Evaluation framework (C4) extended into the capstone suite (E.16).

Usage:
    python -m evaluation.run_eval                 # offline workflow mode (free, deterministic)
    python -m evaluation.run_eval --mode claude   # real Claude agent (costs API credit)
    python -m evaluation.run_eval --limit 5       # quick smoke run

Test-case format (evaluation/cases.jsonl, one JSON object per line):
    {"id": "...", "category": "...", "turns": ["customer message", ...], "expect": {...checks...}}
Each check in "expect" is optional; the scorer only applies the checks a case defines,
plus automatic checks (rule compliance, groundedness, hallucinated SKUs) on every case.
Output: evaluation/results/eval_<mode>_<time>.json (+ .md report) with per-case details.
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = ROOT / "evaluation" / "cases.jsonl"
RESULTS = ROOT / "evaluation" / "results"
REFUSAL_WORDS = ["can't", "cannot", "unable", "not able", "won't", "confidential", "لا يمكن", "عذر"]
METRICS = ["requirement_extraction", "product_correctness", "rule_compliance", "groundedness",
           "calculation_correctness", "inventory_correctness", "hallucination", "security", "usefulness"]


def build_agent(mode: str, model: str | None):
    os.environ.setdefault("RAG_DENSE", "0")
    if mode == "workflow":
        os.environ["LLM_PROVIDER"] = "mock"
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    if model:
        os.environ["LLM_MODEL"] = model
    from backend.agent import SalesAgent
    from backend.gateway import LLMGateway
    from backend.providers import AnthropicProvider, MockProvider
    if mode == "claude":
        import anthropic
        gw = LLMGateway([AnthropicProvider(), MockProvider()])
        return SalesAgent(gw, client=anthropic.Anthropic(max_retries=2, timeout=60))
    return SalesAgent(LLMGateway([MockProvider()]), client=None)


def matches(actual, expected) -> bool:
    if actual is None:
        return False
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(actual) - expected) <= max(0.01, abs(expected) * 0.01)
        except (TypeError, ValueError):
            return False
    return str(expected).lower() in str(actual).lower()


def score_case(case: dict, result: dict, latency_ms: float) -> dict:
    from backend.catalog import CATALOG
    from backend.guardrails import CANARY, check_output, numbers_in
    from backend.tools import compute_quantity

    exp, reply = case["expect"], result["reply"]
    low = reply.lower()
    recs = [r["sku"] for r in result.get("recommendations") or []]
    req = result.get("requirements") or {}
    calls = result.get("tool_calls") or []
    checks: dict[str, list[tuple[str, bool]]] = {m: [] for m in METRICS}

    # --- requirement extraction (field-level)
    for field, value in (exp.get("requirements") or {}).items():
        checks["requirement_extraction"].append((f"req.{field}={value}", matches(req.get(field), value)))
    if "intent" in exp:
        checks["requirement_extraction"].append(("intent", result["analysis"]["intent"] == exp["intent"]))

    # --- product correctness
    if exp.get("valid_skus"):
        checks["product_correctness"].append(("only valid SKUs", bool(recs) and set(recs) <= set(exp["valid_skus"])))
    if exp.get("must_recommend_any"):
        checks["product_correctness"].append(("expected SKU present", bool(set(recs) & set(exp["must_recommend_any"]))))
    if exp.get("must_not_recommend"):
        checks["product_correctness"].append(("no forbidden SKU", not set(recs) & set(exp["must_not_recommend"])))

    # --- rule compliance (automatic on every case with recommendations)
    if recs:
        checks["rule_compliance"].append(("BR-09 max 3 products", len(recs) <= 3))
        for sku in recs:
            p = CATALOG[sku]
            if req.get("floor_or_wall") == "floor":
                checks["rule_compliance"].append((f"BR-02 {sku} floor-capable", "floor" in p.surfaces))
            if req.get("indoor_outdoor") == "outdoor" and req.get("floor_or_wall") != "wall":
                checks["rule_compliance"].append((f"BR-01 {sku} >= R11", p.slip_value >= 11))
    if result.get("quote"):
        q = result["quote"]
        checks["rule_compliance"].append(("BR-07 approval flag", q["approval_required"] == (q["discount_percent"] > 12)))
    if "quote_approval_required" in exp:
        q = result.get("quote")
        checks["rule_compliance"].append(("quote created with expected approval flag",
                                          bool(q) and q["approval_required"] == exp["quote_approval_required"]))

    # --- groundedness: every $ amount in the final reply exists in tool outputs / catalog / customer input
    allowed = numbers_in(" ".join(case["turns"])) | numbers_in(json.dumps(calls, default=str)) | \
        numbers_in(json.dumps(result.get("recommendations"), default=str)) | numbers_in(json.dumps(req)) | \
        {p.price_per_m2 for p in CATALOG.values()}
    ungrounded = [v for v in check_output(reply, allowed) if v.startswith("ungrounded")]
    checks["groundedness"].append(("all amounts grounded", not ungrounded))

    # --- calculation correctness (independent recomputation)
    if exp.get("calc"):
        c = exp["calc"]
        p = CATALOG[c["sku"]]
        truth = compute_quantity(c["area_m2"], p.tile_width_cm, p.tile_length_cm, p.tiles_per_box, 10, p.price_per_m2)
        found = [x["output"] for x in calls if x["tool"] == "calculate_tile_quantity"
                 and isinstance(x["output"], dict) and x["output"].get("sku") == c["sku"]]
        checks["calculation_correctness"].append(("calculator tool used", bool(found)))
        if found:
            checks["calculation_correctness"].append(("boxes correct", found[-1]["box_count"] == c["boxes"] == truth.box_count))
            checks["calculation_correctness"].append(("subtotal correct",
                                                      found[-1]["material_subtotal"] == c["subtotal"] == truth.material_subtotal))

    # --- inventory correctness
    for sku, status in (exp.get("inventory") or {}).items():
        seen = [x["output"].get("status") for x in calls if x["tool"] == "check_inventory"
                and isinstance(x["output"], dict) and x["output"].get("sku") == sku]
        seen += [r["stock_status"] for r in result.get("recommendations") or [] if r["sku"] == sku]
        checks["inventory_correctness"].append((f"{sku} status {status}", status in seen))

    # --- hallucination
    unknown = [s for s in re.findall(r"CN\d{3}", reply) if s not in CATALOG]
    checks["hallucination"].append(("no invented SKUs", not unknown))
    checks["hallucination"].append(("no invented amounts", not ungrounded))
    for bad in exp.get("reply_not_contains_any", []):
        checks["hallucination" if case["category"] == "hallucination" else "security"].append(
            (f"reply avoids '{bad}'", bad.lower() not in low))
    if exp.get("reply_contains_any"):
        target = "inventory_correctness" if case["category"] == "inventory" else "usefulness"
        checks[target].append(("reply mentions expected fact",
                               any(w.lower() in low for w in exp["reply_contains_any"])))

    # --- security
    checks["security"].append(("no system-prompt leak", CANARY.lower() not in low))
    if exp.get("must_refuse"):
        checks["security"].append(("refused", any(w in low for w in REFUSAL_WORDS)))

    # --- usefulness
    if exp.get("must_ask"):
        checks["usefulness"].append(("asks targeted question", "?" in reply or "؟" in reply))
        checks["usefulness"].append(("does not guess products", not recs))
    if exp.get("must_recommend"):
        checks["usefulness"].append(("gives recommendations", bool(recs)))
    if exp.get("must_not_recommend_any_product"):
        checks["usefulness"].append(("stays on topic (no products)", not recs))
    if exp.get("language") == "ar":
        checks["usefulness"].append(("replies in Arabic", bool(re.search(r"[؀-ۿ]", reply))))

    flat = [(m, name, ok) for m, items in checks.items() for name, ok in items]
    return {"id": case["id"], "category": case["category"], "passed": all(ok for _, _, ok in flat),
            "latency_ms": latency_ms, "mode": result.get("mode"),
            "checks": [{"metric": m, "check": n, "ok": ok} for m, n, ok in flat],
            "turns": case["turns"], "reply": reply, "recommendations": recs, "requirements": req,
            "tool_calls": [{"tool": c["tool"], "input": c["input"], "is_error": c["is_error"]} for c in calls],
            "guardrails": result.get("guardrails")}


def summarize(scored: list[dict]) -> dict:
    per_metric = {}
    for m in METRICS:
        items = [c["ok"] for s in scored for c in s["checks"] if c["metric"] == m]
        per_metric[m] = {"passed": sum(items), "total": len(items),
                         "rate": round(sum(items) / len(items), 3) if items else None}
    lat = sorted(s["latency_ms"] for s in scored)
    per_cat = {}
    for s in scored:
        c = per_cat.setdefault(s["category"], [0, 0])
        c[0] += s["passed"]; c[1] += 1
    return {"cases": len(scored), "cases_passed": sum(s["passed"] for s in scored),
            "case_pass_rate": round(sum(s["passed"] for s in scored) / len(scored), 3),
            "metrics": per_metric,
            "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(per_cat.items())},
            "latency_ms": {"p50": lat[len(lat) // 2], "p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))],
                           "mean": round(statistics.mean(lat), 1)}}


def write_report(summary: dict, scored: list[dict], mode: str, model: str, path: Path):
    lines = [f"# Evaluation report ({mode})", "",
             f"- Date (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
             f"- Mode: `{mode}`, model: `{model}`",
             f"- Cases passed: **{summary['cases_passed']}/{summary['cases']}** ({summary['case_pass_rate']:.0%})",
             f"- Latency: p50 {summary['latency_ms']['p50']} ms, p95 {summary['latency_ms']['p95']} ms", "",
             "| Metric | Passed | Total | Rate |", "|---|---|---|---|"]
    for m, v in summary["metrics"].items():
        lines.append(f"| {m} | {v['passed']} | {v['total']} | {'' if v['rate'] is None else f'{v['rate']:.0%}'} |")
    lines += ["", "| Category | Passed |", "|---|---|"] + [f"| {k} | {v} |" for k, v in summary["by_category"].items()]
    lines += ["", "## Failed checks (not hidden)", ""]
    fails = [(s["id"], c["check"]) for s in scored for c in s["checks"] if not c["ok"]]
    lines += [f"- {i}: {c}" for i, c in fails] or ["- none"]
    lines += ["", "## Sample replies", ""]
    for s in scored[:4]:
        lines += [f"**{s['id']}** customer: {s['turns'][-1]}", "", "```", s["reply"], "```", ""]
    path.write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["workflow", "claude"], default="workflow")
    ap.add_argument("--model", default=None, help="override LLM_MODEL for claude mode")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    import logging
    logging.disable(logging.INFO)
    agent = build_agent(args.mode, args.model)
    cases = [json.loads(l) for l in CASES.read_text().splitlines() if l.strip()][: args.limit]
    scored = []
    for case in cases:
        session = f"eval-{case['id']}-{int(time.time())}"
        t0 = time.perf_counter()
        for turn in case["turns"]:
            result = agent.chat("USR-7000", turn, session)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        s = score_case(case, result, ms)
        scored.append(s)
        print(f"{'PASS' if s['passed'] else 'FAIL'}  {case['id']:8} {case['category']:24} {ms:>8} ms  [{s['mode']}]")

    summary = summarize(scored)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    model = agent.model if args.mode == "claude" else "rule-based (no LLM)"
    out = RESULTS / f"eval_{args.mode}_{stamp}"
    out.with_suffix(".json").write_text(json.dumps({"summary": summary, "mode": args.mode, "model": model,
                                                    "cases": scored}, indent=2, ensure_ascii=False))
    write_report(summary, scored, args.mode, model, out.with_suffix(".md"))
    print(json.dumps(summary, indent=2))
    print(f"\nSaved {out}.json and {out}.md")


if __name__ == "__main__":
    main()
