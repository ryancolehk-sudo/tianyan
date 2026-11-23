"""Small, dependency-free decimal calculations for cached quote presentation."""
from decimal import Decimal, InvalidOperation, localcontext


def changes(price, previous_close):
    """Return price delta and percentage points as decimal strings; unknown stays null."""
    if price is None or previous_close is None:
        return None, None
    try:
        with localcontext() as context:
            context.prec = 40
            current, previous = Decimal(price), Decimal(previous_close)
            if not current.is_finite() or not previous.is_finite() or current <= 0 or previous <= 0:
                return None, None
            difference = current - previous
            percentage = difference / previous * Decimal(100)
            # A tiny negative result can round to zero; do not display -0.00%.
            return format(difference, 'f'), format(percentage.quantize(Decimal('0.01')) + Decimal(0), '.2f')
    except (InvalidOperation, ValueError):
        return None, None
