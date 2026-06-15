"""Outbound Telegram rate limiting.

Telegram throttles accounts that send too quickly and answers further requests
with a ``FloodWaitError`` ("a wait of N seconds is required"). When the first
run of the bot publishes a large backlog of media cards, hundreds of writes in a
few seconds will reliably trip this.

A single process-wide :class:`SendGate` solves both halves of the problem:

  * **Pacing** — it serialises every write (send / edit / delete) and keeps a
    minimum gap between them, so the flood limit is not reached in the first
    place.
  * **Absorption** — if a ``FloodWaitError`` happens anyway, the gate waits out
    the required time and retries the *same* operation, so nothing is lost and
    posts stay in order.

Use :func:`wrap_client` to obtain a drop-in proxy around the Telethon client;
all non-write attributes pass straight through unchanged.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

from ..config import settings
from ..logging_setup import get_logger

try:  # real error type in production
    from telethon.errors import FloodWaitError
except Exception:  # pragma: no cover - Telethon missing (e.g. during tests)
    class FloodWaitError(Exception):  # type: ignore[no-redef]
        seconds = 0

log = get_logger("telegram.rate_limit")

T = TypeVar("T")

# Telegram write methods that must be paced. Everything else (downloads, getters,
# connection handling, …) passes through untouched.
_WRITE_METHODS = ("send_message", "send_file", "edit_message", "delete_messages")


class SendGate:
    """Serialises and paces outgoing writes; waits out flood errors."""

    def __init__(self, min_interval: float):
        self._min_interval = max(0.0, float(min_interval))
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def run(self, factory: Callable[[], Awaitable[T]], *, label: str = "send") -> T:
        """Run ``factory()`` (a no-arg coroutine factory) under the gate.

        ``factory`` is called afresh on every attempt because an awaited
        coroutine cannot be awaited again.
        """
        async with self._lock:
            attempts = 0
            while True:
                gap = self._min_interval - (time.monotonic() - self._last)
                if gap > 0:
                    await asyncio.sleep(gap)
                try:
                    result = await factory()
                    self._last = time.monotonic()
                    return result
                except FloodWaitError as exc:
                    attempts += 1
                    wait = int(getattr(exc, "seconds", 0) or 0) + 2
                    if attempts > settings.tg_flood_max_retries:
                        log.error(
                            "%s still flood-limited after %d wait(s); giving up for now",
                            label, attempts - 1,
                        )
                        raise
                    log.warning(
                        "FloodWait on %s: waiting %ds then retrying (attempt %d/%d)",
                        label, wait, attempts, settings.tg_flood_max_retries,
                    )
                    await asyncio.sleep(wait)
                    self._last = time.monotonic()
                    # loop: retry the same operation


# Process-wide singleton: every wrapped client shares the same pacing, because
# Telegram's limits are per-account, not per-call-site.
_gate = SendGate(settings.tg_min_send_interval)


def gate() -> SendGate:
    return _gate


class RateLimitedClient:
    """Transparent proxy that routes write calls through the global gate.

    Any attribute that is not a write method is returned unchanged, so this can
    stand in for the Telethon client anywhere.
    """

    def __init__(self, client):
        self.__dict__["_client"] = client

    def __getattr__(self, name):
        if name == "_client":
            raise AttributeError(name)
        client = self.__dict__["_client"]
        attr = getattr(client, name)
        if name in _WRITE_METHODS and callable(attr):
            def wrapped(*args, **kwargs):
                return _gate.run(lambda: attr(*args, **kwargs), label=name)
            return wrapped
        return attr

    def __setattr__(self, name, value):
        setattr(self.__dict__["_client"], name, value)


def wrap_client(client) -> RateLimitedClient:
    """Wrap a Telethon client so its writes are paced and flood-safe."""
    return RateLimitedClient(client)
