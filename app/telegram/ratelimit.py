"""Outbound send pacing and flood resilience for the userbot.

A real account that posts/edits into a single topic in bursts is the classic
trigger for Telegram's anti-spam, which answers with ever-larger
``FloodWaitError`` penalties (we observed waits of >1000s during a backfill).

Two mechanisms keep the account healthy:

  * ``throttle()`` serialises message-mutating calls and enforces a minimum
    gap between them (``settings.send_min_interval``). Pacing the sends
    *prevents* most floods from ever being imposed.
  * ``safe_call()`` wraps a Telegram coroutine: it throttles, runs it, treats
    "nothing changed" as success, and — as a backstop for a wait larger than
    the client's auto-sleep threshold — waits the penalty out once (capped by
    ``settings.flood_wait_max``) and retries instead of letting the exception
    crash a worker.

The factory pattern (a zero-arg callable returning a fresh coroutine) is used
so a call can be retried after sleeping.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, TypeVar

from telethon import errors

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("telegram.ratelimit")

T = TypeVar("T")

_lock = asyncio.Lock()
_last_send = 0.0


async def throttle() -> None:
    """Block until at least ``send_min_interval`` has passed since the last send."""
    global _last_send
    async with _lock:
        loop = asyncio.get_event_loop()
        wait = settings.send_min_interval - (loop.time() - _last_send)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_send = loop.time()


async def safe_call(factory: Callable[[], Awaitable[T]],
                    *, what: str = "send") -> Optional[T]:
    """Throttle and execute a Telegram call with flood/no-op resilience.

    Returns the call's result, or ``None`` when the edit changed nothing.
    Re-raises anything that is not a flood/no-op condition.
    """
    while True:
        await throttle()
        try:
            return await factory()
        except errors.MessageNotModifiedError:
            return None  # editing to identical content — treat as done
        except errors.FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 0)) + 1
            if seconds > settings.flood_wait_max:
                log.error("%s hit FloodWait %ss exceeding cap %ss; giving up.",
                          what, seconds, settings.flood_wait_max)
                raise
            log.warning("%s hit FloodWait %ss; waiting it out then retrying.",
                        what, seconds)
            await asyncio.sleep(seconds)
            # loop and retry
