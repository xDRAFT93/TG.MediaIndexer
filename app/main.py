"""MediaIndexer entrypoint.

Wires everything together and runs the userbot until disconnected:

    validate config -> connect DB -> connect Telegram (must already be
    authorised) -> register handlers -> start pipeline workers + healer ->
    resume in-flight work -> run forever -> graceful shutdown.
"""
from __future__ import annotations

import asyncio

from .config import settings
from .logging_setup import get_logger, setup_logging
from .healing.self_heal import start_healer
from .pipeline.queues import resume_pending
from .pipeline.workers import start_workers
from .providers.registry import ProviderRegistry
from .storage import database
from .telegram.client import build_client
from .telegram.handlers import register_handlers


async def amain() -> int:
    setup_logging()
    log = get_logger("main")

    problems = settings.validate()
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        log.error("Refusing to start. Fix your .env (see .env.example).")
        return 1

    await database.connect()
    log.info("Connected to MongoDB '%s'.", settings.mongo_db)

    registry = ProviderRegistry()
    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        log.error(
            "Telegram session is not authorised. Generate a valid TG_SESSION "
            "with scripts/generate_session.py and put it in your .env."
        )
        await client.disconnect()
        await registry.aclose()
        await database.close()
        return 1

    register_handlers(client)
    workers = start_workers(client, registry)
    healer = start_healer(registry)
    await resume_pending()

    me = await client.get_me()
    who = getattr(me, "username", None) or getattr(me, "first_name", None) or me.id
    log.info("MediaIndexer is running as %s (id=%s).", who, me.id)

    try:
        await client.run_until_disconnected()
    finally:
        log.info("Shutting down ...")
        for task in (*workers, healer):
            task.cancel()
        await asyncio.gather(*workers, healer, return_exceptions=True)
        await registry.aclose()
        await database.close()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
