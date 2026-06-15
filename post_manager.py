"""Telegram post synchronisation.

Renders a media's card into Telegram messages and keeps them in sync. The
database is the source of truth; Telegram is only the rendered view.

Layout guarantees (these are exactly the bugs this module fixes):

  * The poster is a **real photo** sent as the root message with the card as its
    caption, so the image is always on top and the title/info follow it — never
    a bare link rendered as a preview *below* the text.
  * Overflow (when the card is longer than one message) goes into linked reply
    posts that are sent **after** the root, in order, so they can never appear
    before the main post.
  * Every chunk is rendered with the custom collapsible-HTML parse mode, so the
    overview blockquote is shown collapsed and all inline ``<a>`` links work.
  * Posts are edited in place; an unchanged chunk (same content hash) is skipped.
  * Telegram forbids editing a text message into a media message (and vice
    versa). The root records whether it is a photo (``has_media``); when that
    intent flips we delete and recreate the whole thread cleanly.
"""
from __future__ import annotations

from typing import Optional

from ..config import settings
from ..logging_setup import get_logger
from ..storage.models import Media, Post, PostState
from ..storage.repositories import MediaRepository, PostRepository
from ..telegram.formatting import HTML
from ..util import content_hash
from . import templates as T

log = get_logger("ui.post_manager")


