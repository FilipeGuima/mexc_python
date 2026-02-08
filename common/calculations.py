# common/calculations.py
import math

def calculate_volume(balance: float, leverage: int, price: float, contract_size: float = 1.0, equity_perc: float = 1.0) -> float:
    """Calculates position size in contracts."""
    if price == 0: return 0.0
    margin_amount = balance * (equity_perc / 100.0)
    notional_value = margin_amount * leverage
    contract_usdt_value = contract_size * price
    return notional_value / contract_usdt_value

def round_to_step(value: float, step: float) -> float:
    """Rounds a price/size to the nearest exchange step size."""
    if step == 0: return value
    return round(value / step) * step