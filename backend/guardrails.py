"""Guardrails enforced in CODE (C3, E.15).

The paper lists "security mechanisms consisting solely of prompt instructions" as a
critical failure. So the prompt is only ONE layer. The real protection is:
1. Input inspection   - flags known attack patterns before the LLM sees them.
2. Capability limits  - the LLM has no tool that can change prices, stock or approvals,
                        and never supplies the customer id (code injects it).
3. Output checking    - every $ amount in the reply must exist in tool results/catalog;
                        a canary token detects system-prompt leakage; approval and
                        "free" claims are blocked.
4. Authorization      - quotes and sessions are checked against the requesting user.
"""
import re

CANARY = "CNV-CANARY-7731"   # secret marker placed in the system prompt; must never appear in output

INPUT_RULES = {
    "prompt_injection": [r"ignore (all |any )?(of )?(your|previous|prior|the) (rules|instructions)",
                         r"ignore all", r"you are now", r"new instructions", r"developer mode", r"jailbreak"],
    "system_prompt_extraction": [r"system prompt", r"hidden (prompt|instructions)", r"your instructions",
                                 r"reveal (your|the) (prompt|rules|instructions)"],
    "other_customer_data": [r"another customer", r"other customer", r"someone else'?s", r"all quotes",
                            r"customer (list|data|database)"],
    "inventory_manipulation": [r"ignore (the )?inventory", r"everything is available", r"say .* available",
                               r"tell me .* available today"],
    "price_manipulation": [r"for \$\s?\d+ per", r"\bfor free\b", r"\bis free\b", r"management approved",
                           r"manager approved", r"pretend .* (discount|approved)"],
    "admin_pricing": [r"admin(istrator)? pric", r"staff pric", r"internal pric", r"cost price",
                      r"admin(istrator)? credential"],
    "retrieved_doc_as_instructions": [r"run (this|the) .*document as", r"(document|retrieved).* as system instructions",
                                      r"execute (this|the) document"],
}


def inspect_input(message: str) -> list[str]:
    t = message.lower()
    return [cat for cat, patterns in INPUT_RULES.items() if any(re.search(p, t) for p in patterns)]


MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
FORBIDDEN_OUTPUT = [r"management (has )?approved", r"manager (has )?approved", r"discount (is |has been )?approved",
                    r"\b(is|are) free\b", r"administrator password", r"admin credentials"]


def numbers_in(text: str) -> set[float]:
    out = set()
    for n in NUMBER.findall(text):
        try:
            out.add(round(float(n.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def _expand_grounded(allowed: set[float]) -> set[float]:
    """Grounded amounts = numbers from tools/catalog/customer, plus the difference of any two
    larger amounts (>= 100) among them (e.g. "$222.40 under your $1,000 budget" = 1000 - 777.60). Only simple arithmetic
    on already-grounded numbers is accepted, never a brand-new number."""
    base = {round(x, 2) for x in allowed if x >= 0}
    out = set(base)
    nums = sorted(x for x in base if x >= 100)[:300]   # totals/budgets only, so small invented prices can't sneak in
    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            out.add(round(b - a, 2))
    return out


def _is_grounded(value: float, raw: str, grounded: set[float]) -> bool:
    if value in grounded:
        return True
    if "." not in raw:            # whole-dollar amount: accept rounding, e.g. "$778" for 777.60
        return any(abs(g - value) < 1 for g in grounded)
    return False


def check_output(reply: str, allowed_numbers: set[float]) -> list[str]:
    """Returns a list of violations. Empty list = reply is safe to send."""
    violations = []
    if CANARY.lower() in reply.lower():
        violations.append("system_prompt_leak")
    grounded = _expand_grounded(allowed_numbers)
    for amount in MONEY.findall(reply):
        value = round(float(amount.replace(",", "")), 2)
        if not _is_grounded(value, amount, grounded):
            violations.append(f"ungrounded_amount:${amount}")
    t = reply.lower()
    for p in FORBIDDEN_OUTPUT:
        if re.search(p, t):
            violations.append(f"forbidden_claim:{p}")
    return violations
