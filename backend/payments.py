"""Card payments through Tap Payments (tap.company), a GCC provider that supports QAR.

Flow (Tap's hosted payment page, so card numbers never touch our server):
1. create_charge(): we ask Tap for a charge of the amount due and get back a payment page URL.
2. The customer pays on Tap's page; Tap sends them back to /api/shop/pay/return/{token}?tap_id=chg_...
3. fetch_charge(): we ask Tap ourselves what happened to that charge. Only Tap's own answer marks an order paid;
   nothing the browser sends is trusted. "CAPTURED" means the money was taken.

The secret key is read from TAP_SECRET_KEY (Render settings or the local .env), never from the repo. With a test
key (sk_test_...) Tap only accepts its test cards and no real money moves. Without a key, card payment is simply
not offered and customers can pay on delivery.
API: https://developers.tap.company/docs/charges
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

import httpx

API = "https://api.tap.company/v2"
PAID = "CAPTURED"
FAILED = {"DECLINED", "FAILED", "CANCELLED", "ABANDONED", "RESTRICTED", "VOID", "TIMEDOUT", "UNKNOWN"}


class PaymentError(Exception):
    pass


def _key() -> str:
    return os.getenv("TAP_SECRET_KEY", "").strip()


def enabled() -> bool:
    return bool(_key())


def test_mode() -> bool:
    return _key().startswith("sk_test_")


def _client() -> httpx.Client:
    return httpx.Client(timeout=30, headers={"Authorization": f"Bearer {_key()}", "accept": "application/json",
                                             "content-type": "application/json"})


def create_charge(amount: Decimal, reference: str, description: str, name: str, phone: str, email: Optional[str],
                  return_url: str, webhook_url: Optional[str] = None) -> dict:
    """Returns {"id": "chg_...", "url": Tap's payment page}."""
    if not enabled():
        raise PaymentError("Card payment is not set up.")
    digits = "".join(c for c in phone if c.isdigit())
    number = digits[-8:]
    first, _, last = name.strip().partition(" ")
    body = {"amount": float(amount), "currency": "QAR", "threeDSecure": True, "save_card": False,
            "description": description[:200], "reference": {"transaction": reference, "order": reference},
            "customer": {"first_name": first[:40], "last_name": (last or first)[:40],
                         "phone": {"country_code": "974", "number": number},
                         **({"email": email} if email else {})},
            "source": {"id": os.getenv("TAP_SOURCE", "src_all")},
            "redirect": {"url": return_url}, "metadata": {"reference": reference}}
    if webhook_url:
        body["post"] = {"url": webhook_url}
    try:
        with _client() as c:
            r = c.post(f"{API}/charges", json=body)
    except httpx.HTTPError as e:
        raise PaymentError(f"Could not reach the payment provider ({type(e).__name__}).")
    if r.status_code >= 400:
        raise PaymentError(f"The payment provider refused the payment ({r.status_code}).")
    data = r.json()
    url = (data.get("transaction") or {}).get("url")
    if not data.get("id") or not url:
        raise PaymentError("The payment provider did not return a payment page.")
    return {"id": data["id"], "url": url}


def fetch_charge(charge_id: str) -> dict:
    """Tap's own record of a charge: {"status", "amount", "currency", "card"}."""
    if not enabled() or not charge_id.startswith("chg_"):
        raise PaymentError("Unknown payment.")
    try:
        with _client() as c:
            r = c.get(f"{API}/charges/{charge_id}")
    except httpx.HTTPError as e:
        raise PaymentError(f"Could not reach the payment provider ({type(e).__name__}).")
    if r.status_code >= 400:
        raise PaymentError(f"The payment provider could not find this payment ({r.status_code}).")
    d = r.json()
    card = d.get("card") or {}
    brand = (card.get("brand") or (d.get("source") or {}).get("payment_method") or "").upper()
    return {"id": d.get("id"), "status": (d.get("status") or "").upper(), "amount": Decimal(str(d.get("amount", 0))),
            "currency": d.get("currency"), "reference": (d.get("reference") or {}).get("transaction"),
            "card": f"{brand} •••• {card['last_four']}".strip() if card.get("last_four") else (brand or None)}
