"""Saves one end-to-end conversation (the video scenario) as evidence: reports/sample_conversation.json

    python -m evaluation.save_sample_conversation                 # offline workflow mode, free
    python -m evaluation.save_sample_conversation --mode claude   # real Claude agent (small API cost)

Every reply is stored together with what the code computed (tool calls, quote, guardrail flags),
so a reader can check that each number in the reply came from a tool.
"""
import argparse
import json
from datetime import datetime, timezone

from evaluation.run_eval import ROOT, build_agent

TURNS = [
    "Hi",
    "I'm renovating a 24 m² bathroom floor. Budget QAR 8,000.",
    "Sandstone Pro sounds good",
    "Add a basin and a shower set",
    "Yes please, prepare the quote",
    "Can you give me 20% off?",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["workflow", "claude"], default="workflow")
    ap.add_argument("--model", default=None, help="override LLM_MODEL for claude mode")
    args = ap.parse_args()

    agent = build_agent(args.mode, args.model)
    session, turns = None, []
    for msg in TURNS:
        r = agent.chat("USR-1001", msg, session)
        session = r["session_id"]
        turns.append({"customer": msg, "assistant": r["reply"], "mode": r["mode"],
                      "tool_calls": r.get("tool_calls", []), "quote": r.get("quote"),
                      "guardrails": r.get("guardrails"), "latency_ms": r.get("latency_ms")})
        print(f"\n> {msg}\n{r['reply']}")

    out = ROOT / "reports" / "sample_conversation.json"
    out.write_text(json.dumps({"saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "run_mode": args.mode, "turns": turns}, indent=2, ensure_ascii=False, default=str))
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
