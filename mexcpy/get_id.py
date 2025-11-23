from telethon import TelegramClient

API_ID =
API_HASH = ""

client = TelegramClient('session', API_ID, API_HASH)

async def main():
    print("Fetching your chats...")
    async for dialog in client.iter_dialogs():
        print(f"Name: {dialog.name}  |  ID: {dialog.id}")

with client:
    client.loop.run_until_complete(main())