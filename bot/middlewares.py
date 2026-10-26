"""Защита от флуда: слишком частые нажатия и сообщения одного пользователя пропускаются.
Так один человек не может завалить бота запросами и вызвать ограничения Telegram для всех."""
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, interval: float = 0.5, max_users: int = 10_000):
        self.interval = interval
        self.max_users = max_users
        self._last: dict[int, float] = {}

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        now = time.monotonic()
        if now - self._last.get(user.id, float("-inf")) < self.interval:
            if isinstance(event, CallbackQuery):
                with contextlib.suppress(Exception):
                    await event.answer()  # убираем «часики» на кнопке
            return None
        self._last[user.id] = now
        if len(self._last) > self.max_users:
            self._last = {k: v for k, v in self._last.items() if now - v < self.interval}
        return await handler(event, data)
