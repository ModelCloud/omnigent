"""Method/object containment without owner identity fallback or a real runner process."""

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from omnigent.db import current_workspace_id, workspace_scope
from omnigent.errors import OmnigentError
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.bgr_generation_auth import (
    GenerationAuth,
    GenerationAuthorityMiddleware,
    generation_actor,
)
from tests.host.test_bgr_supervisor import request_stop as request_stop


@pytest.fixture
def generation_authority(request_stop):
    generation = request_stop.admission.generation.model_copy(
        update={"runner_id": token_bound_runner_id("binding")}
    )
    state = {"live": True, "binding": True}

    def validate(candidate):
        if not state["live"] or candidate != generation:
            raise ValueError("revoked or foreign generation")
        return "owner"

    authority = GenerationAuth(
        key=b"s" * 32,
        resolve=lambda runner: generation if runner == generation.runner_id else None,
        validate=validate,
        binding=lambda session: (
            (generation.runner_id, generation.host_id)
            if state["binding"] and session == generation.brain_session_id
            else None
        ),
    )
    return authority, generation, state


@pytest.fixture
def accounts_provider():
    return UnifiedAuthProvider(
        source="accounts",
        accounts_config=AccountsConfig(
            cookie_secret=b"s" * 32,
            session_ttl_hours=8,
            base_url="http://server",
            init_admin_password=None,
            invite_ttl_seconds=3600,
            magic_ttl_seconds=300,
        ),
    )


def test_generation_principal_is_never_an_owner_bearer(generation_authority, accounts_provider):
    authority, generation, _ = generation_authority
    token = authority.mint(generation)["token"]
    principal = authority.decode(token)
    assert principal.generation == generation
    request = HTTPConnection(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    )
    assert accounts_provider.get_user_id(request) is None
    with pytest.raises(jwt.InvalidTokenError):
        jwt.decode(token.removeprefix("bgr1."), b"s" * 32, algorithms=["HS256"])
    assert accounts_provider._cookie_cache == {}
    assert accounts_provider.generation_signing_key() == b"s" * 32
    assert UnifiedAuthProvider(source="header").generation_signing_key() is None


@pytest.mark.parametrize(
    "fault",
    ["revoked", "binding", "workspace", "signature", "unsupported", "expired", "malformed"],
)
def test_generation_credentials_require_live_ledger_on_every_use(generation_authority, fault):
    authority, generation, state = generation_authority
    token = authority.mint(generation)["token"]
    if fault in ("revoked", "binding"):
        state["live" if fault == "revoked" else "binding"] = False
    elif fault == "signature":
        token = token[:-8] + "invalid!"
    elif fault == "unsupported":
        authority._key = None
    elif fault in ("expired", "malformed"):
        claims = jwt.decode(
            token.removeprefix("bgr1."),
            authority._key,
            algorithms=["HS256"],
            audience="omnigent-bgr-generation-v1",
        )
        if fault == "expired":
            claims["exp"] = 1
        else:
            claims["generation"]["epoch"] = True
        token = "bgr1." + jwt.encode(claims, authority._key, algorithm="HS256")
    if fault == "workspace":
        with workspace_scope(current_workspace_id() + 1), pytest.raises(OmnigentError):
            authority.decode(token)
    else:
        with pytest.raises((OmnigentError, ValueError)):
            authority.decode(token)


@pytest.mark.parametrize(
    "method,path,allowed",
    [
        ("GET", "/v1/bgr/teams/{team}", True),
        ("POST", "/v1/bgr/control", True),
        ("POST", "/v1/bgr/teams/{team}/native-callback", True),
        ("DELETE", "/v1/bgr/teams/{team}", False),
        ("GET", "/v1/bgr/teams/foreign", False),
        ("POST", "/v1/bgr/teams/{team}/recover", False),
        ("POST", "/v1/bgr/teams/{team}/reviews/round/publish", False),
        ("POST", "/v1/hosts/host/runners", False),
        ("GET", "/v1/hosts", False),
        ("POST", "/v1/sessions", False),
        ("POST", "/v1/sessions/session/events", False),
        ("GET", "/v1/sessions/session/agent", False),
        ("POST", "/v1/mcp", False),
        ("GET", "/v1/credentials", False),
        ("GET", "/v1/install", False),
        ("POST", "/auth/delegated/grant", False),
    ],
)
def test_only_explicit_generation_routes_are_authorized(
    generation_authority, accounts_provider, method, path, allowed
):
    authority, generation, _ = generation_authority
    app = FastAPI()
    app.state.bgr_generation_auth = authority
    app.add_middleware(GenerationAuthorityMiddleware, authority=authority)

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    def endpoint(request: Request):
        assert accounts_provider.get_user_id(request) is None
        return {"actor": generation_actor(request)}

    headers = {
        "Authorization": f"Bearer {authority.mint(generation)['token']}",
        "Cookie": f"ap_session={accounts_provider.mint_runner_token('other-owner', 60)}",
        RUNNER_TUNNEL_TOKEN_HEADER: "binding",
        ORCHESTRATION_SOURCE_HEADER: generation.brain_session_id,
    }
    with TestClient(app) as client:
        response = client.request(method, path.format(team=generation.team_id), headers=headers)
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        assert response.json() == {"actor": "owner"}


@pytest.mark.parametrize(
    "fault", ["wrong-source", "no-source", "wrong-binding", "no-binding", "bare-binding"]
)
def test_headers_cannot_select_another_actor_or_upgrade_binding(generation_authority, fault):
    authority, generation, _ = generation_authority
    app = FastAPI()
    app.add_middleware(GenerationAuthorityMiddleware, authority=authority)

    @app.get("/v1/bgr/teams/{team}")
    def endpoint():
        pytest.fail("unauthorized request reached handler")

    headers = {
        "Authorization": f"Bearer {authority.mint(generation)['token']}",
        RUNNER_TUNNEL_TOKEN_HEADER: "binding",
        ORCHESTRATION_SOURCE_HEADER: generation.brain_session_id,
    }
    if fault == "bare-binding":
        headers.pop("Authorization")
    elif fault.endswith("source"):
        headers[ORCHESTRATION_SOURCE_HEADER] = "foreign" if fault == "wrong-source" else ""
    else:
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = "foreign" if fault == "wrong-binding" else ""
    with TestClient(app) as client:
        assert (
            client.get(f"/v1/bgr/teams/{generation.team_id}", headers=headers).status_code == 403
        )


def test_disabled_generation_backend_rejects_typed_tokens_but_leaves_ordinary_requests_unchanged():
    app = FastAPI()
    app.add_middleware(GenerationAuthorityMiddleware, authority=None)

    @app.get("/ordinary")
    def endpoint():
        return {"ordinary": True}

    with TestClient(app) as client:
        assert client.get("/ordinary").status_code == 200
        assert (
            client.get("/ordinary", headers={"Authorization": "Bearer bgr1.invalid"}).status_code
            == 403
        )
