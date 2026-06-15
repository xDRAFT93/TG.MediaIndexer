"""Telegram message -> structured RawEvent.

Hard rules enforced here:
  * ``file_name`` comes EXCLUSIVELY from the document/video filename attribute
    (Telegram raw field). Never from text, caption, OCR or interpretation.
  * At most ``MAX_CONTENT_LINES`` lines of textual content are kept.
  * Messages from bots are flagged so they can be ignored downstream.
"""
from __future__ import annotations

from typing import Optional

from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from ..config import settings
from ..storage.models import RawEvent
from ..util import first_n_lines


def _extract_file_name(message) -> str:
    """Return message.document/video file_name only (raw, unmodified)."""
    doc = getattr(message, "document", None)
    if doc is not None:
        for attr in getattr(doc, "attributes", []) or []:
            if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
                return attr.file_name
    # Telethon's high-level accessor resolves the same DocumentAttributeFilename.
    file = getattr(message, "file", None)
    if file is not None and getattr(file, "name", None):
        return file.name
    return ""


def _media_kind(message) -> str:
    media = getattr(message, "media", None)
    if media is None:
        return "none"
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = getattr(message, "document", None)
        if doc is not None:
            for attr in getattr(doc, "attributes", []) or []:
                if isinstance(attr, DocumentAttributeVideo):
                    return "video"
        return "document"
    return media.__class__.__name__


def _thread_id(message) -> int:
    reply = getattr(message, "reply_to", None)
    if reply is not None:
        top = getattr(reply, "reply_to_top_id", None)
        if top:
            return top
        rid = getattr(reply, "reply_to_msg_id", None)
        if rid:
            return rid
    # No forum-topic / reply context -> single default "General" thread.
    return 0


async def _is_bot(message) -> bool:
    try:
        sender = await message.get_sender()
    except Exception:
        sender = None
    return bool(getattr(sender, "bot", False))


async def parse_message(message) -> RawEvent:
    file_name = _extract_file_name(message)
    media_kind = _media_kind(message)
    has_media = media_kind != "none"

    raw_text = message.message or ""
    # Telegram has a single text field; when media is attached it acts as the
    # caption. Split into the two conceptual fields without double-counting.
    if has_media:
        caption = first_n_lines(raw_text, settings.max_lines)
        message_text = ""
    else:
        caption = ""
        message_text = first_n_lines(raw_text, settings.max_lines)

    file = getattr(message, "file", None)
    mime = getattr(file, "mime_type", "") or "" if file is not None else ""
    size = getattr(file, "size", None) if file is not None else None

    return RawEvent(
        chat_id=message.chat_id,
        message_id=message.id,
        thread_id=_thread_id(message),
        message_text=message_text,
        caption=caption,
        file_name=file_name,
        media_type_raw=media_kind,
        mime_type=mime,
        size_bytes=size,
        sender_id=message.sender_id,
        is_bot=await _is_bot(message),
        timestamp=message.date,
    )
