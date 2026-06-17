"""Telegram post synchronisation.

Renders a media's card into Telegram messages and keeps them in sync:

  * the root post shows the poster as a real photo with the card as its caption,
    so the image sits ON TOP and the title follows underneath; when no poster is
    known the root is a plain text message instead;
  * overflow content goes into linked text posts that are sent AFTER the root
    and AS REPLIES TO IT, so the whole set stays threaded together and the
    description (always on the root) is reachable from any overflow part; each
    overflow part also repeats the media title;
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
            # The root post lands in the target topic; every overflow part is sent
            # as a reply to the root post, so the whole set stays threaded together
            # and the description (always on the root) is one tap away. Telegram
            # keeps a reply inside the same topic as the message it replies to.
            # (root_msg_id is set while processing index 0, which always precedes
            # the overflow parts in this ordered loop.)
            reply_to = topic_id if is_root else (root_msg_id or topic_id)

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
        header_reserve = T.visible_len(T.overflow_header(media, 999)) + 1
        # The first chunk becomes a photo caption when a poster exists, so it is
        # bound by the (smaller) caption limit; otherwise the full message limit.
        first_limit = caption_limit if media.poster_url else message_limit

        chunks = _chunk_units(full_text, first_limit, message_limit, header_reserve)
        texts: list[str] = []
        for index, body in enumerate(chunks):
            if index == 0:
                texts.append(body)
            else:
                texts.append(f"{T.overflow_header(media, index)}\n{body}")
        return texts


# --------------------------------------------------------------------------- #
# Splitting helpers (visible-length aware, blockquote-atomic)
# --------------------------------------------------------------------------- #
def _atomic_units(text: str) -> list[str]:
    """Split text into units that must never be broken across posts.

    A blockquote spanning multiple physical lines is ONE unit, so a season's
    collapsed episode list stays together with its header on the same post.
    Every other physical line is its own unit.
    """
    units: list[str] = []
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if "<blockquote" in line and "</blockquote>" not in line:
            buf = [line]
            i += 1
            while i < n and "</blockquote>" not in lines[i]:
                buf.append(lines[i])
                i += 1
            if i < n:
                buf.append(lines[i])
                i += 1
            units.append("\n".join(buf))
        else:
            units.append(line)
            i += 1
    return units


def _chunk_units(full_text: str, first_limit: int, message_limit: int,
                 header_reserve: int) -> list[str]:
    """Pack atomic units into posts by VISIBLE length.

    Telegram counts only visible text against its limit; link URLs and tags do
    not count. Measuring visible length (not raw HTML) is what lets a post hold
    far more linked episodes than the old byte-based estimate allowed.
    """
    overflow_budget = max(256, message_limit - header_reserve - 1)
    units = _atomic_units(full_text)

    # Hard-wrap a plain (tag-free) unit that alone exceeds the budget; tagged
    # units (links / blockquotes) are kept intact and are bounded by the card.
    prepared: list[str] = []
    for u in units:
        if "<" not in u and T.visible_len(u) > overflow_budget:
            s = u
            while T.visible_len(s) > overflow_budget:
                prepared.append(s[:overflow_budget])
                s = s[overflow_budget:]
            prepared.append(s)
        else:
            prepared.append(u)

    chunks: list[str] = []
    cur = ""
    cur_vis = 0
    limit = first_limit
    for u in prepared:
        uvis = T.visible_len(u)
        if not cur:
            cur, cur_vis = u, uvis
            continue
        if cur_vis + 1 + uvis <= limit:
            cur = f"{cur}\n{u}"
            cur_vis += 1 + uvis
        else:
            chunks.append(cur)
            cur, cur_vis = u, uvis
            limit = overflow_budget
    if cur or not chunks:
        chunks.append(cur)
    return chunks
