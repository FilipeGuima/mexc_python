import logging
from datetime import datetime, timezone
from telethon import TelegramClient, events
from bots.common.listener_interface import ListenerInterface

logger = logging.getLogger(__name__)


class TelegramListenerImplementation(ListenerInterface):
    def __init__(self, session_name: str, api_id: str, api_hash: str, target_chats: list,
                 start_time: datetime = None):
        super().__init__()
        self.client = TelegramClient(session_name, api_id, api_hash)
        self.target_chats = target_chats
        self.start_time = start_time or datetime.now(timezone.utc)

    def connect(self):
        """Register message handler and connect to Telegram. Synchronous (blocks briefly)."""
        logger.info(f"Listening to chats: {self.target_chats}")

        start_time = self.start_time

        @self.client.on(events.NewMessage(chats=self.target_chats, incoming=True))
        async def handler(event):
            if event.date < start_time:
                return
            if event.text:
                await self._notify(event.text)

        self.client.start()

    def run_forever(self):
        """Block until disconnected. Synchronous."""
        self.client.run_until_disconnected()
