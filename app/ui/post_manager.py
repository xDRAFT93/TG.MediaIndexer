"""Telegram post synchronisation.

Renders a media's card into Telegram messages and keeps them in sync:

  * exactly one root post per media (its message id never changes);
  * overflow content goes into linked reply posts, each of which repeats the
    media title (the title is never missing from a sub-post);
  * posts are edited in place, not recreated; an unchanged chunk (same content
    hash) is skipped entirely, so updates are incremental;
  * a Post state machine records CREATED / UPDATED / SPLIT / ARCHIVED;
  * Telegram's length limit is handled by splitting at line boundaries.

The database is the source of truth; Telegram is only the rendered view.
"""
from __future__ import annotations

from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import Media, Post, PostState
from ..storage.repositories import MediaRepository, PostRepository
from ..util import content_hash
from . import templates as T

log = get_logger("ui.post_manager")


class PostManager:
    async def sync(self, client, media: Media, full_text: str) -> None:
        texts = self._build_texts(media, full_text)
        existing = await PostRepository.list_for_media(media._id)
        existing_by_part = {p.part_index: p for p in existing}
        first_time = not existing

        target_chat = settings.target_chat_id
        topic_id = settings.target_topic_id or None

        root_msg_id: Optional[int] = None
        root_post_id: str = ""

        for index, text in enumerate(texts):
            digest = content_hash(text)
            post = existing_by_part.get(index)
            use_preview = index == 0 and bool(media.poster_url)
            reply_to = topic_id if index == 0 else root_msg_id

            if post is None:
                msg = await client.send_message(
                    target_chat, text,
                    parse_mode="html",
                    reply_to=reply_to,
                    link_preview=use_preview,
                )
                state = PostState.CREATED.value if first_time else PostState.SPLIT.value
                new_post = Post(
                    media_id=media._id,
                    chat_id=target_chat,
                    topic_id=topic_id,
                    message_id=msg.id,
                    role="root" if index == 0 else "overflow",
                    part_index=index,
                    parent_post_id="" if index == 0 else root_post_id,
                    state=state,
                    content_hash=digest,
                    char_len=len(text),
                )
                await PostRepository.insert(new_post)
                if index == 0:
                    root_msg_id = msg.id
                    root_post_id = new_post._id
                    await MediaRepository.set_root_post(media._id, new_post._id)
            else:
                if post.content_hash != digest:
                    try:
                        await client.edit_message(
                            target_chat, post.message_id, text,
                            parse_mode="html",
                            link_preview=use_preview,
                        )
                    except Exception as exc:  # pragma: no cover - telegram runtime
                        log.warning("Edit failed for post %s: %s", post.message_id, exc)
                    post.content_hash = digest
                    post.char_len = len(text)
                    post.state = PostState.UPDATED.value
                    await PostRepository.update(post)
                if index == 0:
                    root_msg_id = post.message_id
                    root_post_id = post._id

        # Remove now-surplus overflow posts (content shrank): archive + delete.
        for post in existing:
            if post.part_index >= len(texts):
                try:
                    await client.delete_messages(target_chat, post.message_id)
                except Exception as exc:  # pragma: no cover - telegram runtime
                    log.warning("Delete failed for post %s: %s", post.message_id, exc)
                await PostRepository.delete(post._id)

    # --------------------------------------------------------------------- #
    def _build_texts(self, media: Media, full_text: str) -> list[str]:
        limit = settings.tg_message_limit
        header_reserve = len(T.overflow_header(media, 999)) + 1
        prefix = ""
        if media.poster_url:
            # Hidden link so Telegram shows the poster as a preview above the
            # card without using the 1024-char caption limit.
            prefix = f'<a href="{T.esc(media.poster_url)}">\u200b</a>'
        base = max(256, limit - max(header_reserve, len(prefix)) - 1)

        chunks = self._chunk_lines(full_text, base)
        texts: list[str] = []
        for index, body in enumerate(chunks):
            if index == 0:
                texts.append(f"{prefix}{body}" if prefix else body)
            else:
                texts.append(f"{T.overflow_header(media, index)}\n{body}")
        return texts

    @staticmethod
    def _chunk_lines(full_text: str, base: int) -> list[str]:
        # Hard-wrap any single overlong line, then greedily pack lines.
        wrapped: list[str] = []
        for line in full_text.split("\n"):
            while len(line) > base:
                wrapped.append(line[:base])
                line = line[base:]
            wrapped.append(line)

        chunks: list[str] = []
        cur = ""
        for line in wrapped:
            candidate = line if not cur else f"{cur}\n{line}"
            if len(candidate) <= base:
                cur = candidate
            else:
                chunks.append(cur)
                cur = line
        if cur or not chunks:
            chunks.append(cur)
        return chunks
