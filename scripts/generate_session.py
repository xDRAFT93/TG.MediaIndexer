"""Generate a Telegram StringSession for the userbot.

Run this once on a machine where you can receive the Telegram login code:

    python scripts/generate_session.py

It prompts for your API id/hash (from https://my.telegram.org -> API
development tools) and your phone number, performs the login, and prints a
StringSession. Copy that value into TG_SESSION in your .env. The session is a
credential - keep it secret; anyone with it can act as your account.
"""
from __future__ import annotations

import os

from telethon import TelegramClient
from telethon.sessions import StringSession


def _prompt(label: str, env: str) -> str:
    val = os.getenv(env)
    if val:
        return val
    return input(f"{label}: ").strip()


def main() -> None:
    api_id = int(_prompt("API ID", "TG_API_ID"))
    api_hash = _prompt("API HASH", "TG_API_HASH")

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session = client.session.save()
        me = client.get_me()
        who = getattr(me, "username", None) or me.id
        print("\nLogged in as:", who)
        print("\nYour TG_SESSION (copy into .env):\n")
        print(session)
        print("\nKeep this secret.")


if __name__ == "__main__":
    main()
