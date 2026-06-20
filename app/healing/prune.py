"""On-demand dead-link pruning (``.prune``).

Source posts in the origin threads get deleted over time. When that happens the
catalog entry in the target topic still links to a message that no longer
exists. This module walks the whole catalog on demand, asks Telegram which of
the referenced source messages still exist, and removes the dead references:

  * dead releases are dropped from films and episodes;
  * an episode with no surviving release is deleted;
  * a media entry with no surviving source, film release or episode is deleted
    together with its target post(s).

Safety: anything that cannot be positively confirmed as deleted is KEPT. A
reference without a checkable message id, or one whose existence check errored
(network / permission), is treated as "unknown" and never pruned, so a transient
failure can never wipe a live entry.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..logging_setup import get_logger
from ..storage.repositories import (
    EpisodeRepository,
    MediaRepository,
    PostRepository,
)

log = get_logger("healing.prune")


def _msg_id(ref: dict) -> Optional[int]:
    """The source message id a source/release points at, if any."""
    return ref.get("message_id") or ref.get("first_message_id") or ref.get("last_message_id")


def _is_dead(ref: dict, live: set, checked: set) -> bool:
    """A reference is dead only if it has a checkable id that was checked and is
    NOT live. Unverifiable or unchecked references are kept."""
    mid = _msg_id(ref)
    if not mid:
        return False
    key = (ref.get("chat_id"), mid)
    if key not in checked:
        return False
    return key not in live


def collect_pairs(sources: list[dict], film_releases: list[dict],
                  episodes: Iterable[tuple]) -> set:
    """All (chat_id, message_id) pairs referenced by a media, for existence
    checking. ``episodes`` is an iterable of (episode_id, releases)."""
    pairs: set = set()
    for ref in list(sources) + list(film_releases):
        mid = _msg_id(ref)
        if mid:
            pairs.add((ref.get("chat_id"), mid))
    for _eid, rels in episodes:
        for r in rels:
            mid = _msg_id(r)
            if mid:
                pairs.add((r.get("chat_id"), mid))
    return pairs


def plan_prune(sources: list[dict], film_releases: list[dict],
               episodes: list[tuple], live: set, checked: set) -> dict:
    """Pure planner. Given the set of live and the set of actually-checked
    (chat_id, message_id) pairs, decide what survives.

    Returns a dict with the surviving ``sources`` / ``film_releases``, a map of
    surviving ``episode_releases`` {episode_id: [release, ...]}, the set of
    ``delete_episodes`` ids, and an ``empty`` flag (nothing live remains)."""
    keep_src = [s for s in sources if not _is_dead(s, live, checked)]
    keep_film = [r for r in film_releases if not _is_dead(r, live, checked)]

    ep_releases: dict = {}
    delete_eps: set = set()
    live_episode_count = 0
    for eid, rels in episodes:
        kept = [r for r in rels if not _is_dead(r, live, checked)]
        had_release = bool(rels)
        if kept:
            if len(kept) != len(rels):
                ep_releases[eid] = kept
            live_episode_count += 1
        elif had_release:
            # Every release was confirmed dead -> the episode is gone.
            delete_eps.add(eid)
        else:
            # Episode never had a release (provider placeholder); keep as-is.
            live_episode_count += 1

    empty = not keep_src and not keep_film and live_episode_count == 0
    changed = (
        len(keep_src) != len(sources)
        or len(keep_film) != len(film_releases)
        or bool(ep_releases)
        or bool(delete_eps)
    )
    return {
        "sources": keep_src,
        "film_releases": keep_film,
        "episode_releases": ep_releases,
        "delete_episodes": delete_eps,
        "empty": empty,
        "changed": changed,
    }


async def _live_pairs(client, pairs: set) -> tuple[set, set]:
    """Ask Telegram which (chat_id, msg_id) pairs still exist.

    Returns (live, checked). Only pairs we could actually query land in
    ``checked``; a chat whose lookup raises is left out entirely so nothing in it
    is pruned on a transient error.
    """
    live: set = set()
    checked: set = set()
    by_chat: dict = {}
    for chat_id, mid in pairs:
        by_chat.setdefault(chat_id, []).append(mid)
    for chat_id, ids in by_chat.items():
        try:
            msgs = await client.get_messages(chat_id, ids=ids)
        except Exception as exc:  # pragma: no cover - network/permission
            log.warning("prune: could not check chat %s (%s) - keeping all.", chat_id, exc)
            continue
        for mid, msg in zip(ids, msgs):
            checked.add((chat_id, mid))
            if msg is not None:
                live.add((chat_id, mid))
    return live, checked


async def run_prune(client, enqueue) -> dict:
    """Walk the whole catalog, drop dead links, delete emptied entries.

    ``enqueue`` is an async callable (e.g. ``update_queue.put``) awaited with
    each surviving-but-changed media id so the update worker re-renders it; using
    the awaitable ``put`` applies backpressure on a bounded queue instead of
    raising ``QueueFull``.
    """
    summary = {"checked": 0, "media_pruned": 0, "media_deleted": 0,
               "episodes_deleted": 0, "releases_removed": 0}
    media_ids = await MediaRepository.all_ids()
    summary["checked"] = len(media_ids)

    for media_id in media_ids:
        media = await MediaRepository.get(media_id)
        if media is None:
            continue
        episodes = await EpisodeRepository.list_for_media(media_id)
        src = [s if isinstance(s, dict) else s.to_dict() for s in media.sources]
        film = [r if isinstance(r, dict) else r.to_dict() for r in media.releases]
        ep_pairs = [(e._id, [r if isinstance(r, dict) else r.to_dict()
                             for r in e.releases]) for e in episodes]

        pairs = collect_pairs(src, film, ep_pairs)
        if not pairs:
            continue
        live, checked = await _live_pairs(client, pairs)
        if not checked:
            continue  # nothing could be verified -> leave this entry alone

        plan = plan_prune(src, film, ep_pairs, live, checked)
        if not plan["changed"] and not plan["empty"]:
            continue

        # Count removed releases for the summary.
        removed = (len(src) - len(plan["sources"])) + (len(film) - len(plan["film_releases"]))
        for eid, rels in ep_pairs:
            if eid in plan["episode_releases"]:
                removed += len(rels) - len(plan["episode_releases"][eid])
        summary["releases_removed"] += max(0, removed)

        if plan["empty"]:
            await _delete_media_fully(client, media_id, episodes)
            summary["media_deleted"] += 1
            continue

        # Apply surviving data.
        await MediaRepository.set_sources(media_id, plan["sources"])
        await MediaRepository.set_film_releases(media_id, plan["film_releases"])
        for eid, rels in plan["episode_releases"].items():
            await EpisodeRepository.set_releases(eid, rels)
        for eid in plan["delete_episodes"]:
            await EpisodeRepository.delete(eid)
            summary["episodes_deleted"] += 1
        summary["media_pruned"] += 1
        await enqueue(media_id)

    log.info("Prune done: %s", summary)
    return summary


async def _delete_media_fully(client, media_id: str, episodes: list) -> None:
    """Remove a media everywhere: its target posts, its episodes, the record."""
    posts = await PostRepository.list_for_media(media_id)
    for post in posts:
        try:
            await client.delete_messages(post.chat_id, post.message_id)
        except Exception as exc:  # pragma: no cover - network/permission
            log.warning("prune: failed to delete post %s: %s", post.message_id, exc)
        try:
            await PostRepository.delete(post._id)
        except Exception:
            pass
    for e in episodes:
        try:
            await EpisodeRepository.delete(e._id)
        except Exception:
            pass
    await MediaRepository.delete(media_id)
