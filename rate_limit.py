"""Outbound Telegram rate limiting.

Telegram throttles accounts that send too quickly and answers further requests
with a ``FloodWaitError`` ("a wait of N seconds is required"). This can be hit
both by the first-run card backlog *and* by ordinary command replies, because
all of them ultimately call the same ``send_message`` on the one shared client.

A single process-wide :class:`SendGate` solves both halves of the problem:

  * **Pacing** — it serialises every write (send / edit / delete) and keeps a
    minimum gap between them, so the flood limit is not reached in the first
    place.
  * **Absorption** — if a ``FloodWaitError`` happens anyway, the gate waits out
    the required time and retries the *same* operation, so nothing is lost and
    posts stay in order.

:func:`install_rate_limit` patches the client's write methods **in place**, so
*every* send through that client — direct calls as well as ``event.reply`` /
``event.respond`` / ``message.edit`` — is paced and flood-safe.
"""
from __future__ import annotations

import asyncio
import contextvars
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
# connection handling, …) is left untouched.
_WRITE_METHODS = ("send_message", "send_file", "edit_message", "delete_messages")

# True while the current task is already executing inside the gate. Telethon may
# internally chain one write into another (e.g. send_message -> send_file); the
# inner call must NOT try to re-acquire the gate's lock or it would deadlock.
_in_gate: "contextvars.ContextVar[bool]" = contextvars.ContextVar("_in_gate", default=False)


class SendGate:
    """Serialises and paces outgoing writes; waits out flood errors."""

    def __init__(self, min_interval: float):
        self._min_interval = max(0.0, float(min_interval))
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def run(self, factory: Callable[[], Awaitable[T]], *, label: str = "send") -> T:
        """Run ``factory()`` (a no-arg coroutine factory) under the gate.

        ``factory`` is called afresh on every attempt because an awaited
        coroutine cannot be awaited again. Nested calls within the same task
        bypass the gate to avoid a re-entrant deadlock.
        """
        if _in_gate.get():
            return await factory()

        token = _in_gate.set(True)
        try:
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
        finally:
            _in_gate.reset(token)


# Process-wide singleton: every send shares the same pacing, because Telegram's
# limits are per-account, not per-call-site.
_gate = SendGate(settings.tg_min_send_interval)


def gate() -> SendGate:
    return _gate


def install_rate_limit(client):
    """Patch ``client``'s write methods in place so every send is flood-safe.

    Idempotent: re-installing on an already-patched client is a no-op. Returns
    the same client for convenience.
    """
    for name in _WRITE_METHODS:
        original = getattr(client, name, None)
        if original is None or getattr(original, "_rate_limited", False):
            continue

        def make(orig, label):
            async def gated(*args, **kwargs):
                return await _gate.run(lambda: orig(*args, **kwargs), label=label)
            gated._rate_limited = True  # type: ignore[attr-defined]
            return gated

        try:
            setattr(client, name, make(original, name))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not install rate limit on %s: %s", name, exc)
    log.info("Outbound rate limiting installed (min interval %.1fs).",
             _gate._min_interval)
    return client
