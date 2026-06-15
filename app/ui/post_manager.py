"""Telegram post synchronisation.

Renders a media's card into Telegram messages and keeps them in sync:

  * the root post shows the poster as a real photo with the card as its caption,
    so the image sits ON TOP and the title follows underneath; when no poster is
    known the root is a plain text message instead;
  * overflow content goes into linked text posts that are sent AFTER the root
    (so ordering is always root-first) and each repeats the media title;
  * all parts are posted into the configured target topic, in order;
  * posts are edited in place, not recreated; an unchanged chunk (same content
    hash) is skipped entirely, so updates are incremental;
  * link previews are disabled on text posts so the inline source/provider links
    don't each spawn a preview;
  * a Post state machine records CREATED / UPDATED / SPLIT / ARCHIVED;
  * Telegram's length limits are handled by splitting on line boundaries — the
    first chunk respects the (smaller) caption limit when a photo is attached.

The database is the source of truth; Telegram is only the rendered view.
"""
from __future__ import annotations

from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import Media, Post, PostState
from ..storage.repositories import MediaRepository, PostRepository
from ..telegram.ratelimit import safe_call
from ..util import content_hash
from . import templates as T

log = get_logger("ui.post_manager")


class PostManager:
    async def sync(self, client, media: Media, full_text: str) -> None:
        texts = self._build_texts(media, full_text)
        target_chat = settings.target_chat_id
        topic_id = settings.target_topic_id or None
        has_poster = bool(media.poster_url)

        existing = await PostRepository.list_for_media(media._id)
        existing_by_part = {p.part_index: p for p in existing}

        # If the root's media-ness changed (e.g. a poster was only found after a
        # text root had already been posted), rebuild all posts so the photo
        # lands on top and ordering stays root-first.
        root_post = existing_by_part.get(0)
        if root_post is not None and bool(root_post.has_media) != has_poster:
            for p in existing:
                await self._safe_delete(client, target_chat, p)
            existing, existing_by_part = [], {}

        first_time = not existing_by_part
        root_msg_id: Optional[int] = None
        root_post_id: str = ""

        for index, text in enumerate(texts):
            digest = content_hash(text)
            post = existing_by_part.get(index)
            is_root = index == 0
            reply_to = topic_id  # every part is sent into the target topic, in order

            if post is None:
                msg, used_photo = await self._send_part(
                    client, target_chat, reply_to, text,
                    as_photo=is_root and has_poster,
                    poster_url=media.poster_url,
                )
                state = PostState.CREATED.value if first_time else PostState.SPLIT.value
                new_post = Post(
                    media_id=media._id,
                    chat_id=target_chat,
                    topic_id=topic_id,
                    message_id=msg.id,
                    role="root" if is_root else "overflow",
                    part_index=index,
                    parent_post_id="" if is_root else root_post_id,
                    has_media=used_photo,
                    state=state,
                    content_hash=digest,
                    char_len=len(text),
                )
                await PostRepository.insert(new_post)
                if is_root:
                    root_msg_id = msg.id
                    root_post_id = new_post._id
                    await MediaRepository.set_root_post(media._id, new_post._id)
            else:
                if post.content_hash != digest:
                    try:
                        await safe_call(
                            lambda: client.edit_message(
                                target_chat, post.message_id, text,
                                parse_mode="html", link_preview=False,
                            ),
                            what="edit_message",
                        )
                    except Exception as exc:  # pragma: no cover - telegram runtime
                        log.warning("Edit failed for post %s: %s", post.message_id, exc)
                    post.content_hash = digest
                    post.char_len = len(text)
                    post.state = PostState.UPDATED.value
                    await PostRepository.update(post)
                if is_root:
                    root_msg_id = post.message_id
                    root_post_id = post._id

        # Remove now-surplus overflow posts (content shrank): delete + forget.
        for post in existing:
            if post.part_index >= len(texts):
                await self._safe_delete(client, target_chat, post)

    # --------------------------------------------------------------------- #
    async def _send_part(self, client, chat, reply_to, text: str,
                         as_photo: bool, poster_url: str):
        """Send one part. Returns (message, used_photo).

        The root is sent as a poster photo with the text as caption (image on
        top). If that fails — or there is no poster — it falls back to a plain
        text message, and the returned flag reflects what actually happened.
        """
        if as_photo and poster_url:
            try:
                msg = await safe_call(
                    lambda: client.send_file(
                        chat, poster_url, caption=text,
                        parse_mode="html", reply_to=reply_to,
                    ),
                    what="send_file",
                )
                if msg is not None:
                    return msg, True
            except Exception as exc:  # pragma: no cover - telegram runtime
                log.warning("Poster photo send failed (%s); falling back to text.", exc)
        msg = await safe_call(
            lambda: client.send_message(
                chat, text, parse_mode="html", reply_to=reply_to, link_preview=False,
            ),
            what="send_message",
        )
        return msg, False

    async def _safe_delete(self, client, chat, post: Post) -> None:
        try:
            await safe_call(
                lambda: client.delete_messages(chat, post.message_id),
                what="delete_messages",
            )
        except Exception as exc:  # pragma: no cover - telegram runtime
            log.warning("Delete failed for post %s: %s", post.message_id, exc)
        await PostRepository.delete(post._id)

    # --------------------------------------------------------------------- #
    def _build_texts(self, media: Media, full_text: str) -> list[str]:
        message_limit = settings.tg_message_limit
        caption_limit = settings.tg_caption_limit
        header_reserve = len(T.overflow_header(media, 999)) + 1
        # The first chunk becomes a photo caption when a poster exists, so it is
        # bound by the (smaller) caption limit; otherwise the full message limit.
        first_limit = caption_limit if media.poster_url else message_limit

        chunks = self._chunk_lines(full_text, first_limit, message_limit, header_reserve)
        texts: list[str] = []
        for index, body in enumerate(chunks):
            if index == 0:
                texts.append(body)
            else:
                texts.append(f"{T.overflow_header(media, index)}\n{body}")
        return texts

    @staticmethod
    def _chunk_lines(full_text: str, first_limit: int, message_limit: int,
                     header_reserve: int) -> list[str]:
        overflow_budget = max(256, message_limit - header_reserve - 1)
        wrap_limit = min(first_limit, overflow_budget)

        # Hard-wrap only plain (tag-free) lines; a line containing an HTML tag
        # (a link entry or the capped blockquote) is never split, since that
        # would corrupt the markup. Such lines are short by construction.
        wrapped: list[str] = []
        for line in full_text.split("\n"):
            if "<" in line or len(line) <= wrap_limit:
                wrapped.append(line)
                continue
            while len(line) > wrap_limit:
                wrapped.append(line[:wrap_limit])
                line = line[wrap_limit:]
            wrapped.append(line)

        chunks: list[str] = []
        cur = ""
        limit = first_limit
        for line in wrapped:
            if not cur:
                cur = line
                continue
            candidate = f"{cur}\n{line}"
            if len(candidate) <= limit:
                cur = candidate
            else:
                chunks.append(cur)
                cur = line
                limit = overflow_budget
        if cur or not chunks:
            chunks.append(cur)
        return chunks
