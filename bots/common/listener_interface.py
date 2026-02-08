from abc import ABC, abstractmethod
from typing import Callable, Awaitable


class ListenerInterface(ABC):
    def __init__(self):
        self._callback: Callable[[str], Awaitable[None]] = None

    def register_callback(self, callback: Callable[[str], Awaitable[None]]):
        self._callback = callback

    async def _notify(self, message: str):
        if self._callback:
            await self._callback(message)

    @abstractmethod
    def connect(self):
        """Connect to the message source and register handlers. Synchronous."""
        pass

    @abstractmethod
    def run_forever(self):
        """Block until disconnected. Synchronous."""
        pass
