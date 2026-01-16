import asyncio
import sys
from pathlib import Path

current_path = Path(__file__).resolve()
project_root = current_path.parent.parent.parent
sys.path.append(str(project_root))

from blofincpy.api import BlofinFuturesAPI
from mexcpy.config import BLOFIN_API_KEY, BLOFIN_SECRET_KEY, BLOFIN_PASSPHRASE


async def main():
    print("--- Checking Blofin Demo Balance ---")

    api = BlofinFuturesAPI(BLOFIN_API_KEY, BLOFIN_SECRET_KEY, BLOFIN_PASSPHRASE, testnet=True)

    assets = await api.get_user_assets()

    print(f"Found {len(assets)} assets.")
    for asset in assets:
        print(f" - {asset.currency}: Available={asset.availableBalance}, Equity={asset.equity}")

    usdt = next((a for a in assets if a.currency == "USDT"), None)
    if usdt:
        print(f"\n SUCCESS: Found USDT with balance: {usdt.availableBalance}")
    else:
        print("\n FAILURE: USDT not found in response.")


if __name__ == "__main__":
    asyncio.run(main())