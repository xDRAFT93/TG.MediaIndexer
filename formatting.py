"""HTML parse mode that renders blockquotes as *collapsed* (expandable) quotes.

Telethon's built-in HTML parser understands ``<blockquote>`` but always emits a
normal :class:`MessageEntityBlockquote` — it never reads a ``collapsed`` /
``expandable`` attribute. Telegram, however, supports a collapsed blockquote
(the "show more" quote that saves vertical space). To get it we delegate parsing
to Telethon's own parser (which correctly handles UTF-16 offsets and every other
tag) and then flip ``collapsed = True`` on each blockquote entity afterwards.

The object is passed as ``parse_mode=HTML`` to ``send_message`` / ``send_file`` /
``edit_message``. If Telethon is unavailable for some reason we fall back to the
plain ``"html"`` string mode so posting still works (just without collapsing).
"""
from __future__ import annotations

try:  # pragma: no cover - exercised only with Telethon installed
    from telethon.extensions import html as _tl_html
    from telethon.tl.types import MessageEntityBlockquote

    class _CollapsibleHtml:
        """A Telethon parse-mode (``parse`` / ``unparse``) with collapsed quotes."""

        @staticmethod
        def parse(text: str):
            text, entities = _tl_html.parse(text)
            for ent in entities or []:
                if isinstance(ent, MessageEntityBlockquote):
                    # ``collapsed`` exists on the modern TL layer; setting the
                    # attribute is harmless even if an older layer ignores it.
                    try:
                        ent.collapsed = True
                    except Exception:
                        pass
            return text, entities

        @staticmethod
        def unparse(text: str, entities):
            return _tl_html.unparse(text, entities)

    HTML = _CollapsibleHtml()
except Exception:  # pragma: no cover - Telethon missing (e.g. during tests)
    HTML = "html"
