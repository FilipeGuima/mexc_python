"""
Test Signal Generator for Blofin Bots

Finds the top 5 most volatile coins and generates test signals with TPs
close to current price (~1%) for quick testing.

Usage: python bots/test/generate_test_signal.py
"""
import asyncio
import sys
from pathlib import Path

current_path = Path(__file__).resolve()
project_root = current_path.parent.parent.parent
sys.path.append(str(project_root))

from blofincpy.api import BlofinFuturesAPI
from mexcpy.config import BLOFIN_API_KEY, BLOFIN_SECRET_KEY, BLOFIN_PASSPHRASE

BlofinAPI = BlofinFuturesAPI(
    api_key=BLOFIN_API_KEY,
    secret_key=BLOFIN_SECRET_KEY,
    passphrase=BLOFIN_PASSPHRASE,
    testnet=True
)


async def get_volatile_coins(top_n: int = 5):
    """Find the most volatile coins by 24h price range."""
    res = await BlofinAPI._make_request("GET", "/api/v1/market/tickers")

    if not res or res.get('code') != '0':
        return []

    tickers = res.get('data', [])

    # Filter USDT pairs and calculate volatility
    volatile_coins = []
    for t in tickers:
        symbol = t.get('instId', '')
        if not symbol.endswith('-USDT'):
            continue

        try:
            last_price = float(t.get('last', 0))
            high_24h = float(t.get('high24h', 0))
            low_24h = float(t.get('low24h', 0))

            if last_price > 0 and high_24h > 0 and low_24h > 0:
                volatility = (high_24h - low_24h) / last_price * 100
                volatile_coins.append({
                    'symbol': symbol,
                    'price': last_price,
                    'volatility': volatility,
                    'high': high_24h,
                    'low': low_24h
                })
        except (ValueError, TypeError):
            continue

    if not volatile_coins:
        return []

    # Sort by volatility and return top N
    volatile_coins.sort(key=lambda x: x['volatility'], reverse=True)
    return volatile_coins[:top_n]


def format_price(price):
    """Format price nicely."""
    if price >= 1000:
        return f"{price:,.2f}"
    elif price >= 1:
        return f"{price:.4f}"
    else:
        return f"{price:.6f}"


def generate_signal(coin, side: str = "LONG") -> str:
    """Generate a test signal for a coin."""
    symbol = coin['symbol']
    price = coin['price']
    pair = symbol.replace('-', '/')

    if side == "LONG":
        tp1 = price * 1.003   # +0.3%
        tp2 = price * 1.006   # +0.6%
        tp3 = price * 1.01    # +1.0%
        sl = price * 0.99     # -1.0%
        entry = price * 0.999  # Slightly below current
    else:  # SHORT
        tp1 = price * 0.997   # -0.3%
        tp2 = price * 0.994   # -0.6%
        tp3 = price * 0.99    # -1.0%
        sl = price * 1.01     # +1.0%
        entry = price * 1.001  # Slightly above current

    signal = f"""**TRADING SIGNAL ALERT**

**PAIR:** {pair}
**(TEST SIGNAL)**

**SIZE: 10%**
**SIDE:** __{side}__

**ENTRY:** {format_price(entry)}
**SL:** {format_price(sl)}

**TAKE PROFIT TARGETS:**

**TP1:** {format_price(tp1)}
**TP2:** {format_price(tp2)}
**TP3:** {format_price(tp3)}

**LEVERAGE:** 5x"""

    return signal


async def main():
    print("\n" + "="*60)
    print("  TEST SIGNAL GENERATOR FOR BLOFIN - TOP 5 VOLATILE COINS")
    print("="*60)

    print("\nFetching market data...")

    top_5 = await get_volatile_coins(5)

    if not top_5:
        print("Error: Could not fetch market data")
        return

    print("\nTop 5 Most Volatile (24h):")
    print("-" * 50)
    for i, coin in enumerate(top_5, 1):
        print(f"  {i}. {coin['symbol']}: ${format_price(coin['price'])} ({coin['volatility']:.2f}% volatility)")

    print("\n" + "="*60)
    print("  SIGNALS FOR ALL TOP 5 COINS")
    print("  (Copy any signal below to Telegram)")
    print("="*60)

    for i, coin in enumerate(top_5, 1):
        symbol = coin['symbol']
        price = coin['price']
        volatility = coin['volatility']

        print(f"\n{'#'*60}")
        print(f"  #{i} - {symbol}")
        print(f"  Price: ${format_price(price)} | Volatility: {volatility:.2f}%")
        print(f"{'#'*60}")

        # LONG signal
        print(f"\n--- LONG SIGNAL for {symbol} ---")
        print(generate_signal(coin, "LONG"))

        # SHORT signal
        print(f"\n--- SHORT SIGNAL for {symbol} ---")
        print(generate_signal(coin, "SHORT"))

    print("\n" + "="*60)
    print("  NOTES:")
    print("  - TPs will hit within ~1% price movement")
    print("  - Using low leverage (5x) for safety")
    print("  - Pick the coin/direction based on current trend")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
