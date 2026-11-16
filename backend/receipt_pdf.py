"""The PDF a shop customer downloads: a RECEIPT for an order (paid or to pay on delivery) or a QUOTATION.

Every number is copied from receipt_data() in shop_api.py, which reads the saved order, so the PDF always
matches the page. No AI here.
"""
from __future__ import annotations

from datetime import date, datetime

from fpdf import FPDF

from backend.quote_pdf import AMBER, GREEN, GREY, INK, LINE, SOFT, _logo

M = 18           # page margin, mm


def _money(x: float) -> str:
    return f"QAR {x:,.2f}"


def _txt(s) -> str:
    """The built-in PDF font only has Latin-1 letters: swap the few characters it cannot draw."""
    return (str(s or "").replace("•", "*").replace("–", "-").replace("—", "-").replace("’", "'")
            .encode("latin-1", "replace").decode("latin-1"))


def _day(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%A %d %B %Y") if iso else "-"


def _when(v) -> str:
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    return v.strftime("%d %B %Y, %H:%M") if v else "-"


def build_receipt_pdf(d: dict) -> bytes:
    order = d["kind"] == "order"
    pay = d["payment"]
    paid = pay["status"] == "paid"

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=24)
    pdf.set_margins(M, M, M)
    pdf.add_page()
    W = pdf.w - 2 * M

    # header
    _logo(pdf, M, 18)
    pdf.set_xy(M + 13, 18.5)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*INK)
    pdf.cell(80, 6, _txt(d["company"]["name"].upper()))
    pdf.set_xy(M + 13, 24.5)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*GREY)
    pdf.cell(80, 4, "Tiles, bathroom sets and design help across Qatar")
    pdf.set_xy(M, 18)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*INK)
    pdf.cell(W, 9, "RECEIPT" if order else "QUOTATION", align="R")
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.8)
    pdf.line(M, 33, M + W, 33)

    def label_value(x, y, label, value, color=INK, w=56):
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*GREY)
        pdf.cell(w, 4, label.upper())
        pdf.set_xy(x, y + 4.2)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*color)
        pdf.cell(w, 5, _txt(value))

    c1, c2, c3 = M, M + 60, M + 120
    label_value(c1, 38, "Order number" if order else "Quotation number", d["number"])
    label_value(c2, 38, "Date", _when(d["created_at"]))
    if order:
        status = ("Paid by card" if paid else "Card payment not completed" if pay["status"] in ("pending", "failed")
                  else "To pay on delivery")
        label_value(c3, 38, "Payment", status, GREEN if paid else AMBER)
    else:
        label_value(c3, 38, "Valid until", _when(d["valid_until"]).split(",")[0])

    # customer and delivery boxes
    y = 52
    pdf.set_fill_color(*SOFT)
    pdf.rect(M, y, W / 2 - 3, 34, style="F")
    pdf.rect(M + W / 2 + 3, y, W / 2 - 3, 34, style="F")

    def box(x, title, rows):
        pdf.set_xy(x + 4, y + 3.5)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*INK)
        pdf.cell(80, 5, title)
        yy = y + 10
        for r in rows:
            pdf.set_xy(x + 4, yy)
            pdf.set_font("Helvetica", "", 8.8)
            pdf.set_text_color(*INK)
            pdf.cell(W / 2 - 11, 4.6, _txt(r))
            yy += 4.8

    cu = d["customer"]
    box(M, "Customer", [cu["name"], cu["phone"], cu["email"] or ""])
    a = d["address"]
    if order:
        box(M + W / 2 + 3, "Delivery", [f"Zone {a['zone']}, Street {a['street']}, Building {a['building']}",
                                         (f"{a['unit']}, " if a["unit"] else "") + f"{a['area']}, Qatar",
                                         f"On {_day(d['delivery_date'])}"])
    else:
        box(M + W / 2 + 3, "Delivery", [f"Delivery area: {a['area']}", "Delivery day agreed when you order",
                                         f"Contact: {d['salesperson'] or 'our sales team'}"])

    # lines
    cols = [("Product", 70, "L"), ("Size", 22, "L"), ("Quantity", 34, "R"), ("Unit price", 24, "R"), ("Amount", W - 150, "R")]
    pdf.set_xy(M, y + 42)
    pdf.set_fill_color(*INK)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8.5)
    for t, w, al in cols:
        pdf.cell(w, 8, f" {t} " if al == "L" else f"{t} ", fill=True, align=al)
    pdf.ln(8)
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    for i, l in enumerate(d["lines"]):
        yy = pdf.get_y()
        if i % 2:
            pdf.set_fill_color(*SOFT)
            pdf.rect(M, yy, W, 11, style="F")
        if l["kind"] == "tile":
            qty, unit = f"{l['boxes']} boxes · {l['quantity']:g} m²", f"{l['unit_price']:,.2f}/m²"
        else:
            n = int(l["quantity"])
            word = "bag" if l["unit"] == "bag" else "set"
            qty, unit = f"{n} {word}{'s' if n > 1 else ''}", f"{l['unit_price']:,.2f}/{word}"
        pdf.set_xy(M, yy + 1.8)
        pdf.set_font("Helvetica", "B", 9.2)
        pdf.set_text_color(*INK)
        pdf.cell(70, 4.5, _txt(f" {l['name']}"))
        pdf.set_font("Helvetica", "", 8.5)
        pdf.cell(22, 4.5, _txt(f" {l['size_cm'] + ' cm' if l['size_cm'] else '-'}"))
        pdf.cell(34, 4.5, _txt(f"{qty} "), align="R")
        pdf.cell(24, 4.5, f"{unit} ", align="R")
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(W - 150, 4.5, f"{l['line_total']:,.2f} ", align="R")
        pdf.set_xy(M, yy + 6.2)
        pdf.set_font("Helvetica", "", 7.3)
        pdf.set_text_color(*GREY)
        pdf.cell(70, 4, f" {l['sku']}")
        pdf.line(M, yy + 11, M + W, yy + 11)
        pdf.set_y(yy + 11)

    # totals
    y = pdf.get_y() + 5
    tx, tw = M + W - 82, 82

    def row(label, value):
        nonlocal y
        pdf.set_xy(tx, y)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*GREY)
        pdf.cell(tw / 2, 6, label)
        pdf.set_text_color(*INK)
        pdf.cell(tw / 2, 6, value, align="R")
        y += 6.3

    row("Products", _money(d["subtotal"]))
    row("Delivery", "Free" if not d["delivery_fee"] else _money(d["delivery_fee"]))
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.5)
    pdf.line(tx, y + 0.5, tx + tw, y + 0.5)
    y += 2.5
    pdf.set_fill_color(*SOFT)
    pdf.rect(tx, y - 0.5, tw, 9, style="F")
    pdf.set_xy(tx + 2, y)
    pdf.set_font("Helvetica", "B", 11.5)
    pdf.set_text_color(*INK)
    pdf.cell(tw / 2 - 2, 8, "Total paid" if paid else "Total" if not order else "Total to pay")
    pdf.cell(tw / 2 - 2, 8, _money(d["total"]), align="R")
    y += 13

    # payment box (orders)
    if order:
        pdf.set_xy(M, y)
        pdf.set_fill_color(*((232, 246, 238) if paid else (255, 246, 225)))
        pdf.set_text_color(*(GREEN if paid else AMBER))
        pdf.set_font("Helvetica", "B", 9)
        if paid:
            text = (f" Paid {_money(d['total'])} by card ({pay['card'] or 'card'}) on {_when(pay['paid_at'])}. "
                    f"Payment reference {pay['reference']}.")
            if pay.get("test_mode"):
                text += " TEST PAYMENT: no real money was taken."
        elif pay["method"] == "on_delivery":
            text = f" To pay on delivery: {_money(d['total'])}, by card or cash to our driver."
        else:
            text = " The card payment was not completed. You can pay again from your receipt page, or pay on delivery."
        pdf.multi_cell(W, 6, _txt(text), fill=True, align="L", new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y() + 5

    # notes
    pdf.set_xy(M, y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*INK)
    pdf.cell(W, 5, "Good to know")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 8.3)
    pdf.set_text_color(*GREY)
    notes = ["Tile quantities are rounded up to whole boxes and include spare tiles for cutting.",
             "Each tile line is supplied from one production batch where possible, so the shade matches.",
             "All prices are in Qatari riyal (QAR)."]
    if order:
        notes += [f"We will call you before delivery{(' (' + d['salesperson'] + ' from our sales team)') if d['salesperson'] else ''}.",
                  "Unopened full boxes can be returned within 14 days with this receipt."]
    else:
        notes += ["This quotation is not an order. Stock is confirmed when you order."]
    for n in notes:
        pdf.cell(4, 4.8, "-")
        pdf.multi_cell(W - 4, 4.8, _txt(n), align="L", new_x="LMARGIN", new_y="NEXT")

    # footer
    pdf.set_auto_page_break(auto=False)
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    pdf.line(M, pdf.h - 18, M + W, pdf.h - 18)
    pdf.set_xy(M, pdf.h - 16)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*GREY)
    left = f"{d['company']['name']} · Doha, Qatar"
    if d["company"]["demo"]:
        left += " · Demo store: products, prices and stock are demonstration data"
    pdf.cell(W * 0.8, 4, _txt(left))
    pdf.cell(W * 0.2, 4, _txt(d["number"]), align="R")
    return bytes(pdf.output())
