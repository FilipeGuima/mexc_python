import asyncio
from mexcpy.api import MexcFuturesAPI

# --- CONFIGURATION ---
# Make sure this is your TESTNET API token
token = "REDACTED"
api = MexcFuturesAPI(token, testnet=True)


async def main():
    """
    Checks the API key by fetching account assets.
    """
    print("--- Checking API Key Permissions ---")
    print("Attempting to fetch your account assets...")

    assets_response = await api.get_user_assets()

    if assets_response.success:
        print("\n✔️ API Key is VALID and can read account information!")
        print("Successfully fetched assets:")
        # Print the details of the first asset found (usually USDT on testnet)
        if assets_response.data:
            first_asset = assets_response.data[0]
            print(f"  - Currency: {first_asset.currency}")
            print(f"  - Available Balance: {first_asset.availableBalance}")
            print(f"  - Equity: {first_asset.equity}")
        else:
            print("  - No assets found in the account.")

    else:
        print(f"\n❌ API Key is INVALID or lacks basic read permissions.")
        print(f"   Error from API: {assets_response.message}")


if __name__ == "__main__":
    asyncio.run(main())