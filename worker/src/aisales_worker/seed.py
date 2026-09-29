"""One pilot business, and a catalogue worth talking about.

Idempotent: re-running updates rather than duplicating, so it is safe against
a database that already has the pilot's real data in it.

The catalogue deliberately includes awkward cases, because a seed of eight
well-formed products tests nothing:

  * KAF-001 has stock 0        -- "none left" must be refused
  * GEL-001 has stock NULL     -- "not tracked" must NOT be refused. If these
                                  two collapse into one behaviour, the agent
                                  either lies about stock or refuses to sell.
  * prices from ₦3,200 to ₦48,000, so the money guard is exercised at more
    than one digit count.
"""

from __future__ import annotations

import psycopg

from aisales import db

SLUG = "adafabrics"

SETTINGS = {
    # Who the shop belongs to, for the dashboard header. Configured rather than
    # hardcoded: greeting somebody by a name nobody entered is how a demo
    # becomes a lie in production.
    "owner_name": "Ada",
    "tone": "warm, brief, Nigerian English. Never oversell.",
    "confidence_threshold": 0.6,
    # Stops the AI and hands the thread to a person.
    "escalate_on": [
        "refund_request", "complaint", "payment_dispute", "delivery_dispute",
    ],
    # Worth the owner's attention; the AI keeps selling.
    #
    # A discount request is here rather than above on purpose. "Reduce am" is
    # the most common opening move in Nigerian commerce, and handing over
    # permanently on every haggle would take the agent offline for most
    # customers after one message. The guard already makes granting a discount
    # impossible, so holding the price and telling the owner is the right
    # response -- not silencing the shop's best salesperson.
    "flag_on": ["discount_request", "price_objection"],
    "followup": {
        # 120 minutes, not 1440. Meta rejects free-form messages outside the
        # 24-hour service window, so a next-day default would produce
        # follow-ups that are silently refused.
        "first_after_minutes": 120,
        "min_gap_minutes": 1440,
        "max_attempts": 2,
        "quiet_start": "21:00",
        "quiet_end": "08:00",
        "tone": "warm, brief, no pressure",
    },
    # The fenced facts block. Its money figures join the allowed set on every
    # turn, so the agent may quote the delivery fee without a tool call -- and
    # still may not invent one.
    "approved_facts": (
        "Delivery within Lagos is ₦2,500 and takes 1-2 working days. "
        "Delivery outside Lagos is ₦4,500 and takes 3-5 working days. "
        "We do not offer returns on cut fabric; unopened packaged items may be "
        "returned within 7 days. Payment is by bank transfer or card through "
        "the payment link we send. We do not offer discounts; any request for "
        "one must be referred to the owner."
    ),
    # What the agent may say if a customer asks about their data. Sent
    # verbatim or not at all -- the agent must never improvise a policy, which
    # would create a commitment the business never made. See PRIVACY.md, which
    # also flags that messages reach model providers outside Nigeria and that
    # this notice therefore has to say so.
    "privacy_notice": (
        "We keep your messages and your phone number so we can serve you and "
        "keep a record of your orders. Our assistant uses an AI service "
        "outside Nigeria to help answer you. Ask us any time to see or delete "
        "what we hold about you."
    ),
    "paystack_subaccount": None,
}

# (sku, name, price_naira, stock, variants, description)
PRODUCTS: list[tuple[str, str, int, int | None, list[dict], str]] = [
    ("LACE-001", "Swiss Voile Lace", 12_500, 8,
     [{"colour": "black"}, {"colour": "wine"}, {"colour": "gold"}],
     "Premium Swiss voile lace, 5 yards. Sold per set."),
    ("ANK-006", "Ankara Wax Print", 4_500, 24,
     [{"colour": "blue"}, {"colour": "green"}, {"colour": "orange"}],
     "Genuine wax print, 6 yards. Colourfast, does not run."),
    ("ASO-001", "Aso-Oke Set", 35_000, 3,
     [{"colour": "gold"}, {"colour": "cream"}],
     "Handwoven aso-oke, gele and ipele included."),
    # Not tracked. A shop that does not count head ties must still sell them.
    ("GEL-001", "Gele Head Tie", 3_200, None,
     [{"colour": "assorted"}],
     "Stiffened gele, holds shape. Assorted colours."),
    ("AGB-001", "Agbada 3-Piece", 48_000, 5,
     [{"size": "M"}, {"size": "L"}, {"size": "XL"}],
     "Complete agbada set: top, inner, trousers. Tailored fit."),
    # None left. The agent must say so rather than promise it.
    ("KAF-001", "Kaftan (Men)", 18_000, 0,
     [{"size": "M"}, {"size": "L"}],
     "Two-piece kaftan in cotton blend."),
    ("SEN-002", "Senator Material", 9_000, 15,
     [{"colour": "navy"}, {"colour": "charcoal"}],
     "Senator material, 2 yards. Per yard sold separately on request."),
    ("CLU-001", "Beaded Clutch", 7_500, 6,
     [{"colour": "silver"}, {"colour": "gold"}],
     "Hand-beaded evening clutch with detachable chain."),
]


def _search_text(name: str, sku: str, description: str) -> str:
    return " ".join((name, sku, description)).lower()


def seed(dsn: str) -> dict[str, int]:
    """Create or update the pilot business. Returns counts for the CLI to print."""
    db.install(dsn)
    with db.connect(dsn) as conn:
        row = conn.execute(
            """
            insert into businesses (slug, name, settings)
            values (%s, %s, %s)
            on conflict (slug) do update
               set name = excluded.name, settings = excluded.settings
            returning id
            """,
            (SLUG, "Ada Fabrics", psycopg.types.json.Json(SETTINGS)),
        ).fetchone()
        business_id = str(row["id"])

        for sku, name, naira, stock, variants, description in PRODUCTS:
            conn.execute(
                """
                insert into products (business_id, sku, name, price_kobo, stock_qty,
                                      variants, description, search_text)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (business_id, sku) do update
                   set name = excluded.name,
                       price_kobo = excluded.price_kobo,
                       stock_qty = excluded.stock_qty,
                       variants = excluded.variants,
                       description = excluded.description,
                       search_text = excluded.search_text
                """,
                (business_id, sku, name, naira * 100, stock,
                 psycopg.types.json.Json(variants), description,
                 _search_text(name, sku, description)),
            )

    return {"businesses": 1, "products": len(PRODUCTS)}
