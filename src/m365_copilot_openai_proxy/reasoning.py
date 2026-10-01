"""Request-scoped, typed summaries; never part of answer text or tool syntax."""
from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    item_id: str
    text: str


StreamChunk = str | ReasoningDelta


def is_reasoning_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("author") != "user"
        and (
            bool(entry.get("addToChainOfThought"))
            or entry.get("contentOrigin") == "ChainOfThoughtSummary"
        )
    )


# Only the explicitly delimited citation control is removed. Plain identifiers
# and human brackets in a summary are prose, not evidence of a citation.
_CITATION = re.compile(
    r"\ue200cite\ue202[^\ue201]*(?:\ue201|$)|\ue200(?:c(?:i(?:te?)?)?)?$"
)


class ReasoningSnapshots:
    """Emit monotonic suffixes, with a local ID for each upstream message.

    Revisions cannot retract an already delivered prefix, so they are ignored.
    An entry without an ID uses one anonymous slot: guessing identities from
    text would replay revisions as new summaries.
    """

    def __init__(self) -> None:
        self._seen: dict[str | None, tuple[str, str]] = {}

    def extract(self, entries: object) -> Iterator[ReasoningDelta]:
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not is_reasoning_entry(entry):
                continue
            text = entry.get("text")
            if not isinstance(text, str):
                continue
            text = _CITATION.sub("", text)
            if not text.strip():
                continue
            message_id = entry.get("MessageID") or entry.get("messageId")
            if not isinstance(message_id, str):
                message_id = None
            previous = self._seen.get(message_id)
            if previous is None:
                item_id, delivered = f"rs_{uuid.uuid4().hex}", ""
            else:
                item_id, delivered = previous
                if not text.startswith(delivered):
                    continue
            delta = text[len(delivered):]
            if delta:
                self._seen[message_id] = item_id, text
                yield ReasoningDelta(item_id, delta)


class ReasoningCollector:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self._last_item_id: str | None = None

    def add(self, event: ReasoningDelta) -> str:
        """Collect a suffix; return the Chat/Anthropic delta with item spacing."""
        separator = "\n\n" if self._last_item_id is not None and event.item_id != self._last_item_id else ""
        self.items[event.item_id] = self.items.get(event.item_id, "") + event.text
        self._last_item_id = event.item_id
        return separator + event.text

    def chat_fields(self) -> dict:
        return {"reasoning_content": "\n\n".join(self.items.values())} if self.items else {}

    def anthropic_blocks(self) -> list[dict]:
        if not self.items:
            return []
        # These are M365 summaries, not Anthropic-signed hidden thinking. The
        # signature is intentionally empty; no upstream signature is invented.
        return [{"type": "thinking", "thinking": "\n\n".join(self.items.values()), "signature": ""}]

    def responses_items(self) -> list[dict]:
        return [
            {"id": item_id, "type": "reasoning", "status": "completed",
             "summary": [{"type": "summary_text", "text": text}]}
            for item_id, text in self.items.items()
        ]


def collect_reasoning(*clients) -> ReasoningCollector:
    """Attach only for non-streaming requests; streaming consumes typed events."""
    collector = ReasoningCollector()
    for client in clients:
        if client is not None:
            client._reasoning_sink = collector.add
    return collector


@contextmanager
def suppress_reasoning(client):
    """A router classification uses chat()'s answer projection, with no sink."""
    sink = getattr(client, "_reasoning_sink", None)
    if sink is None:
        yield
        return
    client._reasoning_sink = None
    try:
        yield
    finally:
        client._reasoning_sink = sink
