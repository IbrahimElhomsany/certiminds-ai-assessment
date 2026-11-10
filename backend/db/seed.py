"""Demo data for Nova Studio: 90 days of Ceramica Nova activity.

    python -m backend.db.seed            # (re)creates data/nova_studio.db

Everything is DEMO data and the company is flagged is_demo=True, so the admin shows a
"Demo data" badge. A fixed random seed makes the data identical on every run, and every
number is built the same way the real system builds it: prices and box rounding from the
catalog (tools.compute_quantity), profit from cost prices, stock from batches.
"""
import json
import os
import random
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import audit, hash_password
from backend.catalog import CATALOG, SANITARY
from backend.db.database import Base, SessionLocal, engine, init_db, local_now
from backend.db.models import (Activity, Batch, Customer, DiscountRequest, Lead, Order, OrderLine, Product,
                               Quote, QuoteLine, StockMovement, Tenant, User)
from backend.tools import compute_quantity

DAYS = 90
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "NovaDemo2026!")
SET_IMAGES = {"basin": "/images/BASIN.jpg", "shower": "/images/SHOWER.jpg", "wc": "/images/WC.jpg"}

STAFF = [  # email, name, role, discount limit %, branch
    ("owner@ceramicanova.demo", "Business Owner", "owner", 15, "Doha"),
    ("manager@ceramicanova.demo", "Sara Haddad", "sales_manager", 10, "Doha"),
    ("omar@ceramicanova.demo", "Omar Khalil", "salesperson", 5, "Doha"),
    ("lina@ceramicanova.demo", "Lina Mansour", "salesperson", 5, "Doha"),
    ("yusuf@ceramicanova.demo", "Yusuf Rahman", "salesperson", 5, "Lusail"),
    ("warehouse@ceramicanova.demo", "Rajesh Nair", "warehouse", 0, "Doha"),
    ("viewer@ceramicanova.demo", "Board Viewer", "viewer", 0, "Doha"),
]
FIRST = ["Ayesha", "Mohammed", "Fatima", "Ahmed", "Noura", "Khalid", "Mariam", "Hassan", "Layla", "Ali",
         "Sara", "Tariq", "Huda", "Faisal", "Reem", "Youssef", "Amal", "Karim", "Dana", "Saeed",
         "Priya", "John", "Elena", "Raj", "Maya", "Daniel", "Hiba", "Nasser", "Salma", "Ibrahim"]
LAST = ["Rahman", "Al-Kuwari", "Haddad", "Nasser", "Saleh", "Mansour", "Farouk", "Al-Sulaiti", "Aziz",
        "Qasim", "Menon", "Smith", "Petrova", "Hamdan", "Yousef", "Khan", "Al-Marri", "Darwish"]
COMPANIES = ["Al Waab Contracting", "Pearl Fitout W.L.L.", "Lusail Build Co.", "Doha Interiors Studio",
             "Gulf Line Contracting", "Atelier Majlis Design", "Rayyan Renovations", "Msheireb Design Lab"]
AREAS = ["Doha", "Lusail", "Al Wakra", "Al Rayyan", "The Pearl", "West Bay", "Al Khor", "Umm Salal"]
SOURCES = [("showroom_chat", 30), ("walk_in", 25), ("shop", 20), ("referral", 15), ("phone", 10)]
ROOMS = {  # room: (weight, floor SKUs, wall SKUs, floor m2 range, wall m2 range)
    "bathroom": (26, ["CN012", "CN005", "CN011", "CN015"], ["CN007", "CN008", "CN010", "CN013"], (5, 14), (15, 35)),
    "kitchen": (18, ["CN005", "CN002", "CN015"], ["CN010", "CN007", "CN015"], (10, 25), (6, 14)),
    "living room": (22, ["CN001", "CN002", "CN003", "CN006", "CN011"], [], (30, 110), None),
    "majlis": (10, ["CN001", "CN006", "CN002", "CN003"], [], (40, 90), None),
    "outdoor": (16, ["CN004", "CN009", "CN014"], [], (20, 80), None),
    "office": (8, ["CN005", "CN015", "CN012"], [], (80, 260), None),
}
STYLE_WORD = {"Marble": "marble look", "Luxury marble": "marble look", "Stone": "stone look", "Wood": "wood look",
              "Slate": "natural stone look", "Natural": "natural stone look", "Industrial": "concrete look"}
