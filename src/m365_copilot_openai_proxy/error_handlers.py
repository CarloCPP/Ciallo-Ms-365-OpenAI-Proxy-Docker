from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

_logger = logging.getLogger(__name__)


def rate_limit_error_payload(path: str, message: str) -> dict:
    if path.rstrip("/") == "/v1/messages":
        return {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": message},
        }
    return {
        "error": {
            "message": message,
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }
    }


class UpstreamNetworkHTTPError(HTTPException):
    def __init__(self, message: str) -> None:
        super().__init__(status_code=503, detail=message)


def network_error_payload(path: str, message: str) -> dict:
    error = {"type": "network_error", "code": "network_error", "message": message}
    if path.rstrip("/") == "/v1/messages":
        # Keep Anthropic's standard type; the additive code identifies transport.
        return {"type": "error", "error": {**error, "type": "api_error"}}
    return {"error": error}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # Log the real exception server-side but never leak internal details
        # (file paths, hostnames, library internals) to the client.
        _logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        # This handler is installed on ServerErrorMiddleware, which sits OUTSIDE
        # the middleware stack, so the default refusal never reaches this body.
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Internal server error", "type": "internal_error"}},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        headers = {"Access-Control-Allow-Origin": "*"}
        headers.update(exc.headers or {})
        if isinstance(exc, UpstreamNetworkHTTPError):
            content = network_error_payload(request.url.path, str(exc.detail))
        elif exc.status_code == 429:
            content = rate_limit_error_payload(request.url.path, str(exc.detail))
        else:
            content = {"error": {"message": exc.detail, "type": "http_error"}}
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=headers,
        )