class PostManager:
    async def sync(self, client, media: Media, blocks) -> None:
        target_chat = settings.target_chat_id
        topic_id = settings.target_topic_id or None

        want_media = bool(media.poster_url)
        first_limit = settings.tg_caption_limit if want_media else settings.tg_message_limit
        rest_limit = settings.tg_message_limit

        lines = self._flatten(blocks)
        chunks = self._pack(media, lines, first_limit, rest_limit)

        existing = await PostRepository.list_for_media(media._id)
        existing_by_part = {p.part_index: p for p in existing}
        root = existing_by_part.get(0)

        # Telegram cannot convert a text message <-> photo message in place.
        # If the root's media intent flipped, rebuild the whole thread.
        if root is not None and bool(root.has_media) != want_media:
            log.info(
                "Root media intent changed (had_media=%s want_media=%s) -> rebuild %s",
                root.has_media, want_media, media._id,
            )
            await self._delete_all(client, target_chat, existing)
            existing = []
            existing_by_part = {}
            root = None

        first_time = not existing

        # ---- Root (part 0) ------------------------------------------------- #
        root_text = chunks[0] if chunks else T.title_line(media)
        root_digest = content_hash(root_text)

        if root is None:
            root_msg_id, used_media = await self._send_root(
                client, target_chat, topic_id, media, root_text, want_media
            )
            root = Post(
                media_id=media._id,
                chat_id=target_chat,
                topic_id=topic_id,
                message_id=root_msg_id,
                role="root",
                part_index=0,
                parent_post_id="",
                state=PostState.CREATED.value,
                content_hash=root_digest,
                char_len=T.visible_len(root_text),
                has_media=want_media,  # intent, even if the photo send fell back
            )
            await PostRepository.insert(root)
            await MediaRepository.set_root_post(media._id, root._id)
        else:
            if root.content_hash != root_digest:
                await self._edit(client, target_chat, root.message_id, root_text)
                root.content_hash = root_digest
                root.char_len = T.visible_len(root_text)
                root.state = PostState.UPDATED.value
                await PostRepository.update(root)

        root_msg_id = root.message_id
        root_post_id = root._id

        # ---- Overflow (parts 1..n), strictly after the root, in order ------ #
        for index in range(1, len(chunks)):
            text = chunks[index]
            digest = content_hash(text)
            post = existing_by_part.get(index)

            if post is None:
                msg_id = await self._send_overflow(client, target_chat, root_msg_id, text)
                post = Post(
                    media_id=media._id,
                    chat_id=target_chat,
                    topic_id=topic_id,
                    message_id=msg_id,
                    role="overflow",
                    part_index=index,
                    parent_post_id=root_post_id,
                    state=PostState.CREATED.value if first_time else PostState.SPLIT.value,
                    content_hash=digest,
                    char_len=T.visible_len(text),
                    has_media=False,
                )
                await PostRepository.insert(post)
            elif post.content_hash != digest:
                await self._edit(client, target_chat, post.message_id, text)
                post.content_hash = digest
                post.char_len = T.visible_len(text)
                post.state = PostState.UPDATED.value
                await PostRepository.update(post)

        # ---- Remove now-surplus overflow posts (content shrank) ------------ #
        for post in existing:
            if post.part_index >= len(chunks):
                try:
                    await client.delete_messages(target_chat, post.message_id)
                except Exception as exc:  # pragma: no cover - telegram runtime
                    log.warning("Delete failed for post %s: %s", post.message_id, exc)
                await PostRepository.delete(post._id)

    # --------------------------------------------------------------------- #
    # Telegram send / edit helpers
    # --------------------------------------------------------------------- #
    async def _send_root(
        self, client, chat, topic_id, media: Media, text: str, want_media: bool
    ) -> tuple[int, bool]:
        """Send the root message. Returns ``(message_id, sent_as_photo)``."""
        if want_media:
            try:
                msg = await client.send_file(
                    chat,
                    file=media.poster_url,
                    caption=text,
                    parse_mode=HTML,
                    reply_to=topic_id,
                )
                return msg.id, True
            except Exception as exc:  # pragma: no cover - telegram runtime
                log.warning(
                    "Photo send failed for %s (%s) - falling back to text",
                    media._id, exc,
                )
                msg = await client.send_message(
                    chat, text, parse_mode=HTML, reply_to=topic_id, link_preview=False
                )
                # Intent stays "media" so we do not thrash rebuilds on retry.
                return msg.id, False

        msg = await client.send_message(
            chat, text, parse_mode=HTML, reply_to=topic_id, link_preview=False
        )
        return msg.id, False

    async def _send_overflow(self, client, chat, root_msg_id, text: str) -> int:
        msg = await client.send_message(
            chat, text, parse_mode=HTML, reply_to=root_msg_id, link_preview=False
        )
        return msg.id

    async def _edit(self, client, chat, message_id, text: str) -> None:
        # For a photo root this edits the caption; for a text post it edits the
        # body. Both go through the same Telethon call.
        try:
            await client.edit_message(
                chat, message_id, text, parse_mode=HTML, link_preview=False
            )
        except Exception as exc:  # pragma: no cover - telegram runtime
            log.warning("Edit failed for post %s: %s", message_id, exc)

    async def _delete_all(self, client, chat, posts) -> None:
        for post in posts:
            try:
                await client.delete_messages(chat, post.message_id)
            except Exception as exc:  # pragma: no cover - telegram runtime
                log.warning("Delete failed for post %s: %s", post.message_id, exc)
            await PostRepository.delete(post._id)

    # --------------------------------------------------------------------- #
    # Chunking (line based, measured in *visible* UTF-16 units)
    # --------------------------------------------------------------------- #
    @staticmethod
    def _flatten(blocks) -> list[str]:
        """Blocks -> physical lines (blank line between blocks)."""
        text = "\n\n".join(b.text for b in blocks if b.text)
        return text.split("\n")

    def _pack(self, media: Media, lines: list[str], first_limit: int, rest_limit: int) -> list[str]:
        """Greedily pack lines into chunks without ever splitting a line.

        Chunk 0 is the root (caption or text). Chunks >= 1 are overflow posts and
        each carries an ``overflow_header`` (repeated title) whose length is
        reserved against that chunk's budget. All card lines are pre-clamped
        short, so no single line exceeds a budget and no tag is ever split.
        """
        chunks: list[str] = []
        cur: list[str] = []
        cur_len = 0

        def limit_for(idx: int) -> int:
            return first_limit if idx == 0 else rest_limit

        def header_for(idx: int) -> str:
            return "" if idx == 0 else T.overflow_header(media, idx)

        def budget_for(idx: int) -> int:
            header = header_for(idx)
            reserve = (T.visible_len(header) + 1) if header else 0
            return max(64, limit_for(idx) - reserve)

        def flush() -> None:
            nonlocal cur, cur_len
            ls = list(cur)
            while ls and not ls[0].strip():
                ls.pop(0)
            while ls and not ls[-1].strip():
                ls.pop()
            body = "\n".join(ls)
            idx = len(chunks)
            header = header_for(idx)
            if header:
                chunks.append(f"{header}\n{body}" if body else header)
            else:
                chunks.append(body)
            cur = []
            cur_len = 0

        for raw in lines:
            idx = len(chunks)
            budget = budget_for(idx)
            line = self._safe_clamp_line(raw, budget)
            line_len = T.visible_len(line)
            add = line_len + (1 if cur else 0)
            if cur and cur_len + add > budget:
                flush()
                # Recompute against the *new* chunk index (overflow header).
                budget = budget_for(len(chunks))
                line = self._safe_clamp_line(raw, budget)
                cur = [line]
                cur_len = T.visible_len(line)
            else:
                cur.append(line)
                cur_len += add

        if cur or not chunks:
            flush()
        return chunks

    @staticmethod
    def _safe_clamp_line(line: str, limit: int) -> str:
        """Defensive: clamp only *plain* lines (no markup) that exceed the limit.

        Real card lines are already clamped in the templates, and any line
        containing ``<`` may carry a tag we must not cut, so those are left
        untouched.
        """
        if "<" in line:
            return line
        if T.visible_len(line) <= limit:
            return line
        return T.clamp(line, limit)