SHADES = ["A1", "A2", "B1", "B2", "C1"]


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pick(rng: random.Random, weighted):
    items, weights = zip(*weighted)
    return rng.choices(items, weights=weights)[0]


def at(day: date, rng: random.Random) -> datetime:
    """A showroom-hours timestamp (09:00-21:00) on the given day."""
    return datetime.combine(day, time(9)) + timedelta(minutes=rng.randint(0, 12 * 60))


def later(t: datetime, days: float) -> datetime:
    """t + days, moved into showroom hours (09:00-21:00) so activity never happens at 3 am."""
    t = t + timedelta(days=days)
    if t.hour < 9:
        t = t.replace(hour=9 + t.hour % 4)
    elif t.hour >= 21:
        t = (t + timedelta(days=1)).replace(hour=9 + t.hour % 4)
    return t


# ------------------------------------------------------------------ catalog
def seed_products(db: Session, tenant: Tenant, rng: random.Random) -> dict[str, Product]:
    products = {}
    for p in CATALOG.values():
        box_m2 = p.tile_width_cm * p.tile_length_cm / 10000 * p.tiles_per_box
        products[p.sku] = Product(
            tenant_id=tenant.id, sku=p.sku, name=p.name, kind="tile", category=p.style, unit="m2",
            price=q2(p.price_per_m2), cost_price=q2(p.price_per_m2 * rng.uniform(0.55, 0.66)),
            reorder_point=Decimal(150), image=f"/images/{p.sku}.jpg",
            attributes={"material": p.material, "finish": p.finish, "size_cm": f"{p.tile_width_cm}x{p.tile_length_cm}",
                        "tiles_per_box": p.tiles_per_box, "box_m2": round(box_m2, 4), "slip_rating": p.slip_rating,
                        "surfaces": p.surfaces, "locations": p.locations, "catalog_stock_m2": p.stock_m2,
                        "commercial": p.commercial, "application": p.application})
    for s in SANITARY.values():
        products[s.sku] = Product(
            tenant_id=tenant.id, sku=s.sku, name=s.name, kind="set", category=s.category, unit="set",
            price=q2(s.price), cost_price=q2(s.price * rng.uniform(0.58, 0.66)), reorder_point=Decimal(8),
            image=SET_IMAGES.get(s.category), attributes={"finish": s.finish, "includes": s.includes,
                                                          "catalog_stock_units": s.stock_units})
    db.add_all(products.values())
    db.flush()
    return products


