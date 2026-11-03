"""Showroom offers shown next to a quotation: reward points and delivery.

These are PROPOSED demo rules (not an existing loyalty programme). They live in code, in one
place, so the AI never invents an offer and the numbers on screen are always calculated the same way.
"""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

QAR_PER_POINT = Decimal("20")          # 1 reward point for every QAR 20 spent
POINT_VALUE_QAR = Decimal("0.10")      # 1 point = QAR 0.10 store credit
FREE_DELIVERY_FROM_QAR = Decimal("500")
DELIVERY_FEE_QAR = Decimal("50")


def project_offers(total_qar: float) -> dict:
    total = Decimal(str(total_qar))
    if total < 0:
        raise ValueError("total cannot be negative")
    points = int((total / QAR_PER_POINT).to_integral_value(rounding=ROUND_DOWN))
    free = total >= FREE_DELIVERY_FROM_QAR
    return {
        "proposed": True,
        "reward_points": points,
        "reward_credit_qar": float((points * POINT_VALUE_QAR).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "free_delivery": free,
        "delivery_fee_qar": 0.0 if free else float(DELIVERY_FEE_QAR),
        "free_delivery_from_qar": float(FREE_DELIVERY_FROM_QAR),
        "rules": "1 point per QAR 20 (1 point = QAR 0.10); free delivery from QAR 500, otherwise QAR 50",
    }
