"""Single-model connectivity probe for one pool account.

Whether a given mode works is decided by Microsoft's rollout per account, not by
anything in this proxy: the same refresh token answers on one tone and is refused
on the next, and the only way to know is to ask. Doing that by hand meant sending
a real request through a client and reading the raw failure, so this endpoint does
exactly that one turn and reports the four outcomes an operator acts on
differently:

    ok        -- upstream answered with text; this mode works for this account
    empty     -- upstream accepted the turn and said nothing (mode not rolled out)
    refused   -- upstream declined the turn outright (mode/account not allowed)
    throttled -- quota, not availability; retry after the reported window
    error     -- transport/credential failure; nothing to conclude about the mode

The probe rides the same client factory as /v1/chat/completions -- same per-account
egress, same provider dispatch, same tone/mode resolution -- because a test that
takes a different path can pass while real traffic fails. A request-local tracker
records only conversations this probe demonstrably created, including retries.
Cleanup is bounded, identity-bound, and never changes the probe verdict.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request

from .consumer_client import AccountThrottled
from .m365_cloud_client import CloudSessionIdentityChanged, delete_conversation
from .probe_conversations import ProbeConversations, probe_conversations
from .refresh_via_rt import _stored_binding
from .response_helpers import _json_err
from .routes_api_common import apply_request_model
from .substrate_client import (
    _EMPTY_TURN_MARKER,
    _M365_REFUSAL_TEXTS,
    _REFUSED_TURN_MARKER,
)
from .token_store import decode_jwt_payload

# Short enough that the answer is unambiguous, long enough that a mode which only
# emits a canned deflection still produces text we can show the operator.
_PROBE_PROMPT = "Reply with one word: pong"
_PROBE_TIMEOUT_SECONDS = 180.0
_CLEANUP_TIMEOUT_SECONDS = 20.0
_PREVIEW_CHARS = 600


def classify_probe(reply: str, error: str = "", *, throttled: bool = False) -> str:
    """Map one probe outcome onto the verdict an operator acts on."""
    if throttled:
        return "throttled"
    if error:
        if _REFUSED_TURN_MARKER in error or _EMPTY_TURN_MARKER in error:
            return "refused"
        return "error"
    # A refused mode does not always fail: M365 also declines by answering with one
    # canned line ("Sorry, I wasn't able to respond to that..."), which arrives as an
    # ordinary non-empty reply and so read as "ok" here. That made a sweep of the
    # picker report every mode this tenant may not use as working -- the one column an
    # operator acts on, wrong in the direction that costs a debugging session. Same
    # check scan_tones.probe() has always done, against the same imported set.
    if reply.strip() in _M365_REFUSAL_TEXTS:
        return "refused"
    return "ok" if reply.strip() else "empty"


def _is_throttled(exc: BaseException) -> bool:
    return isinstance(exc, AccountThrottled) or isinstance(
        getattr(exc, "__cause__", None), AccountThrottled
    )


def _probe_identity(account) -> tuple:
    token = getattr(account, "token", "") or ""
    try:
        claims = decode_jwt_payload(token) if token.count(".") == 2 else {}
    except Exception:
        claims = {}
    subject = (claims.get("tid"), claims.get("oid"))
    if not all(subject):
        subject = token  # Opaque JWE cannot prove that a replacement is the same user.
    return (
        account.provider, account.protocol_epoch, subject,
        getattr(account, "substrate_account_id", ""),
        getattr(account, "consumer_account_id", ""), _stored_binding(account),
    )


def _probe_profile(app, account):
    store = getattr(app.state, "protocol_profile_store", None)
    if store is None or account.provider != "m365":
        return None
    try:
        claims = decode_jwt_payload(account.token) if account.token.count(".") == 2 else {}
    except Exception:
        claims = {}
    return store.active(account_id=account.id, tenant_id=str(claims.get("tid") or ""))


async def _cleanup_probe(app, account, profile, probe: ProbeConversations) -> dict:
    if not probe.attempted:
        return {"status": "not_created"}

    def is_current() -> bool:
        current = app.state.account_store.get_request_snapshot(account.id)
        return (
            current is not None
            and _probe_identity(current) == _probe_identity(account)
            and _probe_profile(app, current) == profile
        )

    if not is_current():
        return {"status": "skipped", "message": "identity_changed"}
    if not probe.confirmed:
        return {"status": "skipped", "message": "creation_unconfirmed"}
    # Cloud management requires a verified AAD refresh grant. Personal Substrate
    # JWE has no readable AAD subject; Consumer's create credential is not a
    # verified delete grant. Neither may borrow another protocol's credentials.
    if (
        account.provider != "m365" or account.token.count(".") != 2
        or not account.refresh_token or _stored_binding(account) is None
    ):
        return {"status": "unsupported", "message": "delete_unsupported"}

    async def delete_owned() -> bool:
        failed = False
        for conversation_id in probe.confirmed:
            try:
                await delete_conversation(
                    app.state.account_store, account.id, conversation_id,
                    snapshot=account, is_current=is_current,
                )
            except (CloudSessionIdentityChanged, asyncio.TimeoutError):
                raise
            except Exception:
                failed = True
        return failed

    try:
        failed = await asyncio.wait_for(delete_owned(), timeout=_CLEANUP_TIMEOUT_SECONDS)
    except CloudSessionIdentityChanged:
        return {"status": "skipped", "message": "identity_changed"}
    except asyncio.TimeoutError:
        return {"status": "failed", "message": "delete_timeout"}
    except Exception:  # Cleanup is separate from the original probe result.
        return {"status": "failed", "message": "delete_failed"}
    if failed:
        return {"status": "failed", "message": "delete_failed"}
    if probe.candidates - probe.confirmed or probe.uncertain_create:
        return {"status": "skipped", "message": "creation_unconfirmed"}
    return {"status": "deleted"}


def register_admin_model_test_routes(
    app: FastAPI,
    require_admin: Callable[[Request], object | None],
    get_copilot_client: Callable[[Request], object],
) -> None:
    @app.post("/admin/model-test")
    async def model_test(request: Request) -> dict:
        err = require_admin(request)
        if err: return err
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - any unparsable body is the same 400
            return _json_err(400, "invalid JSON body")
        if not isinstance(body, dict):
            return _json_err(400, "invalid JSON body")
        account_id = str(body.get("account_id") or "").strip()
        model = str(body.get("model") or "").strip()
        prompt = str(body.get("prompt") or "").strip() or _PROBE_PROMPT
        if not account_id or not model:
            return _json_err(400, "account_id and model are required")
        account = app.state.account_store.get_request_snapshot(account_id)
        if account is None:
            return _json_err(404, "account not found")

        # The client factory reads only request.state, so a shim carrying this
        # account is enough to reuse the real /v1 path unchanged. api_key_obj is
        # None on purpose: a probe should measure the account, not inherit one
        # user's tone/prompt/timeout overrides.
        probe_request = SimpleNamespace(
            state=SimpleNamespace(account=account, api_key_obj=None)
        )
        result: dict = {
            "account_id": account_id,
            "account_name": account.name or account.id,
            "provider": getattr(account, "provider", "m365"),
            "model": model,
            "prompt": prompt,
            "cleanup": {"status": "not_created"},
        }
        try:
            client, resolved, is_consumer = apply_request_model(
                app, probe_request, get_copilot_client, model
            )
        except ValueError as exc:
            return _json_err(400, str(exc))
        except HTTPException as exc:
            result.update(verdict="error", error=str(exc.detail), latency_ms=0, reply="")
            return result
        result["upstream_selector"] = resolved
        profile = _probe_profile(app, account)
        probe = ProbeConversations()
        scope = probe_conversations.set(probe)

        started = time.monotonic()
        try:
            reply = await asyncio.wait_for(
                client.chat(prompt, [], None, None), timeout=_PROBE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            result.update(
                verdict="error",
                error=f"no response within {int(_PROBE_TIMEOUT_SECONDS)}s",
                latency_ms=int((time.monotonic() - started) * 1000),
                reply="",
            )
            return result
        except Exception as exc:  # Every client failure still reaches cleanup.
            detail = str(getattr(exc, "detail", "") or exc)
            result.update(
                verdict=classify_probe("", detail, throttled=_is_throttled(exc)),
                error=detail,
                latency_ms=int((time.monotonic() - started) * 1000),
                reply="",
            )
            return result
        else:
            result.update(
                verdict=classify_probe(reply),
                error="",
                latency_ms=int((time.monotonic() - started) * 1000),
                reply=reply[:_PREVIEW_CHARS],
                reply_len=len(reply),
            )
        finally:
            probe_conversations.reset(scope)
            # Cancellation of chat also lands here. A separate shielded task
            # keeps disconnect cancellation from interrupting bounded cleanup;
            # preserve cancellation rather than manufacturing a probe verdict.
            cleanup_task = asyncio.create_task(_cleanup_probe(app, account, profile, probe))
            try:
                result["cleanup"] = await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup_task)
                raise
        return result