# ------------------------------------------------------------------ main generator
def seed_tenant(db: Session, slug: str = "ceramica-nova", name: str = "Ceramica Nova",
                today: date | None = None, seed: int = 42, password: str = DEMO_PASSWORD,
                bcrypt_rounds: int | None = None) -> Tenant:
    """`today` fixes the date (tests); by default the data ends at the current moment."""
    rng = random.Random(seed)
    now = datetime.combine(today, time(18)) if today else local_now()
    today = now.date()
    start = today - timedelta(days=DAYS - 1)

    tenant = Tenant(slug=slug, name=name, currency="QAR", min_margin_pct=Decimal(25), is_demo=True,
                    logo_url="/images/logo.svg", created_at=datetime.combine(start - timedelta(days=30), time(9)))
    db.add(tenant)
    db.flush()

    if bcrypt_rounds:
        os.environ["BCRYPT_ROUNDS"] = str(bcrypt_rounds)
    pw_hash = hash_password(password)       # same demo password for every demo account
    users = {}
    for email, uname, role, limit, branch in STAFF:
        users[email] = User(tenant_id=tenant.id, email=email, name=uname, role=role, password_hash=pw_hash,
                            discount_limit_pct=Decimal(limit), branch=branch, created_at=tenant.created_at)
    db.add_all(users.values())
    db.flush()
    sellers = [u for u in users.values() if u.role == "salesperson"]
    manager = next(u for u in users.values() if u.role == "sales_manager")
    owner = next(u for u in users.values() if u.role == "owner")

    products = seed_products(db, tenant, rng)
    by_id = {p.id: p for p in products.values()}
    tiles = {s: p for s, p in products.items() if p.kind == "tile"}
    sets = [p for p in products.values() if p.kind == "set"]

    counters = {"L": 0, "Q": 0, "O": 0}

    def number(prefix: str) -> str:
        counters[prefix] += 1
        return f"{prefix}-{counters[prefix]:04d}"

    customers: list[Customer] = []
    sold: list[tuple[datetime, Product, Decimal, OrderLine, str]] = []   # for batch allocation later

    for day_i in range(DAYS):
        day = start + timedelta(days=day_i)
        growth = 1 + day_i / DAYS * 0.6                       # demand grows ~60% over the period
        n_leads = max(0, round(rng.gauss(3.4 * growth, 1.2)))
        if day.weekday() == 4:                                # Friday: quieter
            n_leads = n_leads // 2
        for _ in range(n_leads):
            created = at(day, rng)
            if created > now:
                continue
            ctype = pick(rng, [("retail", 75), ("contractor", 15), ("designer", 10)])
            if customers and rng.random() < 0.12:
                customer = rng.choice(customers)               # returning customer
            else:
                person = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                cname = rng.choice(COMPANIES) if ctype != "retail" else person
                customer = Customer(tenant_id=tenant.id, name=cname, type=ctype, area=rng.choice(AREAS),
                                    phone=f"+974 {rng.randint(3000, 7799)} {rng.randint(1000, 9999)}",
                                    email=f"{person.split()[0].lower()}{rng.randint(1, 99)}@example.com",
                                    created_at=created)
                db.add(customer)
                db.flush()
                customers.append(customer)

            room = pick(rng, [(r, v[0]) for r, v in ROOMS.items()])
            _, floors, walls, f_rng, w_rng = ROOMS[room]
            scale = 2.2 if customer.type == "contractor" else 1
            floor_sku = rng.choice(floors)
            floor_m2 = round(rng.uniform(*f_rng) * scale)
            wall_sku = rng.choice(walls) if walls else None
            wall_m2 = round(rng.uniform(*w_rng) * scale) if walls else 0
            area = floor_m2 + wall_m2
            est = float(tiles[floor_sku].price) * floor_m2 * 1.1 + (float(tiles[wall_sku].price) * wall_m2 * 1.1 if wall_sku else 0)
            budget = Decimal(max(500, round(est * rng.uniform(0.8, 1.4) / 500) * 500))
            style = STYLE_WORD.get(tiles[floor_sku].category, f"{tiles[floor_sku].category.lower()} look")
            source = pick(rng, SOURCES)
            owner_user = rng.choice(sellers)
            summary = (f"{room.capitalize()}, about {area} m², budget QAR {budget:,.0f}, likes a {style} "
                       f"({tiles[floor_sku].name}).")
            lead = Lead(tenant_id=tenant.id, number=number("L"), customer_id=customer.id, owner_id=owner_user.id,
                        stage="new", source=source, room=room, area_m2=Decimal(area), budget=budget, style=style,
                        summary=summary, created_at=created, stage_changed_at=created)
            db.add(lead)
            db.flush()
            db.add(Activity(tenant_id=tenant.id, lead_id=lead.id, user_id=None if source == "showroom_chat" else owner_user.id,
                            type="created", created_at=created,
                            text="Lead created from a Ceramica Nova AI chat" if source == "showroom_chat"
                            else f"Lead created ({source.replace('_', ' ')})"))

            def move(stage: str, when: datetime, text: str, kind: str = "stage_change") -> bool:
                if when > now:
                    return False                      # hasn't happened yet: lead stays where it is
                lead.stage, lead.stage_changed_at = stage, when
                db.add(Activity(tenant_id=tenant.id, lead_id=lead.id, user_id=owner_user.id, type=kind,
                                text=text, created_at=when))
                return True

            pace = 0.4 if source in ("walk_in", "shop") else 1.0   # walk-ins decide faster
            t = later(created, rng.uniform(0.1, 2) * pace)
            if rng.random() > 0.76:                            # never qualified
                if rng.random() < 0.6:
                    lead.lost_reason = "no_response"
                    move("lost", later(t, rng.uniform(3, 8)), "Marked lost: no response")
                continue
            if not move("qualified", t, "Qualified: site details and budget confirmed"):
                continue

            # ---- quotation
            t = later(t, rng.uniform(0.5, 3) * pace)
            if rng.random() > 0.78 or t > now:
                continue
            lines = []
            for sku, m2 in ((floor_sku, floor_m2), (wall_sku, wall_m2)):
                if not sku or m2 <= 0:
                    continue
                p = tiles[sku]
                qty = compute_quantity(m2, *map(int, p.attributes["size_cm"].split("x")),
                                       p.attributes["tiles_per_box"], 10, float(p.price))
                lines.append((p, Decimal(str(qty.total_purchasable_area_m2)), qty.box_count))
            if room == "bathroom" and rng.random() < 0.55:
                for s in rng.sample(sets, rng.randint(1, 2)):
                    lines.append((s, Decimal(1), None))
            qlines = [QuoteLine(product_id=p.id, quantity=qty, boxes=boxes, unit_price=p.price,
                                unit_cost=p.cost_price, line_total=q2(qty * p.price)) for p, qty, boxes in lines]
            subtotal = sum(l.line_total for l in qlines)
            cost = q2(sum(l.quantity * l.unit_cost for l in qlines))
            disc = Decimal(0) if rng.random() < 0.5 else Decimal(rng.choice([3, 5, 5, 7, 8, 10, 12]))
            quote = Quote(tenant_id=tenant.id, number=number("Q"), lead_id=lead.id, customer_id=customer.id,
                          created_by=owner_user.id, status="sent",
                          channel={"showroom_chat": "chat", "shop": "shop"}.get(source, "staff"), subtotal=subtotal, discount_pct=Decimal(0),
                          discount_amount=Decimal(0), total=subtotal, cost_total=cost, created_at=t, sent_at=t,
                          valid_until=t + timedelta(days=14), lines=qlines)
            db.add(quote)
            db.flush()

            # ---- discount, checked against the margin rule (feature 5)
            if disc:
                total_after = q2(subtotal * (1 - disc / 100))
                margin = q2((total_after - cost) / total_after * 100)
                req = DiscountRequest(tenant_id=tenant.id, quote_id=quote.id, requested_by=owner_user.id,
                                      percent=disc, margin_after_pct=margin, created_at=t)
                approved = True
                if disc <= owner_user.discount_limit_pct and margin >= tenant.min_margin_pct:
                    req.status, req.reason = "auto_approved", "Within salesperson limit and minimum margin"
                elif t > now - timedelta(days=4):
                    req.status, approved = "pending", False      # still waiting for the manager
                elif margin < tenant.min_margin_pct:
                    req.status, approved = "rejected", False
                    req.reason, req.decided_by = f"Margin {margin}% is below the {tenant.min_margin_pct}% minimum", manager.id
                    req.decided_at = later(t, rng.uniform(0.05, 0.8))
                else:
                    req.status, req.decided_by = "approved", manager.id
                    req.decided_at = later(t, rng.uniform(0.05, 0.8))
                    req.reason = "Approved by sales manager"
                db.add(req)
                db.flush()
                if req.decided_by:
                    audit(db, tenant.id, "discount.approve" if req.status == "approved" else "discount.reject",
                          user_id=manager.id, entity="quote", entity_id=quote.number,
                          after={"percent": float(disc), "margin_after_pct": float(margin)}, at=req.decided_at)
                if approved:
                    quote.discount_pct = disc
                    quote.discount_amount = subtotal - total_after
                    quote.total = total_after
            move("quote_sent", t, f"Quotation {quote.number} sent (QAR {quote.total:,.2f})", "quote_sent")

            # ---- outcome
            t = later(t, rng.uniform(1, 5) * pace)
            if rng.random() < 0.55 and move("negotiation", t, "Customer asked to revise quantities or price"):
                t = later(t, rng.uniform(1, 6) * pace)
            r = rng.random()
            if r < 0.48:
                if not move("won", t, f"Won: customer accepted {quote.number}"):
                    continue
                quote.status = "accepted"
                order = Order(tenant_id=tenant.id, number=number("O"), quote_id=quote.id, customer_id=customer.id,
                              created_by=owner_user.id, status="confirmed", total=quote.total,
                              cost_total=quote.cost_total, created_at=t)
                factor = quote.total / quote.subtotal if quote.subtotal else Decimal(1)
                for ql in quote.lines:
                    ol = OrderLine(product_id=ql.product_id, quantity=ql.quantity, unit_price=q2(ql.unit_price * factor),
                                   unit_cost=ql.unit_cost, line_total=q2(ql.line_total * factor))
                    order.lines.append(ol)
                    sold.append((t, by_id[ql.product_id], ql.quantity, ol, order.number))
                delivered = later(t, rng.uniform(2, 7))
                if delivered <= now:
                    order.status, order.delivered_at = "delivered", delivered
                db.add(order)
                db.flush()
                db.add(Activity(tenant_id=tenant.id, lead_id=lead.id, user_id=owner_user.id, type="order",
                                text=f"Order {order.number} confirmed", created_at=t))
            elif r < 0.75:
                if move("lost", t, "Marked lost"):
                    quote.status = "rejected"
                    lead.lost_reason = pick(rng, [("price", 40), ("competitor", 25), ("timing", 20), ("stock", 15)])
            elif t < now - timedelta(days=30):
                quote.status = "expired"                     # old quote nobody followed up

    seed_batches(db, tenant, products, sold, start, rng, users["warehouse@ceramicanova.demo"])
    audit(db, tenant.id, "seed", user_id=owner.id, entity="tenant", entity_id=tenant.slug,
          after={"days": DAYS, "leads": counters["L"], "quotes": counters["Q"], "orders": counters["O"]}, at=now)
    db.commit()
    return tenant


