"""
Shared utility functions for all trading bots.
"""


def adjust_price_to_step(price, step_size):
    """Rounds a price to the nearest valid step size allowed by the exchange."""
    if not price:
        return None
    if not step_size or step_size == 0:
        return price

    step_str = f"{float(step_size):.16f}".rstrip('0')
    precision = 0
    if '.' in step_str:
        precision = len(step_str.split('.')[1])

    return round(price, precision)


def validate_signal_tp_sl(signal_data: dict) -> str | None:
    """
    Validates that a parsed signal has required TP and SL fields.

    Returns None if valid, or a formatted error string if invalid.
    The caller should print/return the error and abort the trade.
    """
    validation_errors = []
    symbol = signal_data.get('symbol', 'UNKNOWN')

    if not signal_data.get('sl'):
        validation_errors.append("NO STOP LOSS (SL) in signal")

    if not signal_data.get('tps'):
        validation_errors.append("NO TAKE PROFIT (TP) levels in signal")

    if not validation_errors:
        return None

    error_msg = (
        f"\n{'='*50}\n"
        f" **ORDER REJECTED** - {symbol}\n"
        f"   Reason: Signal missing required TP/SL\n"
        f"   \n"
        f"   Missing:\n"
    )
    for err in validation_errors:
        error_msg += f"   • {err}\n"
    error_msg += (
        f"   \n"
        f"   Raw signal data:\n"
        f"   • Entry: {signal_data.get('entry', 'N/A')}\n"
        f"   • SL: {signal_data.get('sl') or 'MISSING'}\n"
        f"   • TPs: {signal_data.get('tps') or 'MISSING'}\n"
        f"{'='*50}"
    )
    return error_msg
