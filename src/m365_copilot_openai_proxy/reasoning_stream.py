"""Protocol lifecycles for typed reasoning summaries and Anthropic blocks."""
from __future__ import annotations

import json
from collections.abc import Iterator

from .reasoning import ReasoningCollector, ReasoningDelta


def _anthropic_event(payload: dict) -> str:
    return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"


class AnthropicContentStream:
    """One open content block at a time, with monotonic indices."""

    def __init__(self) -> None:
        self.next_index = 0
        self._index = -1
        self._kind: str | None = None
        self._reasoning = ReasoningCollector()

    def close(self) -> Iterator[str]:
        if self._kind is None:
            return
        if self._kind == "thinking":
            yield _anthropic_event({"type": "content_block_delta", "index": self._index,
                                    "delta": {"type": "signature_delta", "signature": ""}})
        yield _anthropic_event({"type": "content_block_stop", "index": self._index})
        self._kind = None

    def _start(self, kind: str) -> Iterator[str]:
        if self._kind == kind:
            return
        yield from self.close()
        self._kind = kind
        self._index = self.next_index
        self.next_index += 1
        field = "thinking" if kind == "thinking" else "text"
        yield _anthropic_event({"type": "content_block_start", "index": self._index,
                                "content_block": {"type": kind, field: ""}})

    def reasoning(self, event: ReasoningDelta) -> Iterator[str]:
        yield from self._start("thinking")
        yield _anthropic_event({"type": "content_block_delta", "index": self._index,
                                "delta": {"type": "thinking_delta", "thinking": self._reasoning.add(event)}})

    def text(self, text: str) -> Iterator[str]:
        yield from self._start("text")
        if text:
            yield _anthropic_event({"type": "content_block_delta", "index": self._index,
                                    "delta": {"type": "text_delta", "text": text}})

    def tool(self, block: dict) -> Iterator[str]:
        yield from self.close()
        index = self.next_index
        self.next_index += 1
        yield _anthropic_event({"type": "content_block_start", "index": index,
                                "content_block": {"type": "tool_use", "id": block["id"],
                                                  "name": block["name"], "input": {}}})
        yield _anthropic_event({"type": "content_block_delta", "index": index,
                                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)}})
        yield _anthropic_event({"type": "content_block_stop", "index": index})


class ResponsesReasoningStream:
    """Keep item IDs, output indices and final summary snapshots consistent."""

    def __init__(self) -> None:
        self.next_index = 0
        self._indices: dict[str, int] = {}
        self._collector = ReasoningCollector()

    def reserve_index(self) -> int:
        index = self.next_index
        self.next_index += 1
        return index

    def add(self, event: ReasoningDelta) -> Iterator[dict]:
        if event.item_id not in self._indices:
            index = self.reserve_index()
            self._indices[event.item_id] = index
            yield {"type": "response.output_item.added", "output_index": index,
                   "item": {"id": event.item_id, "type": "reasoning", "status": "in_progress", "summary": []}}
            yield {"type": "response.reasoning_summary_part.added", "item_id": event.item_id,
                   "output_index": index, "summary_index": 0,
                   "part": {"type": "summary_text", "text": ""}}
        self._collector.add(event)
        yield {"type": "response.reasoning_summary_text.delta", "item_id": event.item_id,
               "output_index": self._indices[event.item_id], "summary_index": 0, "delta": event.text}

    def finish(self) -> Iterator[dict]:
        for item in self._collector.responses_items():
            index = self._indices[item["id"]]
            part = item["summary"][0]
            yield {"type": "response.reasoning_summary_text.done", "item_id": item["id"],
                   "output_index": index, "summary_index": 0, "text": part["text"]}
            yield {"type": "response.reasoning_summary_part.done", "item_id": item["id"],
                   "output_index": index, "summary_index": 0, "part": part}
            yield {"type": "response.output_item.done", "output_index": index, "item": item}

    def output(self, *extra_items: tuple[int, dict]) -> list[dict]:
        indexed = [(self._indices[item["id"]], item) for item in self._collector.responses_items()]
        indexed.extend(extra_items)
        return [item for _, item in sorted(indexed, key=lambda pair: pair[0])]
