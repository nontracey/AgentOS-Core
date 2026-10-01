"""Policy 判定（FR-GOV-01/02、FR-TOOL-08）：默认拒绝 + 显式预授权 + deny 优先。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agentos.adapters.store.sqlite import SqliteStore
from agentos.adapters.tools.local import LocalToolAdapter
from agentos.kernel.dto import PolicyGrant
from agentos.kernel.errors import ErrorCode
from agentos.kernel.types import AuthContext, ToolSpec, utc_now
from agentos.services.policy import PolicyService


def actor(scopes: list[str] | None = None) -> AuthContext:
    return AuthContext(
        subject="owner",
        tenant_id="default",
        scopes=scopes if scopes is not None else ["read", "write", "approve"],
        policy_version="test-1",
        expires_at=utc_now() + timedelta(minutes=10),
    )


def read_spec() -> ToolSpec:
    return ToolSpec(
        name="math.add", version="1", input_schema={"type": "object"}, risks=["read"],
        recovery="pure", timeout_ms=1000,
    )


def write_spec() -> ToolSpec:
    return ToolSpec(
        name="fs.write_text", version="1",
        input_schema={"type": "object"}, risks=["write"], recovery="idempotency_key",
        timeout_ms=1000,
    )


@pytest.fixture
def policy(tmp_path: object) -> PolicyService:
    store = SqliteStore(tmp_path / "p.db")  # type: ignore[arg-type, operator]
    return PolicyService(store, policy_version="test-1")


def test_read_only_tool_allowed_with_read_scope(policy: PolicyService) -> None:
    decision = policy.check(actor(), read_spec(), {})
    assert decision.outcome == "allowed"


def test_actor_without_read_scope_denied(policy: PolicyService) -> None:
    decision = policy.check(actor(scopes=[]), read_spec(), {})
    assert decision.outcome == "denied"


def test_write_tool_without_grant_needs_approval(policy: PolicyService) -> None:
    decision = policy.check(actor(), write_spec(), {})
    assert decision.outcome == "needs_approval"
    assert decision.needs_approval_risks == ["write"]


def test_write_tool_without_write_scope_denied_even_with_read(policy: PolicyService) -> None:
    decision = policy.check(actor(scopes=["read"]), write_spec(), {})
    assert decision.outcome == "denied"
    assert "scope" in decision.reason


def test_active_grant_allows_write(policy: PolicyService) -> None:
    grant = PolicyGrant(
        id="g1", tenant_id="default", subject="owner", tool_name="fs.write_text",
        risks=["write"], expires_at=utc_now() + timedelta(minutes=5), issued_by="owner",
    )
    policy._store.upsert_grant(grant)  # type: ignore[attr-defined]
    decision = policy.check(actor(), write_spec(), {})
    assert decision.outcome == "allowed"
    assert decision.grant_id == "g1"


def test_revoked_grant_does_not_authorize(policy: PolicyService) -> None:
    grant = PolicyGrant(
        id="g2", tenant_id="default", subject="owner", tool_name="fs.write_text",
        risks=["write"], expires_at=utc_now() + timedelta(minutes=5), issued_by="owner",
        revoked=True,
    )
    policy._store.upsert_grant(grant)  # type: ignore[attr-defined]
    assert policy.check(actor(), write_spec(), {}).outcome == "needs_approval"


def test_expired_grant_does_not_authorize(policy: PolicyService) -> None:
    grant = PolicyGrant(
        id="g3", tenant_id="default", subject="owner", tool_name="fs.write_text",
        risks=["write"], expires_at=utc_now() - timedelta(seconds=1), issued_by="owner",
    )
    policy._store.upsert_grant(grant)  # type: ignore[attr-defined]
    assert policy.check(actor(), write_spec(), {}).outcome == "needs_approval"


def test_invalid_arguments_denied(policy: PolicyService) -> None:
    spec = ToolSpec(
        name="math.add", version="1",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "number"}},
            "required": ["a"],
            "additionalProperties": False,
        },
        risks=["read"], recovery="pure", timeout_ms=1000,
    )
    decision = policy.check(actor(), spec, {"a": "not-a-number"})
    assert decision.outcome == "denied"


def test_expired_actor_denied(policy: PolicyService) -> None:
    expired = AuthContext(
        subject="owner", tenant_id="default", scopes=["read", "write"],
        policy_version="test-1", expires_at=utc_now() - timedelta(seconds=1),
    )
    assert policy.check(expired, read_spec(), {}).outcome == "denied"


def test_policy_default_must_be_deny(tmp_path: object) -> None:
    from agentos.kernel.errors import AgentOSError

    with pytest.raises(AgentOSError):
        PolicyService(
            SqliteStore(tmp_path / "x.db"),  # type: ignore[arg-type, operator]
            policy_version="t",
            default="allow",  # type: ignore[arg-type]
        )


def test_tool_sandbox_escape_rejected(tmp_path: object) -> None:
    tools = LocalToolAdapter(tmp_path / "sandbox")  # type: ignore[arg-type, operator]
    for bad in ("/etc/passwd", "../../escape.txt", "a/../../b"):
        with pytest.raises(Exception) as exc_info:
            LocalToolAdapter._sandboxed_path(tools._sandbox, bad)  # type: ignore[attr-defined]
        assert "sandbox" in str(exc_info.value)


def test_error_code_side_effect_semantics() -> None:
    assert ErrorCode.TIMEOUT.side_effect_unknown is True
    assert ErrorCode.UNCERTAIN.side_effect_unknown is True
    assert ErrorCode.DENIED.side_effect_unknown is False
    assert ErrorCode.UNAVAILABLE.side_effect_unknown is False
