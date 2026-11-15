"""A separate, deny-by-default principal for admitted supervised BGR generations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Literal

import jwt
from pydantic import Field
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.bgr_records import Record
from omnigent.bgr_supervision import SupervisedGeneration
from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)

_PRINCIPAL = "omnigent.bgr_generation_principal"
_TOKEN_PREFIX = "bgr1."
_AUDIENCE = "omnigent-bgr-generation-v1"
_TTL_SECONDS = 120


def _deny() -> OmnigentError:
    return OmnigentError(
        "Supervised generation authority is unavailable", code=ErrorCode.FORBIDDEN
    )


class GenerationPrincipal(Record):
    kind: Literal["bgr-generation-v1"] = "bgr-generation-v1"
    workspace_id: int
    owner_id: str = Field(min_length=1, max_length=320)
    generation: SupervisedGeneration


def generation_principal(connection: HTTPConnection) -> GenerationPrincipal | None:
    value = connection.scope.get(_PRINCIPAL)
    return value if isinstance(value, GenerationPrincipal) else None


class GenerationAuth:
    def __init__(
        self,
        *,
        key: bytes | None,
        resolve: Callable[[str], SupervisedGeneration | None],
        validate: Callable[[SupervisedGeneration], str],
        binding: Callable[[str], tuple[str | None, str | None] | None],
    ) -> None:
        self._key = hmac.digest(key, _AUDIENCE.encode(), hashlib.sha256) if key else None
        self.resolve = resolve
        self._validate = validate
        self._binding = binding

    def principal(self, generation: SupervisedGeneration) -> GenerationPrincipal:
        if self._key is None:
            raise _deny()
        principal = GenerationPrincipal(
            workspace_id=current_workspace_id(),
            owner_id=self._validate(generation),
            generation=generation,
        )
        self.check_live(principal)
        return principal

    def check_live(self, principal: GenerationPrincipal) -> None:
        generation = principal.generation
        if (
            self._key is None
            or principal.workspace_id != current_workspace_id()
            or self._validate(generation) != principal.owner_id
            or self._binding(generation.brain_session_id)
            != (generation.runner_id, generation.host_id)
        ):
            raise _deny()

    def mint(self, generation: SupervisedGeneration) -> dict[str, str | int]:
        principal = self.principal(generation)
        assert self._key is not None
        now = int(time.time())
        token = jwt.encode(
            {
                **principal.model_dump(mode="json"),
                "aud": _AUDIENCE,
                "iat": now,
                "exp": now + _TTL_SECONDS,
            },
            self._key,
            algorithm="HS256",
        )
        return {"token": _TOKEN_PREFIX + token, "expires_at": now + _TTL_SECONDS}

    def decode(self, token: str) -> GenerationPrincipal:
        if self._key is None or not token.startswith(_TOKEN_PREFIX) or len(token) > 8192:
            raise _deny()
        try:
            claims = jwt.decode(
                token[len(_TOKEN_PREFIX) :],
                self._key,
                algorithms=["HS256"],
                audience=_AUDIENCE,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "aud",
                        "kind",
                        "workspace_id",
                        "owner_id",
                        "generation",
                    ]
                },
            )
            for name in ("aud", "iat", "exp"):
                claims.pop(name)
            principal = GenerationPrincipal.model_validate(claims)
        except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
            raise _deny() from exc
        self.check_live(principal)
        return principal

    def check_connection(self, connection: HTTPConnection, principal: GenerationPrincipal) -> None:
        self.check_live(principal)
        generation = principal.generation
        token = (connection.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        if not token or token_bound_runner_id(token) != generation.runner_id:
            raise _deny()
        path = connection.url.path
        if (
            connection.scope["type"] == "http"
            and connection.scope["method"] == "POST"
            and path == f"/v1/runners/{generation.runner_id}/token"
        ):
            return
        if connection.scope["type"] == "websocket":
            if path != f"/v1/runners/{generation.runner_id}/tunnel":
                raise _deny()
            return
        allowed = {
            ("GET", f"/v1/bgr/teams/{generation.team_id}"),
            ("POST", "/v1/bgr/control"),
            ("POST", f"/v1/bgr/teams/{generation.team_id}/native-callback"),
        }
        if (connection.scope["method"], path) not in allowed or connection.headers.get(
            ORCHESTRATION_SOURCE_HEADER
        ) != generation.brain_session_id:
            raise _deny()


def generation_actor(request: Request) -> str | None:
    """Internal ledger actor on explicitly opted-in routes, never ordinary user identity."""
    principal = generation_principal(request)
    if principal is None:
        return None
    request.app.state.bgr_generation_auth.check_connection(request, principal)
    return principal.owner_id


class GenerationAuthorityMiddleware:
    def __init__(self, app: ASGIApp, authority: GenerationAuth | None) -> None:
        self._app, self._authority = app, authority

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        try:
            principal = await asyncio.to_thread(self._resolve, connection)
        except (OmnigentError, ValueError):
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 4004,
                        "reason": "generation authority denied",
                    }
                )
            else:
                await JSONResponse({"detail": "generation authority denied"}, status_code=403)(
                    scope, receive, send
                )
            return
        if principal is not None:
            scope[_PRINCIPAL] = principal
        try:
            await self._app(scope, receive, send)
        finally:
            scope.pop(_PRINCIPAL, None)

    def _resolve(self, connection: HTTPConnection) -> GenerationPrincipal | None:
        authorization = connection.headers.get("Authorization", "").split()
        bearer = (
            authorization[1]
            if len(authorization) == 2 and authorization[0].lower() == "bearer"
            else ""
        )
        if self._authority is None:
            if bearer.startswith(_TOKEN_PREFIX):
                raise _deny()
            return None
        binding_token = (connection.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        classified = (
            self._authority.resolve(token_bound_runner_id(binding_token))
            if binding_token
            else None
        )
        if bearer.startswith(_TOKEN_PREFIX):
            principal = self._authority.decode(bearer)
            if classified != principal.generation:
                raise _deny()
        elif classified is not None:
            if connection.scope["type"] == "http" and (
                connection.scope["method"] == "POST"
                and connection.url.path == f"/v1/runners/{classified.runner_id}/token"
            ):
                return None
            if connection.scope["type"] != "websocket":
                raise _deny()
            principal = self._authority.principal(classified)
        else:
            return None
        self._authority.check_connection(connection, principal)
        return principal
