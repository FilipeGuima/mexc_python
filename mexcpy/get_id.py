import os
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

if not API_ID or not API_HASH:
    print(" ERROR: API_ID or API_HASH missing. Please check your .env file.")
    exit(1)

client = TelegramClient('anon_session', API_ID, API_HASH)


async def main():
    print(f" Logged in as User ID: {API_ID}")
    print("Fetching your chats/groups/channels...")
    print("-" * 40)

    async for dialog in client.iter_dialogs():
        print(f"Name: {dialog.name:<30} | ID: {dialog.id}")

    print("-" * 40)
    print("Copy the ID of the group/channel you want to listen to.")
    print("Paste it into TARGET_CHATS in your .env file.")


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())