def seed_batches(db: Session, tenant: Tenant, products: dict[str, Product], sold, start: date,
                 rng: random.Random, warehouse: User) -> None:
    """Three deliveries per product (before the period, day 30, day 60). Each sale is taken from the
    batch that was the current one on that date, so every order line comes from ONE batch (feature 8).
    Received quantities are worked out so that today's stock matches the catalog's stock figure,
    with a small leftover in the older batches (odd boxes, as in a real warehouse)."""
    arrivals = [start - timedelta(days=20), start + timedelta(days=30), start + timedelta(days=60)]
    for p in products.values():
        tile = p.kind == "tile"
        target_left = Decimal(str(p.attributes["catalog_stock_m2" if tile else "catalog_stock_units"]))
        p_sales = [s for s in sold if s[1] is p]
        batches = []
        for i, arrived in enumerate(arrivals):
            arrived_dt = datetime.combine(arrived, time(8))
            next_dt = datetime.combine(arrivals[i + 1], time(8)) if i + 1 < len(arrivals) else None
            mine = [s for s in p_sales if s[0] >= arrived_dt and (next_dt is None or s[0] < next_dt)]
            sold_qty = sum((s[2] for s in mine), Decimal(0))
            if i < 2:
                left = q2(rng.uniform(4, 30)) if tile else Decimal(rng.randint(0, 2))
            else:
                left = target_left - sum(b.qty_left for b in batches)
            left = max(left, Decimal(0))
            code = f"{p.sku}-{arrived.strftime('%y%m')}-{chr(65 + i)}"
            b = Batch(tenant_id=tenant.id, product_id=p.id, code=code, shade=rng.choice(SHADES) if tile else None,
                      received_at=arrived_dt, received_qty=sold_qty + left, qty_left=left)
            db.add(b)
            db.flush()
            batches.append(b)
            db.add(StockMovement(tenant_id=tenant.id, product_id=p.id, batch_id=b.id, qty=b.received_qty,
                                 reason="receive", reference=code, user_id=warehouse.id, created_at=arrived_dt))
            for when, _, qty, line, order_no in mine:
                line.batch_id = b.id
                db.add(StockMovement(tenant_id=tenant.id, product_id=p.id, batch_id=b.id, qty=-qty,
                                     reason="sale", reference=order_no, created_at=when))


def reset_and_seed(today: date | None = None) -> Tenant:
    Base.metadata.drop_all(bind=engine)
    init_db()
    with SessionLocal() as db:
        return seed_tenant(db, today=today)


if __name__ == "__main__":
    t = reset_and_seed()
    with SessionLocal() as db:
        n = {m.__tablename__: len(db.scalars(select(m).where(m.tenant_id == t.id)).all())
             for m in (User, Customer, Lead, Quote, Order, Product, Batch, DiscountRequest)}
    print(json.dumps({"tenant": t.slug, "demo_password": "see DEMO_PASSWORD (default in README)", **n}, indent=2))
