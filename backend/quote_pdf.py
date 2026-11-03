"""Builds a printable PDF copy of a quotation.

No AI here: every number is copied from the Quote object that tools.generate_quote
created, so the PDF always matches the quote on screen.
"""
from datetime import date

from fpdf import FPDF

from backend.catalog import SANITARY, get_product
from backend.tools import Quote

INK = (17, 17, 17)
GREY = (110, 110, 110)
LINE = (225, 225, 225)
SOFT = (246, 246, 246)
GREEN = (47, 168, 101)
AMBER = (196, 128, 0)


def _money(x: float) -> str:
    return f"QAR {x:,.2f}"


def _logo(pdf: FPDF, x: float, y: float) -> None:
    """The same mark as the website: four small rotated squares."""
    pdf.set_fill_color(*INK)
    for dx, dy in ((0, 0), (5, 0), (0, 5), (5, 5)):
        cx, cy, s = x + dx + 2, y + dy + 2, 1.7
        pdf.polygon([(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)], style="F")


def build_quote_pdf(q: Quote, issued: date | None = None) -> bytes:
    issued = issued or date.today()
    pending = q.status == "PENDING_HUMAN_APPROVAL"

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    width = pdf.w - 36

    # Header: logo + name on the left, document title on the right
    _logo(pdf, 18, 18)
    pdf.set_xy(31, 18.5)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*INK)
    pdf.cell(80, 6, "CERAMICA NOVA")
    pdf.set_xy(31, 24.5)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*GREY)
    pdf.cell(80, 4, "Tiles and surfaces")
    pdf.set_xy(18, 18)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*INK)
    pdf.cell(width, 9, "QUOTATION", align="R")
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.8)
    pdf.line(18, 33, 18 + width, 33)

    # Details block: two columns of label / value
    def detail(x, y, label, value, color=INK):
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*GREY)
        pdf.cell(40, 4, label.upper())
        pdf.set_xy(x, y + 4.5)
        pdf.set_font("Helvetica", "B", 10.5)
        pdf.set_text_color(*color)
        pdf.cell(80, 5, value)

    detail(18, 39, "Quotation number", q.quotation_id)
    detail(18, 51, "Customer", q.customer_id)
    detail(110, 39, "Date", issued.strftime("%d %B %Y"))
    detail(110, 51, "Status", "Awaiting manager approval" if pending else "Ready",
           AMBER if pending else GREEN)

    # Line items table
    cols = [("Product", 62, "L"), ("Size / finish", 34, "L"), ("Area", 18, "R"),
            ("Qty", 15, "R"), ("Unit price", 22, "R"), ("Amount (QAR)", width - 151, "R")]
    pdf.set_xy(18, 66)
    pdf.set_fill_color(*INK)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8.5)
    for title, w, align in cols:
        pdf.cell(w, 8, f" {title} " if align == "L" else f"{title} ", fill=True, align=align)
    pdf.ln(8)

    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    for i, line in enumerate(q.products):
        if line.unit == "set":                          # bathroom set: priced per set
            s = SANITARY.get(line.sku)
            spec = s.finish if s else ""
            sub = f"{s.category.upper() if s.category == 'wc' else s.category.capitalize()} set" if s else ""
            area, qty, unit_price = "-", f"{line.quantity_units} set{'s' if line.quantity_units > 1 else ''}", f"{line.unit_price:,.2f}/set"
        else:
            try:
                p = get_product(line.sku)
                spec = f"{p.tile_width_cm}x{p.tile_length_cm} cm, {p.finish}"
                sub = f"{p.slip_rating} slip rating" if p.slip_rating else "Wall tile"
            except Exception:
                spec, sub = "", ""
            area, qty, unit_price = f"{line.quantity_m2:g} m²", f"{line.quantity_boxes} boxes", f"{line.unit_price:,.2f}/m²"
        y = pdf.get_y()
        if i % 2:
            pdf.set_fill_color(*SOFT)
            pdf.rect(18, y, width, 12, style="F")
        pdf.set_xy(18, y + 2)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*INK)
        pdf.cell(62, 4.5, f" {line.display_name or line.product_name}")
        pdf.set_font("Helvetica", "", 8.5)
        pdf.cell(34, 4.5, f" {spec}")
        pdf.cell(18, 4.5, f"{area} ", align="R")
        pdf.cell(15, 4.5, f"{qty} ", align="R")
        pdf.cell(22, 4.5, f"{unit_price} ", align="R")
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(width - 151, 4.5, f"{line.line_subtotal:,.2f} ", align="R")
        pdf.set_xy(18, y + 6.5)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*GREY)
        pdf.cell(62, 4, f" {line.sku} · {sub}" if sub else f" {line.sku}")
        pdf.line(18, y + 12, 18 + width, y + 12)
        pdf.set_y(y + 12)

    # Totals block on the right
    y = pdf.get_y() + 5
    tx, tw = 18 + width - 80, 80

    def total_row(label, value, bold=False, color=INK):
        nonlocal y
        pdf.set_xy(tx, y)
        pdf.set_font("Helvetica", "B" if bold else "", 10 if bold else 9)
        pdf.set_text_color(*(INK if bold else GREY))
        pdf.cell(tw / 2, 6, label)
        pdf.set_text_color(*color)
        pdf.cell(tw / 2, 6, value, align="R")
        y += 6.5

    total_row("Subtotal", _money(q.subtotal))
    if q.discount_percent:
        label = f"Discount ({q.discount_percent:g}%)"
        if pending:
            label += " *"
        total_row(label, "-" + _money(q.discount_amount))
    pdf.set_draw_color(*INK)
    pdf.set_line_width(0.5)
    pdf.line(tx, y + 0.5, tx + tw, y + 0.5)
    y += 2.5
    pdf.set_fill_color(*SOFT)
    pdf.rect(tx, y - 0.5, tw, 9, style="F")
    pdf.set_xy(tx + 2, y)
    pdf.set_font("Helvetica", "B", 11.5)
    pdf.set_text_color(*INK)
    pdf.cell(tw / 2 - 2, 8, f"Total ({q.currency})")
    pdf.cell(tw / 2 - 2, 8, _money(q.total), align="R")
    y += 14

    # Approval note
    if pending:
        pdf.set_xy(18, y)
        pdf.set_fill_color(255, 246, 225)
        pdf.set_text_color(*AMBER)
        pdf.set_font("Helvetica", "B", 9)
        pdf.multi_cell(width, 5.5,
                       f" * The {q.discount_percent:g}% discount is above our 12% limit and needs a "
                       "manager's approval. Until then this quotation is not final.",
                       fill=True, new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y() + 6

    # Notes
    pdf.set_xy(18, max(y, pdf.get_y() + 4))
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*INK)
    pdf.cell(width, 5, "Notes")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*GREY)
    for note in ("Tile quantities are rounded up to whole boxes and include extra tiles for cutting.",
                 "All prices are in Qatari riyal (QAR) and come from the official Ceramica Nova catalog.",
                 "Discounts above 12% always require a manager's approval.",
                 "This quotation is not an order. Stock is confirmed when the order is placed."):
        pdf.cell(4, 5, "-")
        pdf.multi_cell(width - 4, 5, note, new_x="LMARGIN", new_y="NEXT")

    # Footer (pinned to the bottom, so switch off automatic page breaks first)
    pdf.set_auto_page_break(auto=False)
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    pdf.line(18, pdf.h - 18, 18 + width, pdf.h - 18)
    pdf.set_xy(18, pdf.h - 16)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*GREY)
    pdf.cell(width / 2, 4, "Ceramica Nova · Free expert advice and instant quotations")
    pdf.cell(width / 2, 4, f"{q.quotation_id}", align="R")

    return bytes(pdf.output())
