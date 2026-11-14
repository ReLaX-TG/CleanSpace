"""Server-side pricing. Integers only for money (grosze); Decimal only for parsing quantities.

Rounding rule (explicit):
    line_total = rate_grosze × quantity, rounded HALF UP to a whole grosz;
    subtotal   = sum of line totals;
    top-up     = max(0, MINIMUM_ORDER_GROSZE − subtotal)   — applied ONCE per order;
    total      = subtotal + top-up.
Quantities are stored as scaled integers: quantity_scaled = quantity × 10**decimals.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import config

# Plain decimal notation: optional sign, digits, optional fraction. No exponent ("1e5"),
# no NaN/Infinity. Used before any arithmetic so huge exponents never reach Decimal maths.
PLAIN_DECIMAL_RE = re.compile(r"[+-]?\d+(?:\.\d+)?", re.ASCII)


class QuantityError(ValueError):
    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def service_rate(service_id: str, options: dict) -> int:
    """Rate in grosze per unit for a service and its options (options must be valid)."""
    service = config.SERVICES[service_id]
    if "options" in service:
        group, choices = next(iter(service["options"].items()))
        return choices[options[group]]
    return service["rate_grosze"]


def parse_quantity(service_id: str, raw) -> tuple[int, int]:
    """Parse a quantity for a service. Returns (quantity_scaled, decimals).

    Accepts JSON numbers or numeric strings; rejects booleans, NaN/Infinity, too many
    decimals and values outside the service's min/max.
    """
    service = config.SERVICES[service_id]
    decimals = service["decimals"]
    if isinstance(raw, bool) or raw is None or not isinstance(raw, (int, float, str)):
        raise QuantityError("quantity_number")
    text = str(raw).strip() if not isinstance(raw, float) else repr(raw)
    # Format first: plain decimal notation only (no exponent, NaN or Infinity), at most
    # 12 characters. This bounds the value, so the arithmetic below cannot overflow
    # (e.g. "1e999999" used to raise decimal.Overflow -> HTTP 500).
    if len(text) > 12 or not PLAIN_DECIMAL_RE.fullmatch(text):
        raise QuantityError("quantity_number")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise QuantityError("quantity_number") from None
    scaled = value * (10 ** decimals)
    if scaled != scaled.to_integral_value():
        raise QuantityError("quantity_whole" if decimals == 0 else "quantity_decimals", decimals=decimals)
    low, high = Decimal(service["min"]), Decimal(service["max"])
    if value < low or value > high:
        raise QuantityError("quantity_range", min=service["min"], max=service["max"])
    return int(scaled), decimals


def format_quantity(quantity_scaled: int, decimals: int) -> str:
    """Scaled integer -> plain decimal string, e.g. (65, 1) -> "6.5", (50, 0) -> "50"."""
    if decimals == 0:
        return str(quantity_scaled)
    whole, frac = divmod(quantity_scaled, 10 ** decimals)
    return f"{whole}.{frac:0{decimals}d}"


def line_total(rate_grosze: int, quantity_scaled: int, decimals: int) -> int:
    """rate × quantity rounded half up to a whole grosz, using integers only."""
    scale = 10 ** decimals
    return (rate_grosze * quantity_scaled * 2 + scale) // (2 * scale)


def price_order(items: list[dict]) -> dict:
    """items: validated [{service, quantity_scaled, decimals, options}] -> priced order."""
    lines = []
    for item in items:
        rate = service_rate(item["service"], item["options"])
        total = line_total(rate, item["quantity_scaled"], item["decimals"])
        lines.append({**item, "unit": config.SERVICES[item["service"]]["unit"],
                      "category": config.SERVICES[item["service"]]["category"],
                      "unit_rate_grosze": rate, "line_total_grosze": total})
    subtotal = sum(line["line_total_grosze"] for line in lines)
    topup = max(0, config.MINIMUM_ORDER_GROSZE - subtotal)
    return {"lines": lines, "subtotal_grosze": subtotal, "minimum_topup_grosze": topup,
            "total_grosze": subtotal + topup}


# ---------------------------------------------------------------------------
# Legacy single-service pricing (only for replaying stage-2 requests)
# ---------------------------------------------------------------------------
def calculate_price(cleaning_type: str, area_sqm: int) -> dict:
    rate = config.PRICING["rates"][cleaning_type]["per_sqm_grosze"]
    base = area_sqm * rate
    minimum = config.PRICING["minimum_price_grosze"]
    total = max(base, minimum)
    return {"rate_per_sqm_grosze": rate, "base_grosze": base, "total_grosze": total,
            "minimum_applied": base < minimum}


# ---------------------------------------------------------------------------
# Public catalog for the frontend
# ---------------------------------------------------------------------------
def public_pricing() -> dict:
    services = []
    for key, s in config.SERVICES.items():
        entry = {"id": key, "category": s["category"], "unit": s["unit"],
                 "min": s["min"], "max": s["max"], "decimals": s["decimals"],
                 "step": format_quantity(1, s["decimals"])}
        if "options" in s:
            group, choices = next(iter(s["options"].items()))
            entry["option"] = {"id": group, "choices": [{"id": c, "rateGrosze": r} for c, r in choices.items()]}
        else:
            entry["rateGrosze"] = s["rate_grosze"]
        services.append(entry)
    return {
        "currency": config.CURRENCY,
        "minimumOrderGrosze": config.MINIMUM_ORDER_GROSZE,
        "maxItems": config.MAX_ORDER_ITEMS,
        "rounding": "half_up_per_line",
        "categories": list(config.SERVICE_CATEGORIES),
        "units": list(config.UNITS),
        "services": services,
        "preferredTimes": list(config.PREFERRED_TIMES),
        "timezone": config.BUSINESS_TIMEZONE,
        "fieldLimits": {
            "nameMin": config.FIELD_LIMITS["name_min"],
            "nameMax": config.FIELD_LIMITS["name_max"],
            "emailMax": config.FIELD_LIMITS["email_max"],
            "commentMax": config.FIELD_LIMITS["comment_max"],
            "phoneMax": config.FIELD_LIMITS["phone_max"],
            "phoneDigitsMin": config.FIELD_LIMITS["phone_digits_min"],
            "phoneDigitsMax": config.FIELD_LIMITS["phone_digits_max"],
            "cityMin": config.FIELD_LIMITS["city_min"],
            "cityMax": config.FIELD_LIMITS["city_max"],
            "streetMin": config.FIELD_LIMITS["street_min"],
            "streetMax": config.FIELD_LIMITS["street_max"],
            "apartmentMax": config.FIELD_LIMITS["apartment_max"],
        },
    }
