from telethon import TelegramClient

API_ID=30590567
API_HASH="e9799c47ccc8d8950c9159d61399d2f6"

client = TelegramClient('anon_session', API_ID, API_HASH)

async def main():
    print("Fetching your chats...")
    async for dialog in client.iter_dialogs():
        print(f"Name: {dialog.name}  |  ID: {dialog.id}")

with client:
    client.loop.run_until_complete(main())