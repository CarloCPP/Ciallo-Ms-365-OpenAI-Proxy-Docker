"""In-memory creation evidence, enabled only around an admin model probe.

A generated/captured ID is not evidence that a conversation exists. Substrate
must acknowledge that exact new socket ID with stored messages or a completed
turn; Consumer must return an ID from its successful create response. Existing
empty-turn/gate retries remain inside the same request-owned scope.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class ProbeConversations:
    attempted: bool = False
    candidates: set[str] = field(default_factory=set)
    confirmed: set[str] = field(default_factory=set)
    uncertain_create: bool = False

    def substrate_started(self, conversation_id: str) -> None:
        self.attempted = True
        self.candidates.add(conversation_id)

    def substrate_frame(self, conversation_id: str, request_id: str, message: dict) -> None:
        if conversation_id not in self.candidates:
            return
        if message.get("type") == 1 and message.get("target") == "update":
            payloads = message.get("arguments") or []
        elif message.get("type") == 2:
            payloads = [message.get("item")]
        else:
            return
        for payload in payloads:
            if not isinstance(payload, dict) or payload.get("conversationId") != conversation_id:
                continue
            if payload.get("requestId") not in (None, "", request_id):
                continue
            messages = payload.get("messages")
            if (isinstance(messages, list) and messages) or payload.get("turnState") in ("Completed", "Failed"):
                self.confirmed.add(conversation_id)

    def consumer_started(self) -> None:
        self.attempted = True
        self.uncertain_create = True

    def consumer_created(self, conversation_id: str) -> None:
        self.candidates.add(conversation_id)
        self.confirmed.add(conversation_id)
        self.uncertain_create = False


probe_conversations: ContextVar[ProbeConversations | None] = ContextVar(
    "model_probe_conversations", default=None,
)
