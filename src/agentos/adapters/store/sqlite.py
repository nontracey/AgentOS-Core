"""SQLite 存储适配器（蓝图 §21）。

- 每连接启用 foreign_keys、WAL、bounded busy_timeout；单连接操作由锁串行化。
- events / audit_log 由触发器强制 append-only（FR-GOV-03 M0 基线）。
- claim/renew/complete 用版本与 fence 条件更新，返回 0 行即失去所有权。
- accept_once 原子写：幂等登记 + run/job + 附件关联 + outbox + 事件。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.kernel.dto import PolicyGrant
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import ApprovalState, RunState, StepState
from agentos.kernel.store import (
    ApprovalRecord,
    AuditEvent,
    JobRecord,
    Lease,
    OutboxEvent,
    RunRecord,
    StepRecord,
)
from agentos.kernel.types import Receipt
from agentos.kernel.types import Receipt as ReceiptModel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, subject TEXT NOT NULL,
  state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  channel TEXT NOT NULL DEFAULT '', thread_id TEXT,
  goal_text TEXT NOT NULL DEFAULT '', envelope_json TEXT NOT NULL DEFAULT '{}',
  checkpoint_json TEXT NOT NULL DEFAULT '{}', error_json TEXT,
  budget_microusd INTEGER NOT NULL DEFAULT 0,
  cancel_requested_at TEXT, lease_owner TEXT, lease_until TEXT, lease_fence INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id));

CREATE TABLE IF NOT EXISTS steps (
  tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, id TEXT NOT NULL,
  call_id TEXT NOT NULL, tool_name TEXT, args_hash TEXT NOT NULL DEFAULT '',
  input_ref TEXT, state TEXT NOT NULL CHECK(state IN
    ('prepared','awaiting_approval','dispatched','pending_external','completed','failed','uncertain','cancelled')),
  approval_id TEXT, external_ref TEXT, result_json TEXT, error_json TEXT,
  fence INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
  started_at TEXT, finished_at TEXT,
  PRIMARY KEY(tenant_id, run_id, id), UNIQUE(tenant_id, call_id),
  FOREIGN KEY(tenant_id, run_id) REFERENCES runs(tenant_id, id));

CREATE TABLE IF NOT EXISTS jobs (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('ready','claimed','blocked','done')),
  available_at TEXT NOT NULL, lease_owner TEXT, lease_until TEXT,
  fence INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(tenant_id, id),
  FOREIGN KEY(tenant_id, run_id) REFERENCES runs(tenant_id, id));
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, available_at, lease_until);

CREATE TABLE IF NOT EXISTS idempotency (
  tenant_id TEXT NOT NULL, subject TEXT NOT NULL, channel TEXT NOT NULL,
  request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, run_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, expires_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, subject, channel, request_key),
  FOREIGN KEY(tenant_id, run_id) REFERENCES runs(tenant_id, id));

CREATE TABLE IF NOT EXISTS approvals (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, call_id TEXT NOT NULL,
  subject TEXT NOT NULL, args_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected','expired','consumed')),
  expires_at TEXT NOT NULL, decided_by TEXT, decided_at TEXT, summary TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(tenant_id, id), UNIQUE(tenant_id, call_id),
  FOREIGN KEY(tenant_id, call_id) REFERENCES steps(tenant_id, call_id));

CREATE TABLE IF NOT EXISTS events (
  tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, run_id, seq),
  FOREIGN KEY(tenant_id, run_id) REFERENCES runs(tenant_id, id));

CREATE TABLE IF NOT EXISTS outbox (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, event_key TEXT NOT NULL,
  topic TEXT NOT NULL, payload_json TEXT NOT NULL, delivered_at TEXT,
  PRIMARY KEY(tenant_id, id), UNIQUE(tenant_id, event_key));

CREATE TABLE IF NOT EXISTS inbox (
  tenant_id TEXT NOT NULL, provider TEXT NOT NULL, event_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL, processed_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, provider, event_id));

CREATE TABLE IF NOT EXISTS deletion_tombstones (
  tenant_id TEXT NOT NULL, resource_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, resource_id));

CREATE TABLE IF NOT EXISTS budget_reservations (
  tenant_id TEXT NOT NULL, call_id TEXT NOT NULL, run_id TEXT NOT NULL,
  reserved_microusd INTEGER NOT NULL, settled_microusd INTEGER,
  state TEXT NOT NULL CHECK(state IN ('reserved','settled','released')),
  PRIMARY KEY(tenant_id, call_id),
  FOREIGN KEY(tenant_id, run_id) REFERENCES runs(tenant_id, id));

CREATE TABLE IF NOT EXISTS audit_log (
  tenant_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
  subject TEXT, run_id TEXT, detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  prev_hash TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(tenant_id, seq));

CREATE TABLE IF NOT EXISTS grants (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, subject TEXT NOT NULL,
  tool_name TEXT NOT NULL, risks_json TEXT NOT NULL DEFAULT '[]',
  expires_at TEXT NOT NULL, issued_by TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(tenant_id, id));

CREATE TABLE IF NOT EXISTS attachments (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, owner TEXT NOT NULL,
  display_name TEXT NOT NULL, storage_path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
  content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
  run_id TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id),
  FOREIGN KEY(tenant_id, run_id) REFERENCES runs(tenant_id, id));

-- ---- M1: SkillHub / MCP Hub / Workflow / Nodes ----
CREATE TABLE IF NOT EXISTS skills (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL,
  digest TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL, install_path TEXT NOT NULL,
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  entrypoints_json TEXT NOT NULL DEFAULT '[]',
  installed_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(tenant_id, id, version));

CREATE TABLE IF NOT EXISTS mcp_servers (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL,
  transport TEXT NOT NULL CHECK(transport IN ('stdio','http')),
  command TEXT, url TEXT, env_json TEXT NOT NULL DEFAULT '{}',
  risks_json TEXT NOT NULL DEFAULT '["write"]',
  protocol_version TEXT, enabled INTEGER NOT NULL DEFAULT 0,
  tools_json TEXT NOT NULL DEFAULT '[]',
  registered_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id));

CREATE TABLE IF NOT EXISTS resource_drafts (
  tenant_id TEXT NOT NULL, subject TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
  payload_json TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, subject, kind, id));

CREATE TABLE IF NOT EXISTS workflow_definitions (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  definition_json TEXT NOT NULL, definition_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','published','retired')),
  published_at TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id, version));

CREATE TABLE IF NOT EXISTS workflow_delegations (
  tenant_id TEXT NOT NULL, child_run_id TEXT NOT NULL, parent_run_id TEXT NOT NULL,
  reservation_id TEXT NOT NULL, PRIMARY KEY(tenant_id, child_run_id));

CREATE TABLE IF NOT EXISTS workflow_runs (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, workflow_id TEXT NOT NULL,
  definition_version INTEGER NOT NULL, internal_run_id TEXT,
  checkpoint_owner TEXT NOT NULL DEFAULT 'agentos',
  external_ref TEXT, status TEXT NOT NULL DEFAULT 'running',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id),
  FOREIGN KEY(tenant_id, internal_run_id) REFERENCES runs(tenant_id, id));

CREATE TABLE IF NOT EXISTS nodes (
  tenant_id TEXT NOT NULL, node_id TEXT NOT NULL,
  os TEXT NOT NULL DEFAULT '', arch TEXT NOT NULL DEFAULT '',
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  endpoint TEXT NOT NULL, token_hash TEXT NOT NULL, token_plain TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'paired',
  last_heartbeat TEXT, paired_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, node_id));

CREATE TABLE IF NOT EXISTS assets (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL,
  sha256 TEXT NOT NULL, size INTEGER NOT NULL,
  mime TEXT NOT NULL DEFAULT 'application/octet-stream',
  source_node TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  run_id TEXT,
  name TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 0,
  parent_asset_id TEXT,
  revision_reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(tenant_id, id));

-- N2a：素材上传幂等（同键同内容回原 Asset；同键异正文冲突在服务层判定）
CREATE TABLE IF NOT EXISTS material_idem (
  tenant_id TEXT NOT NULL, subject TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  content_sha256 TEXT NOT NULL, asset_id TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, subject, idempotency_key));

-- 登录会话（双 token：refresh 轮换/撤销/重用检测；access 为无状态签名令牌不落库）
CREATE TABLE IF NOT EXISTS auth_sessions (
  tenant_id TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL,
  refresh_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id));
CREATE INDEX IF NOT EXISTS idx_auth_sessions_hash
  ON auth_sessions(tenant_id, refresh_hash);

-- ---- U4-01：演示访问（admin 发布 / guest 只读）----
-- grant：绑定管理员、游客 subject、tenant、到期/撤销状态与当前发布 revision
-- kind：invite=管理员签发的定向邀请；public=免登游客（U5-01，模板与访客会话共用）
CREATE TABLE IF NOT EXISTS demo_grants (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, subject TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'invite',
  status TEXT NOT NULL CHECK(status IN ('active','revoked','expired')) DEFAULT 'active',
  manifest_id TEXT, manifest_revision INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL, expires_at TEXT NOT NULL,
  revoked_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id));
CREATE INDEX IF NOT EXISTS idx_demo_grants_subject
  ON demo_grants(tenant_id, subject);
CREATE INDEX IF NOT EXISTS idx_demo_grants_kind
  ON demo_grants(tenant_id, kind, status);

-- manifest 不可变：revision 只增，历史 revision 由触发器保护不可改/不可删
CREATE TABLE IF NOT EXISTS demo_manifests (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, grant_id TEXT NOT NULL,
  revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL, published_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id, revision));

-- 邀请兑换码：库内只存 hash，明文一次性返回给管理员转交
CREATE TABLE IF NOT EXISTS demo_invites (
  tenant_id TEXT NOT NULL, id TEXT NOT NULL, grant_id TEXT NOT NULL,
  code_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
  redeemed_at TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, id), UNIQUE(tenant_id, code_hash));

-- append-only 强制（FR-GOV-03）：审计与事件历史不可改写、不可截断
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
-- U4：发布清单不可变（新版本=新 revision 行，旧 revision 不可改写）
CREATE TRIGGER IF NOT EXISTS demo_manifests_no_update BEFORE UPDATE ON demo_manifests
BEGIN SELECT RAISE(ABORT, 'demo_manifests is immutable'); END;
CREATE TRIGGER IF NOT EXISTS demo_manifests_no_delete BEFORE DELETE ON demo_manifests
BEGIN SELECT RAISE(ABORT, 'demo_manifests is immutable'); END;
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        tenant_id=row["tenant_id"],
        id=row["id"],
        subject=row["subject"],
        state=RunState(row["state"]),
        version=row["version"],
        channel=row["channel"],
        thread_id=row["thread_id"],
        goal_text=row["goal_text"],
        envelope_json=row["envelope_json"],
        checkpoint_json=row["checkpoint_json"],
        error_json=row["error_json"],
        budget_microusd=row["budget_microusd"],
        cancel_requested_at=_dt(row["cancel_requested_at"]),
        lease_owner=row["lease_owner"],
        lease_until=_dt(row["lease_until"]),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def _row_to_step(row: sqlite3.Row) -> StepRecord:
    return StepRecord(
        tenant_id=row["tenant_id"],
        run_id=row["run_id"],
        id=row["id"],
        call_id=row["call_id"],
        tool_name=row["tool_name"],
        args_hash=row["args_hash"],
        input_ref=row["input_ref"],
        state=StepState(row["state"]),
        approval_id=row["approval_id"],
        external_ref=row["external_ref"],
        result_json=row["result_json"],
        error_json=row["error_json"],
        fence=row["fence"],
        attempt=row["attempt"],
        started_at=_dt(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
    )


def _row_to_approval(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        tenant_id=row["tenant_id"],
        id=row["id"],
        call_id=row["call_id"],
        subject=row["subject"],
        args_hash=row["args_hash"],
        policy_version=row["policy_version"],
        state=ApprovalState(row["state"]),
        expires_at=_dt(row["expires_at"]) or utc_now(),
        decided_by=row["decided_by"],
        decided_at=_dt(row["decided_at"]),
        summary=row["summary"],
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


class SqliteStore:
    """同步实现；异步调用方经 asyncio.to_thread 使用（运行时不阻塞事件循环）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """既有库的就地升级：assets 主体/任务绑定列 + 依据审计回填归属（保守：无证据不赋 owner）。"""
        run_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "lease_fence" not in run_cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN lease_fence INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("UPDATE runs SET lease_fence=version")
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(assets)").fetchall()}
        needed = {
            "subject": "ALTER TABLE assets ADD COLUMN subject TEXT NOT NULL DEFAULT ''",
            "run_id": "ALTER TABLE assets ADD COLUMN run_id TEXT",
            "name": "ALTER TABLE assets ADD COLUMN name TEXT NOT NULL DEFAULT ''",
            "version": "ALTER TABLE assets ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
            "parent_asset_id": "ALTER TABLE assets ADD COLUMN parent_asset_id TEXT",
            "revision_reason": "ALTER TABLE assets ADD COLUMN revision_reason TEXT NOT NULL DEFAULT ''",
        }
        for col, stmt in needed.items():
            if col not in cols:
                self._conn.execute(stmt)
        # U5-01：既有库补 demo_grants.kind（默认 invite，旧数据行为不变）
        mcp_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(mcp_servers)")}
        for column in ("kind", "proposal_id"):
            if column not in mcp_cols:
                default = "tool" if column == "kind" else ""
                self._conn.execute(f"ALTER TABLE mcp_servers ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        grant_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(demo_grants)").fetchall()}
        if "kind" not in grant_cols:
            self._conn.execute("ALTER TABLE demo_grants ADD COLUMN kind TEXT NOT NULL DEFAULT 'invite'")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_demo_grants_kind ON demo_grants(tenant_id, kind, status)")
        # 保守归属回填：仅当审计 asset.registered 事件可对应到该 asset 时回填 subject；
        # 无审计证据的旧资产保持 subject=''（列表不可见、读取拒绝），不默认赋给 owner。
        self._conn.execute(
            "UPDATE assets SET subject = COALESCE(("
            "  SELECT a.subject FROM audit_log a"
            "  WHERE a.kind='asset.registered'"
            "    AND json_extract(a.detail_json, '$.asset_id') = assets.id"
            "  ORDER BY a.seq LIMIT 1), '')"
            " WHERE subject=''"
        )

    @contextlib.contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """FTS5 虚表等 SQLite 专属能力的受控出口：持 store 锁访问连接（P1-4 跨线程事务隔离）。"""
        with self._lock:
            yield self._conn

    def raw_connection(self) -> sqlite3.Connection:
        """仅初始化 schema 用；运行期一律走 locked()。"""
        return self._conn

    def inbox_record(self, tenant_id: str, *, provider: str, event_id: str, payload_hash: str, payload: str) -> None:
        """系统内待办记录（通知备通道；最终可查询，FR-EDG-03）。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO inbox(tenant_id,provider,event_id,payload_hash,processed_at) VALUES(?,?,?,?,?)",
                (tenant_id, provider, event_id, payload_hash, _now()),
            )
            self._conn.commit()

    def audit_verify_chain(self, tenant_id: str) -> dict[str, Any]:
        """FR-GOV-03：校验哈希链完整性（M5 基线：篡改/截断可发现）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, subject, run_id, detail_json, created_at, prev_hash, content_hash"
                " FROM audit_log WHERE tenant_id=? ORDER BY seq",
                (tenant_id,),
            ).fetchall()
            prev = ""
            for row in rows:
                expected = hashlib.sha256(
                    f"{prev}|{row['seq']}|{row['kind']}|{row['subject']}|{row['run_id']}|"
                    f"{row['detail_json']}|{row['created_at']}".encode()
                ).hexdigest()
                if row["prev_hash"] != prev or row["content_hash"] != expected:
                    return {"valid": False, "broken_at_seq": row["seq"]}
                prev = row["content_hash"]
            return {"valid": True, "entries": len(rows)}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ util
    def _tx(self) -> sqlite3.Connection:
        return self._conn

    @staticmethod
    def _ensure(rows: int, what: str) -> None:
        if rows == 0:
            raise AgentOSError(ErrorCode.CONFLICT, f"{what}: conditional update matched 0 rows")

    # ------------------------------------------------------------- accept_once
    def accept_once(
        self,
        envelope_json: str,
        envelope_id: str,
        idempotency_key: str,
        payload_hash: str,
        tenant_id: str,
        subject: str,
        channel: str,
        run_budget_microusd: int,
        attachment_ids: list[str],
        parent_run_id: str | None = None,
        parent_reservation_id: str | None = None,
    ) -> ReceiptModel:
        now = _now()
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        receipt = ReceiptModel(run_id=run_id, job_id=job_id, accepted=True)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                # FK 顺序：runs → idempotency → jobs → attachments → events/outbox
                self._conn.execute(
                    "INSERT INTO runs(tenant_id,id,subject,state,version,channel,thread_id,goal_text,"
                    "envelope_json,budget_microusd,created_at,updated_at) "
                    "VALUES(?,?,?,?,1,?,?,?,?,?,?,?)",
                    (
                        tenant_id,
                        run_id,
                        subject,
                        RunState.CREATED,
                        channel,
                        json.loads(envelope_json).get("thread_id"),
                        str(json.loads(envelope_json).get("payload", {}).get("goal", "")),
                        envelope_json,
                        run_budget_microusd,
                        now,
                        now,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO idempotency(tenant_id,subject,channel,request_key,payload_hash,"
                    "run_id,receipt_json,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        tenant_id,
                        subject,
                        channel,
                        idempotency_key,
                        payload_hash,
                        run_id,
                        receipt.model_dump_json(),
                        (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                    ),
                )
                if parent_run_id is not None:
                    parent = self._conn.execute(
                        "SELECT subject,state,channel,budget_microusd FROM runs WHERE tenant_id=? AND id=?",
                        (tenant_id, parent_run_id)).fetchone()
                    if (not parent or parent["subject"] != subject or parent["channel"] == "workflow"
                            or parent["state"] != RunState.RUNNING or not parent_reservation_id):
                        raise AgentOSError(ErrorCode.DENIED, "invalid workflow delegation parent")
                    used = self._conn.execute(
                        "SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_microusd "
                        "WHEN state='reserved' THEN reserved_microusd ELSE 0 END),0) FROM budget_reservations "
                        "WHERE tenant_id=? AND run_id=?", (tenant_id, parent_run_id)).fetchone()[0]
                    if used + run_budget_microusd > parent["budget_microusd"]:
                        raise AgentOSError(ErrorCode.BUDGET_EXCEEDED, "workflow allocation exceeds parent budget")
                    self._conn.execute(
                        "INSERT INTO budget_reservations(tenant_id,call_id,run_id,reserved_microusd,state) "
                        "VALUES(?,?,?,?,'reserved')",
                        (tenant_id, parent_reservation_id, parent_run_id, run_budget_microusd))
                    self._conn.execute("INSERT INTO workflow_delegations VALUES(?,?,?,?)",
                        (tenant_id, run_id, parent_run_id, parent_reservation_id))
                self._conn.execute(
                    "INSERT INTO jobs(tenant_id,id,run_id,state,available_at) VALUES(?,?,?,?,?)",
                    (tenant_id, job_id, run_id, "ready", now),
                )
                for att in attachment_ids:
                    self._conn.execute(
                        "UPDATE attachments SET run_id=? WHERE tenant_id=? AND id=?",
                        (run_id, tenant_id, att),
                    )
                self._event_locked(tenant_id, run_id, "run.created", {"channel": channel})
                self._outbox_locked(
                    tenant_id,
                    f"run.created:{run_id}",
                    "run.created",
                    json.dumps({"run_id": run_id, "subject": subject}),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                existing = self.find_idempotent_receipt(tenant_id, subject, channel, idempotency_key)
                if existing is not None:
                    prior_hash, receipt_json = existing
                    if prior_hash == payload_hash:
                        prior = ReceiptModel.model_validate_json(receipt_json)
                        return ReceiptModel(run_id=prior.run_id, job_id=prior.job_id, accepted=True, replayed=True)
                raise AgentOSError(ErrorCode.CONFLICT, "accept failed: concurrent idempotency conflict") from exc
            except Exception:
                self._conn.rollback()
                raise
        return receipt

    def find_idempotent_receipt(
        self, tenant_id: str, subject: str, channel: str, idempotency_key: str
    ) -> tuple[str, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_hash, receipt_json FROM idempotency "
                "WHERE tenant_id=? AND subject=? AND channel=? AND request_key=?",
                (tenant_id, subject, channel, idempotency_key),
            ).fetchone()
        return (row["payload_hash"], row["receipt_json"]) if row else None

    # --------------------------------------------------------------- run/step
    def get_run(self, tenant_id: str, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE tenant_id=? AND id=?", (tenant_id, run_id)).fetchone()
        return _row_to_run(row) if row else None

    def list_runs(self, tenant_id: str, subject: str | None = None) -> list[RunRecord]:
        with self._lock:
            if subject is None:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE tenant_id=? ORDER BY created_at DESC", (tenant_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE tenant_id=? AND subject=? ORDER BY created_at DESC",
                    (tenant_id, subject),
                ).fetchall()
        return [_row_to_run(r) for r in rows]

    def transition_run(
        self,
        tenant_id: str,
        run_id: str,
        from_states: set[RunState],
        to_state: RunState,
        *,
        expected_version: int | None = None,
        error_json: str | None = None,
        event_detail: dict[str, Any] | None = None,
    ) -> RunRecord | None:
        now = _now()
        from_list = sorted(s.value for s in from_states)
        placeholders = ",".join("?" * len(from_list))
        sql = (
            f"UPDATE runs SET state=?, updated_at=?, version=version+1, error_json=COALESCE(?,error_json) "
            f"{', cancel_requested_at=?' if to_state == RunState.CANCEL_REQUESTED else ''} "
            f"WHERE tenant_id=? AND id=? AND state IN ({placeholders})"
        )
        params: list[Any] = [to_state.value, now, error_json]
        if to_state == RunState.CANCEL_REQUESTED:
            params.append(now)
        params += [tenant_id, run_id, *from_list]
        if expected_version is not None:
            sql += " AND version=?"
            params.append(expected_version)
        with self._lock:
            cur = self._conn.execute(sql, params)
            if cur.rowcount == 0:
                self._conn.commit()
                return None
            self._event_locked(tenant_id, run_id, "run.state_changed", {"to": to_state.value, **(event_detail or {})})
            if to_state in RUN_TERMINAL:
                delegation = self._conn.execute(
                    "SELECT reservation_id FROM workflow_delegations WHERE tenant_id=? AND child_run_id=?",
                    (tenant_id, run_id)).fetchone()
                if delegation:
                    # Outstanding usage is conservatively charged, never freed on an unknown effect.
                    used = self._conn.execute(
                        "SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_microusd "
                        "WHEN state='reserved' THEN reserved_microusd ELSE 0 END),0) FROM budget_reservations "
                        "WHERE tenant_id=? AND run_id=?", (tenant_id, run_id)).fetchone()[0]
                    self._conn.execute(
                        "UPDATE budget_reservations SET state='settled', settled_microusd=? "
                        "WHERE tenant_id=? AND call_id=? AND state='reserved'",
                        (used, tenant_id, delegation["reservation_id"]))
            self._conn.commit()
            return self.get_run(tenant_id, run_id)

    def request_cancel(self, tenant_id: str, run_id: str) -> RunRecord | None:
        run = self.get_run(tenant_id, run_id)
        if run is None:
            return None
        if run.state in RUN_TERMINAL:
            return run
        updated = self.transition_run(tenant_id, run_id, set(RunState) - RUN_TERMINAL, RunState.CANCEL_REQUESTED)
        if updated is not None:
            self.enqueue_job(tenant_id, run_id)  # 唤醒 worker 处理取消
            return updated
        return self.get_run(tenant_id, run_id)

    def create_step(self, step: StepRecord) -> StepRecord:
        with self._lock:
            self._conn.execute(
                "INSERT INTO steps(tenant_id,run_id,id,call_id,tool_name,args_hash,input_ref,state,"
                "fence,attempt) VALUES(?,?,?,?,?,?,?,?,0,0)",
                (
                    step.tenant_id,
                    step.run_id,
                    step.id,
                    step.call_id,
                    step.tool_name,
                    step.args_hash,
                    step.input_ref,
                    step.state.value,
                ),
            )
            self._conn.commit()
        return step

    def prepare_once(
        self,
        tenant_id: str,
        run_id: str,
        step_id: str,
        tool_name: str,
        args_hash: str,
        fence: int,
    ) -> StepRecord:
        """同一步重试沿用 ID（FR-ORC-08）：已存在则原样返回，不覆盖结果。"""
        call_id = f"{run_id}:{step_id}"
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM steps WHERE tenant_id=? AND run_id=? AND id=?",
                (tenant_id, run_id, step_id),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO steps(tenant_id,run_id,id,call_id,tool_name,args_hash,state,fence)"
                    " VALUES(?,?,?,?,?,?,'prepared',0)",
                    (tenant_id, run_id, step_id, call_id, tool_name, args_hash),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM steps WHERE tenant_id=? AND run_id=? AND id=?",
                    (tenant_id, run_id, step_id),
                ).fetchone()
        return _row_to_step(row)

    def get_step(self, tenant_id: str, run_id: str, step_id: str) -> StepRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM steps WHERE tenant_id=? AND run_id=? AND id=?",
                (tenant_id, run_id, step_id),
            ).fetchone()
        return _row_to_step(row) if row else None

    def list_steps(self, tenant_id: str, run_id: str) -> list[StepRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE tenant_id=? AND run_id=? ORDER BY rowid", (tenant_id, run_id)
            ).fetchall()
        return [_row_to_step(r) for r in rows]

    def transition_step(
        self,
        tenant_id: str,
        run_id: str,
        step_id: str,
        from_states: set[StepState],
        to_state: StepState,
        *,
        expected_fence: int | None = None,
        result_json: str | None = None,
        error_json: str | None = None,
        external_ref: str | None = None,
        approval_id: str | None = None,
    ) -> StepRecord | None:
        from_list = sorted(s.value for s in from_states)
        placeholders = ",".join("?" * len(from_list))
        finished = ", finished_at=?" if to_state in STEP_TERMINAL else ""
        params: list[Any] = []
        set_cols = "state=?"
        params.append(to_state.value)
        if result_json is not None:
            set_cols += ", result_json=?"
            params.append(result_json)
        if error_json is not None:
            set_cols += ", error_json=?"
            params.append(error_json)
        if external_ref is not None:
            set_cols += ", external_ref=?"
            params.append(external_ref)
        if approval_id is not None:
            set_cols += ", approval_id=?"
            params.append(approval_id)
        if to_state in STEP_TERMINAL:
            params.append(_now())
        sql = (
            f"UPDATE steps SET {set_cols}{finished} WHERE tenant_id=? AND run_id=? AND id=? "
            f"AND state IN ({placeholders})"
        )
        params += [tenant_id, run_id, step_id, *from_list]
        if expected_fence is not None:
            sql += " AND fence=?"
            params.append(expected_fence)
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM steps WHERE tenant_id=? AND run_id=? AND id=?",
                (tenant_id, run_id, step_id),
            ).fetchone()
        step = _row_to_step(row)
        self._event(tenant_id, run_id, "step.state_changed", {"step_id": step_id, "to": to_state.value})
        return step

    def commit_dispatch(
        self,
        tenant_id: str,
        run_id: str,
        step_id: str,
        fence: int,
        approval_id: str | None,
        expected_args_hash: str | None = None,
    ) -> StepRecord:
        """原子：消费批准（CAS）+ 步骤 prepared/awaiting_approval→dispatched + 审计 + 事件。

        批准缺失/过期/参数不符时整体失败，步骤不进入 dispatched（FR-GOV-02）。
        """
        call_id = f"{run_id}:{step_id}"
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                run_lease = self._conn.execute(
                    "SELECT lease_fence, lease_until, lease_owner FROM runs WHERE tenant_id=? AND id=?",
                    (tenant_id, run_id),
                ).fetchone()
                if run_lease and run_lease["lease_fence"] > 0 and (
                    run_lease["lease_fence"] != fence or not run_lease["lease_owner"]
                    or not run_lease["lease_until"] or run_lease["lease_until"] <= _now()
                ):
                    raise AgentOSError(ErrorCode.CONFLICT, "dispatch rejected: run lease expired or superseded")
                if approval_id is not None:
                    cur = self._conn.execute(
                        "UPDATE approvals SET state='consumed', decided_at=COALESCE(decided_at,?) "
                        "WHERE tenant_id=? AND id=? AND state='approved' AND expires_at>? "
                        "AND (? IS NULL OR args_hash=?)",
                        (_now(), tenant_id, approval_id, _now(), expected_args_hash, expected_args_hash),
                    )
                    if cur.rowcount == 0:
                        self._conn.rollback()
                        raise AgentOSError(
                            ErrorCode.DENIED,
                            "approval invalid: missing/expired/param-mismatch/not-approved",
                        )
                cur = self._conn.execute(
                    "UPDATE steps SET state='dispatched', fence=?, approval_id=?, started_at=? "
                    "WHERE tenant_id=? AND run_id=? AND id=? AND state IN ('prepared','awaiting_approval')",
                    (fence, approval_id, _now(), tenant_id, run_id, step_id),
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    raise AgentOSError(ErrorCode.CONFLICT, "dispatch raced with another worker")
                self._audit_locked(
                    tenant_id,
                    "tool.dispatched",
                    subject=None,
                    run_id=run_id,
                    detail={"step_id": step_id, "call_id": call_id, "fence": fence},
                )
                self._event_locked(tenant_id, run_id, "step.state_changed", {"step_id": step_id, "to": "dispatched"})
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            row = self._conn.execute(
                "SELECT * FROM steps WHERE tenant_id=? AND run_id=? AND id=?",
                (tenant_id, run_id, step_id),
            ).fetchone()
        return _row_to_step(row)

    def resume_with_checkpoint(
        self, tenant_id: str, run_id: str, expected_version: int, checkpoint_json: str, event_detail: dict[str, Any]
    ) -> bool:
        """CAS supplement acceptance, checkpoint, event and wake-up in one transaction."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "UPDATE runs SET state='running', checkpoint_json=?, version=version+1, updated_at=? "
                    "WHERE tenant_id=? AND id=? AND version=? AND state='awaiting_input'",
                    (checkpoint_json, _now(), tenant_id, run_id, expected_version),
                )
                if cur.rowcount != 1:
                    self._conn.rollback()
                    return False
                self._event_locked(tenant_id, run_id, "plan.rework_accepted", event_detail)
                self._event_locked(tenant_id, run_id, "run.state_changed", {"to": "running"})
                self._conn.execute(
                    "INSERT INTO jobs(tenant_id,id,run_id,state,available_at) VALUES(?,?,?,?,?)",
                    (tenant_id, f"job_{uuid.uuid4().hex}", run_id, "ready", _now()),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def resume_runtime(self, tenant_id: str, run_id: str, expected_version: int, subject: str) -> bool:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "UPDATE runs SET state='running', error_json=NULL, version=version+1, updated_at=? "
                    "WHERE tenant_id=? AND id=? AND version=? AND state='paused' "
                    "AND NOT EXISTS (SELECT 1 FROM steps WHERE tenant_id=? AND run_id=? "
                    "AND state IN ('uncertain','dispatched','pending_external','awaiting_approval'))",
                    (_now(), tenant_id, run_id, expected_version, tenant_id, run_id),
                )
                if cur.rowcount != 1:
                    self._conn.rollback()
                    return False
                self._event_locked(tenant_id, run_id, "runtime.resume_requested", {"subject": subject})
                self._event_locked(tenant_id, run_id, "run.state_changed", {"to": "running"})
                self._conn.execute(
                    "INSERT INTO jobs(tenant_id,id,run_id,state,available_at) VALUES(?,?,?,?,?)",
                    (tenant_id, f"job_{uuid.uuid4().hex}", run_id, "ready", _now()),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    # ----------------------------------------------------------- lease & jobs
    def claim_run_lease(self, tenant_id: str, run_id: str, owner: str, ttl_s: int) -> Lease | None:
        now_iso = _now()
        until = (datetime.now(UTC) + timedelta(seconds=ttl_s)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE runs SET lease_owner=?, lease_until=?, lease_fence=lease_fence+1, "
                "version=version+1, updated_at=? "
                "WHERE tenant_id=? AND id=? AND (lease_until IS NULL OR lease_until<? OR lease_owner=?)",
                (owner, until, now_iso, tenant_id, run_id, now_iso, owner),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT lease_fence, lease_until FROM runs WHERE tenant_id=? AND id=?", (tenant_id, run_id)
            ).fetchone()
        return Lease(run_id=run_id, owner=owner, fence=row["lease_fence"],
                     until=_dt(row["lease_until"]) or utc_now())

    def renew_run_lease(self, tenant_id: str, run_id: str, owner: str, fence: int, ttl_s: int) -> bool:
        until = (datetime.now(UTC) + timedelta(seconds=ttl_s)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE runs SET lease_until=? WHERE tenant_id=? AND id=? "
                "AND lease_owner=? AND lease_fence=? AND lease_until>?",
                (until, tenant_id, run_id, owner, fence, _now()),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def release_run_lease(self, tenant_id: str, run_id: str, owner: str) -> None:
        # owner 已唯一化（进程级），按 owner 释放即安全；version 条件会让 release 永远失败（P1-6）
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET lease_owner=NULL, lease_until=NULL WHERE tenant_id=? AND id=? AND lease_owner=?",
                (tenant_id, run_id, owner),
            )
            self._conn.commit()

    def enqueue_job(self, tenant_id: str, run_id: str) -> str:
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(tenant_id,id,run_id,state,available_at) VALUES(?,?,?,?,?)",
                (tenant_id, job_id, run_id, "ready", _now()),
            )
            self._conn.commit()
        return job_id

    def claim_job_for_run(self, tenant_id: str, run_id: str, owner: str, ttl_s: int) -> JobRecord | None:
        now_iso = _now()
        until = (datetime.now(UTC) + timedelta(seconds=ttl_s)).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE tenant_id=? AND run_id=? AND (state='ready' OR "
                "(state='claimed' AND lease_until<?)) ORDER BY available_at LIMIT 1",
                (tenant_id, run_id, now_iso),
            ).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "UPDATE jobs SET state='claimed', lease_owner=?, lease_until=?, fence=fence+1, "
                "attempts=attempts+1 WHERE tenant_id=? AND id=? AND fence=?",
                (owner, until, tenant_id, row["id"], row["fence"]),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM jobs WHERE tenant_id=? AND id=?", (tenant_id, row["id"])).fetchone()
        return JobRecord(
            tenant_id=row["tenant_id"],
            id=row["id"],
            run_id=row["run_id"],
            state=row["state"],
            available_at=_dt(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_until=_dt(row["lease_until"]),
            fence=row["fence"],
            attempts=row["attempts"],
        )

    def save_checkpoint(self, tenant_id: str, run_id: str, checkpoint_json: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE runs SET checkpoint_json=? WHERE tenant_id=? AND id=?",
                (checkpoint_json, tenant_id, run_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def list_job_tenants(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT tenant_id FROM jobs WHERE state='ready' "
                "OR (state='claimed' AND lease_until<?) ORDER BY tenant_id", (_now(),)
            ).fetchall()
        return [str(row["tenant_id"]) for row in rows]

    def claim_job(self, tenant_id: str, owner: str, ttl_s: int) -> JobRecord | None:
        now_iso = _now()
        until = (datetime.now(UTC) + timedelta(seconds=ttl_s)).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE tenant_id=? AND (state='ready' OR (state='claimed' AND lease_until<?)) "
                "ORDER BY available_at LIMIT 1",
                (tenant_id, now_iso),
            ).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "UPDATE jobs SET state='claimed', lease_owner=?, lease_until=?, fence=fence+1, "
                "attempts=attempts+1 WHERE tenant_id=? AND id=? AND fence=?",
                (owner, until, tenant_id, row["id"], row["fence"]),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM jobs WHERE tenant_id=? AND id=?", (tenant_id, row["id"])).fetchone()
        return JobRecord(
            tenant_id=row["tenant_id"],
            id=row["id"],
            run_id=row["run_id"],
            state=row["state"],
            available_at=_dt(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_until=_dt(row["lease_until"]),
            fence=row["fence"],
            attempts=row["attempts"],
        )

    def complete_job(self, tenant_id: str, job_id: str, owner: str, fence: int, state: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET state=? WHERE tenant_id=? AND id=? AND lease_owner=? AND fence=?",
                (state, tenant_id, job_id, owner, fence),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------------- approvals
    def request_approval_once(self, approval: ApprovalRecord) -> ApprovalRecord:
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND call_id=?",
                (approval.tenant_id, approval.call_id),
            ).fetchone()
            if existing is not None:
                return _row_to_approval(existing)
            self._conn.execute(
                "INSERT INTO approvals(tenant_id,id,call_id,subject,args_hash,policy_version,state,"
                "expires_at,summary) VALUES(?,?,?,?,?,?,'pending',?,?)",
                (
                    approval.tenant_id,
                    approval.id,
                    approval.call_id,
                    approval.subject,
                    approval.args_hash,
                    approval.policy_version,
                    approval.expires_at.isoformat(),
                    approval.summary,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND id=?",
                (approval.tenant_id, approval.id),
            ).fetchone()
        created = _row_to_approval(row)
        self.audit_append(
            approval.tenant_id,
            "approval.requested",
            subject=approval.subject,
            run_id=None,
            detail={
                "approval_id": approval.id,
                "call_id": approval.call_id,
                "args_hash": approval.args_hash,
                "summary": approval.summary,
            },
        )
        return created

    def decide_approval(
        self,
        tenant_id: str,
        approval_id: str,
        decision: str,
        decided_by: str,
    ) -> ApprovalRecord | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET state=?, decided_by=?, decided_at=? "
                "WHERE tenant_id=? AND id=? AND state='pending' AND expires_at>?",
                (decision, decided_by, _now(), tenant_id, approval_id, _now()),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND id=?", (tenant_id, approval_id)
            ).fetchone()
        record = _row_to_approval(row)
        self.audit_append(
            tenant_id,
            f"approval.{decision}",
            subject=decided_by,
            run_id=None,
            detail={"approval_id": approval_id, "call_id": record.call_id},
        )
        return record

    def consume_approval(self, tenant_id: str, approval_id: str, expected_args_hash: str) -> ApprovalRecord | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET state='consumed' WHERE tenant_id=? AND id=? "
                "AND state='approved' AND expires_at>? AND args_hash=?",
                (tenant_id, approval_id, _now(), expected_args_hash),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND id=?", (tenant_id, approval_id)
            ).fetchone()
        return _row_to_approval(row)

    def get_approval(self, tenant_id: str, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND id=?", (tenant_id, approval_id)
            ).fetchone()
        return _row_to_approval(row) if row else None

    def list_approvals(self, tenant_id: str, state: ApprovalState | None = None) -> list[ApprovalRecord]:
        with self._lock:
            if state is None:
                rows = self._conn.execute(
                    "SELECT * FROM approvals WHERE tenant_id=? ORDER BY rowid", (tenant_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM approvals WHERE tenant_id=? AND state=? ORDER BY rowid",
                    (tenant_id, state.value),
                ).fetchall()
        return [_row_to_approval(r) for r in rows]

    # ------------------------------------------------------------------ grants
    def list_grants(self, tenant_id: str, subject: str) -> list[PolicyGrant]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM grants WHERE tenant_id=? AND subject=?", (tenant_id, subject)
            ).fetchall()
        return [
            PolicyGrant(
                id=r["id"],
                tenant_id=r["tenant_id"],
                subject=r["subject"],
                tool_name=r["tool_name"],
                risks=json.loads(r["risks_json"]),
                expires_at=_dt(r["expires_at"]) or utc_now(),
                issued_by=r["issued_by"],
                revoked=bool(r["revoked"]),
            )
            for r in rows
        ]

    def upsert_grant(self, grant: PolicyGrant) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO grants(tenant_id,id,subject,tool_name,risks_json,expires_at,issued_by,revoked)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,id) DO UPDATE SET revoked=excluded.revoked",
                (
                    grant.tenant_id,
                    grant.id,
                    grant.subject,
                    grant.tool_name,
                    json.dumps(grant.risks),
                    grant.expires_at.isoformat(),
                    grant.issued_by,
                    int(grant.revoked),
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------ audit/events
    def audit_append(
        self,
        tenant_id: str,
        kind: str,
        subject: str | None,
        run_id: str | None,
        detail: dict[str, Any],
    ) -> AuditEvent:
        with self._lock:
            event = self._audit_locked(tenant_id, kind, subject, run_id, detail)
            self._conn.commit()
        return event

    def _audit_locked(
        self,
        tenant_id: str,
        kind: str,
        subject: str | None,
        run_id: str | None,
        detail: dict[str, Any],
    ) -> AuditEvent:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM audit_log WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        seq = row["next"]
        now = _now()
        prev_row = self._conn.execute(
            "SELECT content_hash FROM audit_log WHERE tenant_id=? ORDER BY seq DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        prev_hash = prev_row["content_hash"] if prev_row else ""
        detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(
            f"{prev_hash}|{seq}|{kind}|{subject}|{run_id}|{detail_json}|{now}".encode()
        ).hexdigest()
        self._conn.execute(
            "INSERT INTO audit_log(tenant_id,seq,kind,subject,run_id,detail_json,created_at,"
            "prev_hash,content_hash) VALUES(?,?,?,?,?,?,?,?,?)",
            (tenant_id, seq, kind, subject, run_id, detail_json, now, prev_hash, content_hash),
        )
        return AuditEvent(
            seq=seq,
            tenant_id=tenant_id,
            kind=kind,
            subject=subject,
            run_id=run_id,
            detail=detail,
            created_at=_dt(now),
        )

    def audit_query(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        kind_prefix: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        sql = "SELECT * FROM audit_log WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(run_id)
        if kind_prefix is not None:
            sql += " AND kind LIKE ?"
            params.append(f"{kind_prefix}%")
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            AuditEvent(
                seq=r["seq"],
                tenant_id=r["tenant_id"],
                kind=r["kind"],
                subject=r["subject"],
                run_id=r["run_id"],
                detail=json.loads(r["detail_json"]),
                created_at=_dt(r["created_at"]),
            )
            for r in rows
        ]

    def event_append(self, tenant_id: str, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        with self._lock:
            seq = self._event_locked(tenant_id, run_id, kind, payload)
            self._conn.commit()
        return seq

    def _event_locked(self, tenant_id: str, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next FROM events WHERE tenant_id=? AND run_id=?",
            (tenant_id, run_id),
        ).fetchone()
        seq = row["next"]
        self._conn.execute(
            "INSERT INTO events(tenant_id,run_id,seq,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (tenant_id, run_id, seq, kind, json.dumps(payload, ensure_ascii=False), _now()),
        )
        return int(seq)

    def _event(self, tenant_id: str, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        with self._lock:
            seq = self._event_locked(tenant_id, run_id, kind, payload)
            self._conn.commit()
        return seq

    def event_list(self, tenant_id: str, run_id: str, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, payload_json, created_at FROM events "
                "WHERE tenant_id=? AND run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (tenant_id, run_id, after_seq, limit),
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def event_high_water(self, tenant_id: str, run_id: str) -> int:
        """当前事件高水位（最大 seq，无事件为 0）：默认发布据此冻结游客可见快照。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS hw FROM events WHERE tenant_id=? AND run_id=?",
                (tenant_id, run_id),
            ).fetchone()
        return int(row["hw"])

    # ------------------------------------------------------------------ outbox
    def outbox_append(self, tenant_id: str, event_key: str, topic: str, payload_json: str) -> None:
        with self._lock:
            self._outbox_locked(tenant_id, event_key, topic, payload_json)
            self._conn.commit()

    def _outbox_locked(self, tenant_id: str, event_key: str, topic: str, payload_json: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO outbox(tenant_id,id,event_key,topic,payload_json) VALUES(?,?,?,?,?)",
            (tenant_id, f"ob_{uuid.uuid4().hex[:16]}", event_key, topic, payload_json),
        )

    def outbox_drain(self, tenant_id: str, limit: int = 50) -> list[OutboxEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE tenant_id=? AND delivered_at IS NULL ORDER BY rowid LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
            events = [
                OutboxEvent(
                    tenant_id=r["tenant_id"],
                    id=r["id"],
                    event_key=r["event_key"],
                    topic=r["topic"],
                    payload_json=r["payload_json"],
                    delivered_at=None,
                )
                for r in rows
            ]
            if events:
                self._conn.execute(
                    "UPDATE outbox SET delivered_at=? WHERE tenant_id=? AND delivered_at IS NULL "
                    f"AND id IN ({','.join('?' * len(events))})",
                    (_now(), tenant_id, *[e.id for e in events]),
                )
            self._conn.commit()
        return events

    # ------------------------------------------------------------- attachments
    def insert_attachment(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO attachments(tenant_id,id,owner,display_name,storage_path,size_bytes,"
                "sha256,content_type,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record["tenant_id"],
                    record["id"],
                    record["owner"],
                    record["display_name"],
                    record["storage_path"],
                    record["size_bytes"],
                    record["sha256"],
                    record["content_type"],
                    _now(),
                ),
            )
            self._conn.commit()

    def upsert_mcp_server(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO mcp_servers(tenant_id,id,transport,command,url,env_json,risks_json,"
                "protocol_version,enabled,tools_json,registered_at) VALUES(?,?,?,?,?,?,?,?,1,'[]',?) "
                "ON CONFLICT(tenant_id,id) DO UPDATE SET transport=excluded.transport,"
                "command=excluded.command,url=excluded.url,env_json=excluded.env_json,"
                "risks_json=excluded.risks_json,enabled=excluded.enabled,proposal_id=''",
                (
                    record["tenant_id"],
                    record["id"],
                    record["transport"],
                    record["command"],
                    record["url"],
                    record["env_json"],
                    record["risks_json"],
                    record.get("protocol_version"),
                    _now(),
                ),
            )
            self._conn.commit()

    def list_mcp_servers(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mcp_servers WHERE tenant_id=? ORDER BY id", (tenant_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_mcp_server(self, tenant_id: str, server_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mcp_servers WHERE tenant_id=? AND id=?", (tenant_id, server_id)
            ).fetchone()
        return dict(row) if row else None

    def update_mcp_server_discovery(
        self, tenant_id: str, server_id: str, *, protocol_version: str, tools_json: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE mcp_servers SET protocol_version=?, tools_json=? WHERE tenant_id=? AND id=?",
                (protocol_version, tools_json, tenant_id, server_id),
            )
            self._conn.commit()

    def delete_mcp_server(self, tenant_id: str, server_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM mcp_servers WHERE tenant_id=? AND id=?", (tenant_id, server_id))
            self._conn.commit()

    def list_drafts(self, tenant_id: str, subject: str, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM resource_drafts WHERE tenant_id=? AND subject=? AND kind=? AND payload_json!='null' "
                "ORDER BY updated_at DESC", (tenant_id, subject, kind))]

    def list_connection_drafts(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM resource_drafts WHERE tenant_id=? AND kind='mcp' AND payload_json!='null' "
                "ORDER BY updated_at DESC",
                (tenant_id,))]

    def insert_mcp_proposal(self, record: dict[str, Any]) -> bool:
        with self._lock, self._conn:
            return self._conn.execute(
                "INSERT OR IGNORE INTO mcp_servers(tenant_id,id,transport,command,url,env_json,risks_json,"
                "enabled,registered_at,kind,proposal_id) VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                (record["tenant_id"], record["id"], record["transport"], record["command"], record["url"],
                 record["env_json"], record["risks_json"], _now(), record["kind"], record["proposal_id"])
            ).rowcount == 1

    def get_draft(self, tenant_id: str, subject: str, kind: str, id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resource_drafts WHERE tenant_id=? AND subject=? AND kind=? AND id=? "
                "AND payload_json!='null'",
                (tenant_id, subject, kind, id)).fetchone()
            return dict(row) if row else None

    def save_draft(self, tenant_id: str, subject: str, kind: str, id: str,
                   payload_json: str, expected_revision: int) -> bool:
        with self._lock, self._conn:
            if expected_revision == 0:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO resource_drafts VALUES(?,?,?,?,?,1,?)",
                    (tenant_id, subject, kind, id, payload_json, _now()))
            else:
                cursor = self._conn.execute(
                    "UPDATE resource_drafts SET payload_json=?, revision=revision+1, updated_at=? "
                    "WHERE tenant_id=? AND subject=? AND kind=? AND id=? AND revision=? AND payload_json!='null'",
                    (payload_json, _now(), tenant_id, subject, kind, id, expected_revision))
            return cursor.rowcount == 1

    def delete_draft(self, tenant_id: str, subject: str, kind: str, id: str,
                     expected_revision: int) -> bool:
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE resource_drafts SET payload_json='null', revision=revision+1 "
                "WHERE tenant_id=? AND subject=? AND kind=? AND id=? AND revision=? AND payload_json!='null'",
                (tenant_id, subject, kind, id, expected_revision)).rowcount == 1

    def next_workflow_version_atomic(self, tenant_id: str, workflow_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS next FROM workflow_definitions WHERE tenant_id=? AND id=?",
                (tenant_id, workflow_id),
            ).fetchone()
            return int(row["next"])

    def next_workflow_version(self, tenant_id: str, workflow_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS next FROM workflow_definitions WHERE tenant_id=? AND id=?",
                (tenant_id, workflow_id),
            ).fetchone()
            return int(row["next"])
        # P2-13：调用方必须在同一持锁窗口内完成 INSERT（next+insert 分离会竞态双写）

    def insert_workflow_definition(
        self,
        *,
        tenant_id: str,
        id: str,
        version: int,
        name: str,
        definition_json: str,
        definition_hash: str,
        status: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_definitions(tenant_id,id,version,name,definition_json,"
                "definition_hash,status,published_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (tenant_id, id, version, name, definition_json, definition_hash, status, _now(), _now()),
            )
            self._conn.commit()

    def find_workflow_by_hash(self, tenant_id: str, workflow_id: str, definition_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_definitions WHERE tenant_id=? AND id=? AND definition_hash=?",
                (tenant_id, workflow_id, definition_hash),
            ).fetchone()
        return dict(row) if row else None

    def get_workflow_definition(self, tenant_id: str, workflow_id: str, version: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_definitions WHERE tenant_id=? AND id=? AND version=?",
                (tenant_id, workflow_id, version),
            ).fetchone()
        return dict(row) if row else None

    def insert_workflow_run(
        self,
        *,
        tenant_id: str,
        id: str,
        workflow_id: str,
        definition_version: int,
        internal_run_id: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_runs(tenant_id,id,workflow_id,definition_version,"
                "internal_run_id,checkpoint_owner,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'agentos','running',?,?)",
                (tenant_id, id, workflow_id, definition_version, internal_run_id, _now(), _now()),
            )
            self._conn.commit()

    def get_workflow_run(self, tenant_id: str, wf_run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE tenant_id=? AND id=?", (tenant_id, wf_run_id)
            ).fetchone()
        return dict(row) if row else None

    def list_workflow_definitions(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,version,name,definition_hash,status FROM workflow_definitions "
                "WHERE tenant_id=? ORDER BY id,version DESC", (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_workflow_runs(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE tenant_id=? ORDER BY created_at DESC",
                (tenant_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def gc_attachments(self, tenant_id: str, ttl_minutes: int) -> list[str]:
        cutoff = (datetime.now(UTC) - timedelta(minutes=ttl_minutes)).isoformat()
        removed: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, storage_path FROM attachments WHERE tenant_id=? AND run_id IS NULL AND created_at<?",
                (tenant_id, cutoff),
            ).fetchall()
            for row in rows:
                # 文件已不存在即视为清理完成，行仍删除保持账实一致
                with contextlib.suppress(OSError):
                    Path(row["storage_path"]).unlink(missing_ok=True)
                self._conn.execute("DELETE FROM attachments WHERE tenant_id=? AND id=?", (tenant_id, row["id"]))
                removed.append(row["id"])
            self._conn.commit()
        return removed

    def expire_approval(self, tenant_id: str, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approvals SET state='expired' WHERE tenant_id=? AND id=? AND state='pending'",
                (tenant_id, approval_id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND id=?", (tenant_id, approval_id)
            ).fetchone()
        record = _row_to_approval(row)
        self.audit_append(
            tenant_id,
            "approval.expired",
            subject=record.subject,
            run_id=None,
            detail={"approval_id": record.id, "call_id": record.call_id},
        )
        return record

    def get_attachment(self, tenant_id: str, attachment_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attachments WHERE tenant_id=? AND id=?", (tenant_id, attachment_id)
            ).fetchone()
        return dict(row) if row else None

    def count_active_runs(self, tenant_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM runs WHERE tenant_id=? AND state NOT IN ('succeeded','failed','cancelled')",
                (tenant_id,),
            ).fetchone()
        return int(row["c"])

    def upsert_skill(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO skills(tenant_id,id,version,digest,description,source,install_path,"
                "allowed_tools_json,entrypoints_json,installed_at,enabled) VALUES(?,?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(tenant_id,id,version) DO UPDATE SET digest=excluded.digest,"
                "description=excluded.description,install_path=excluded.install_path,"
                "allowed_tools_json=excluded.allowed_tools_json,entrypoints_json=excluded.entrypoints_json",
                (
                    record["tenant_id"],
                    record["id"],
                    record["version"],
                    record["digest"],
                    record["description"],
                    record["source"],
                    record["install_path"],
                    record["allowed_tools_json"],
                    record["entrypoints_json"],
                    _now(),
                ),
            )
            self._conn.commit()

    def list_skills(self, tenant_id: str, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM skills WHERE tenant_id=?"
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY id, version"
        with self._lock:
            rows = self._conn.execute(sql, (tenant_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_skills(self, tenant_id: str, skill_id: str, version: str | None = None) -> None:
        with self._lock:
            if version is None:
                self._conn.execute("DELETE FROM skills WHERE tenant_id=? AND id=?", (tenant_id, skill_id))
            else:
                self._conn.execute(
                    "DELETE FROM skills WHERE tenant_id=? AND id=? AND version=?",
                    (tenant_id, skill_id, version),
                )
            self._conn.commit()

    # ------------------------------------------------------------------ budget
    def budget_reserve(self, tenant_id: str, run_id: str, call_id: str, amount_microusd: int) -> bool:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                run = self._conn.execute(
                    "SELECT budget_microusd FROM runs WHERE tenant_id=? AND id=?", (tenant_id, run_id)
                ).fetchone()
                if run is None:
                    self._conn.rollback()
                    return False
                committed_row = self._conn.execute(
                    "SELECT COALESCE(SUM(COALESCE(settled_microusd,0)),0) AS total FROM budget_reservations "
                    "WHERE tenant_id=? AND run_id=? AND state='settled'",
                    (tenant_id, run_id),
                ).fetchone()
                reserved_row = self._conn.execute(
                    "SELECT COALESCE(SUM(reserved_microusd),0) AS total FROM budget_reservations "
                    "WHERE tenant_id=? AND run_id=? AND state='reserved'",
                    (tenant_id, run_id),
                ).fetchone()
                # 已结算支出 + 未结算预留 + 本次申请 ≤ 预算（P1-3：结算金额必须计入限额）
                if committed_row["total"] + reserved_row["total"] + amount_microusd > run["budget_microusd"]:
                    self._conn.rollback()
                    return False
                existing = self._conn.execute(
                    "SELECT state FROM budget_reservations WHERE tenant_id=? AND call_id=?",
                    (tenant_id, call_id),
                ).fetchone()
                if existing is not None:
                    self._conn.commit()
                    return bool(existing["state"] == "reserved")
                self._conn.execute(
                    "INSERT INTO budget_reservations(tenant_id,call_id,run_id,reserved_microusd,state)"
                    " VALUES(?,?,?,?,'reserved')",
                    (tenant_id, call_id, run_id, amount_microusd),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def budget_settle(self, tenant_id: str, call_id: str, actual_microusd: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE budget_reservations SET settled_microusd=?, state='settled' "
                "WHERE tenant_id=? AND call_id=? AND state='reserved'",
                (actual_microusd, tenant_id, call_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def budget_release(self, tenant_id: str, call_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE budget_reservations SET state='released' WHERE tenant_id=? AND call_id=? AND state='reserved'",
                (tenant_id, call_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def budget_totals(self, tenant_id: str, run_id: str) -> dict[str, int]:
        with self._lock:
            run = self._conn.execute(
                "SELECT budget_microusd FROM runs WHERE tenant_id=? AND id=?", (tenant_id, run_id)
            ).fetchone()
            reserved = self._conn.execute(
                "SELECT COALESCE(SUM(reserved_microusd),0) AS t FROM budget_reservations "
                "WHERE tenant_id=? AND run_id=? AND state='reserved'",
                (tenant_id, run_id),
            ).fetchone()
            settled = self._conn.execute(
                "SELECT COALESCE(SUM(COALESCE(settled_microusd,0)),0) AS t FROM budget_reservations "
                "WHERE tenant_id=? AND run_id=? AND state='settled'",
                (tenant_id, run_id),
            ).fetchone()
        return {
            "budget": run["budget_microusd"] if run else 0,
            "reserved": reserved["t"],
            "settled": settled["t"],
        }

    # ------------------------------------------------------------ nodes/assets
    def pair_node(
        self,
        tenant_id: str,
        node_id: str,
        endpoint: str,
        token_hash: str,
        token_plain: str,
        *,
        os: str = "",
        arch: str = "",
        capabilities_json: str = "[]",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO nodes(tenant_id, node_id, os, arch, capabilities_json, endpoint,"
                " token_hash, token_plain, status, last_heartbeat, paired_at)"
                " VALUES(?,?,?,?,?,?,?,?,'paired',?,?)"
                " ON CONFLICT(tenant_id, node_id) DO UPDATE SET endpoint=excluded.endpoint,"
                " token_hash=excluded.token_hash, token_plain=excluded.token_plain,"
                " status='paired', os=excluded.os,"
                " arch=excluded.arch, capabilities_json=excluded.capabilities_json",
                (tenant_id, node_id, os, arch, capabilities_json, endpoint, token_hash, token_plain, _now(), _now()),
            )
            self._conn.commit()

    def get_node(self, tenant_id: str, node_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE tenant_id=? AND node_id=?", (tenant_id, node_id)
            ).fetchone()
        return dict(row) if row else None

    def list_nodes(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM nodes WHERE tenant_id=? ORDER BY node_id", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]

    def node_heartbeat(
        self,
        tenant_id: str,
        node_id: str,
        *,
        os: str,
        arch: str,
        capabilities_json: str,
        version: str = "",
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nodes SET last_heartbeat=?, os=?, arch=?, capabilities_json=?"
                " WHERE tenant_id=? AND node_id=? AND status='paired'",
                (_now(), os, arch, capabilities_json, tenant_id, node_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def revoke_node(self, tenant_id: str, node_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nodes SET status='revoked' WHERE tenant_id=? AND node_id=? AND status!='revoked'",
                (tenant_id, node_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def upsert_asset(
        self,
        tenant_id: str,
        asset_id: str,
        *,
        sha256: str,
        size: int,
        mime: str,
        source_node: str,
        path: str,
        meta_json: str = "{}",
        subject: str = "",
        run_id: str | None = None,
        name: str = "",
        version: int = 0,
        parent_asset_id: str | None = None,
        revision_reason: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO assets(tenant_id, id, sha256, size, mime, source_node, path,"
                " meta_json, created_at, subject, run_id, name, version, parent_asset_id,"
                " revision_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id, id) DO UPDATE SET sha256=excluded.sha256,"
                " size=excluded.size, mime=excluded.mime, meta_json=excluded.meta_json,"
                " run_id=excluded.run_id, name=excluded.name, version=excluded.version,"
                " parent_asset_id=excluded.parent_asset_id,"
                " revision_reason=excluded.revision_reason",
                (
                    tenant_id,
                    asset_id,
                    sha256,
                    size,
                    mime,
                    source_node,
                    path,
                    meta_json,
                    _now(),
                    subject,
                    run_id,
                    name,
                    version,
                    parent_asset_id,
                    revision_reason,
                ),
            )
            self._conn.commit()

    def get_asset(self, tenant_id: str, asset_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE tenant_id=? AND id=?", (tenant_id, asset_id)
            ).fetchone()
        return dict(row) if row else None

    def list_assets(self, tenant_id: str, subject: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if subject is None:
                rows = self._conn.execute(
                    "SELECT * FROM assets WHERE tenant_id=? ORDER BY created_at DESC",
                    (tenant_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM assets WHERE tenant_id=? AND subject=? ORDER BY created_at DESC",
                    (tenant_id, subject),
                ).fetchall()
        return [dict(r) for r in rows]

    def find_asset_by_content(
        self,
        tenant_id: str,
        subject: str,
        sha256: str,
        mime: str,
    ) -> dict[str, Any] | None:
        """同主体同内容同类型 → 复用既有素材（稳定素材 ID 的去重依据）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE tenant_id=? AND subject=? AND sha256=? AND mime=?"
                " ORDER BY created_at LIMIT 1",
                (tenant_id, subject, sha256, mime),
            ).fetchone()
        return dict(row) if row else None

    def next_asset_version(self, tenant_id: str, parent_asset_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM assets WHERE tenant_id=? AND (id=? OR parent_asset_id=?)",
                (tenant_id, parent_asset_id, parent_asset_id),
            ).fetchone()
        return int(row["v"] or 0) + 1

    def find_material_idem(
        self,
        tenant_id: str,
        subject: str,
        idempotency_key: str,
    ) -> tuple[str, str] | None:
        """返回 (content_sha256, asset_id)；同键重放/冲突判定由服务层据其裁定。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT content_sha256, asset_id FROM material_idem"
                " WHERE tenant_id=? AND subject=? AND idempotency_key=?",
                (tenant_id, subject, idempotency_key),
            ).fetchone()
        return (row["content_sha256"], row["asset_id"]) if row else None

    def insert_material_idem(
        self,
        tenant_id: str,
        subject: str,
        idempotency_key: str,
        content_sha256: str,
        asset_id: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO material_idem(tenant_id, subject, idempotency_key,"
                " content_sha256, asset_id, created_at) VALUES(?,?,?,?,?,?)",
                (tenant_id, subject, idempotency_key, content_sha256, asset_id, _now()),
            )
            self._conn.commit()

    # ---- 登录会话（auth_sessions；refresh hash 查找/轮换/撤销）----
    def insert_auth_session(
        self,
        tenant_id: str,
        subject: str,
        session_id: str,
        refresh_hash: str,
        expires_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO auth_sessions(tenant_id, subject, id, refresh_hash,"
                " expires_at, revoked, created_at) VALUES(?,?,?,?,?,0,?)",
                (tenant_id, subject, session_id, refresh_hash, expires_at, _now()),
            )
            self._conn.commit()

    def find_auth_session_by_hash(
        self,
        tenant_id: str,
        refresh_hash: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM auth_sessions WHERE tenant_id=? AND refresh_hash=? ORDER BY created_at DESC LIMIT 1",
                (tenant_id, refresh_hash),
            ).fetchone()
        return dict(row) if row else None

    def revoke_auth_session(self, tenant_id: str, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE auth_sessions SET revoked=1 WHERE tenant_id=? AND id=?",
                (tenant_id, session_id),
            )
            self._conn.commit()

    def revoke_all_auth_sessions(self, tenant_id: str, subject: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE auth_sessions SET revoked=1 WHERE tenant_id=? AND subject=?",
                (tenant_id, subject),
            )
            self._conn.commit()

    # ---- U4：演示访问（grant / manifest / invite）----
    def add_tombstone(self, tenant_id: str, resource_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO deletion_tombstones(tenant_id, resource_id, deleted_at)"
                " VALUES(?,?,?) ON CONFLICT(tenant_id, resource_id) DO NOTHING",
                (tenant_id, resource_id, _now()),
            )
            self._conn.commit()

    def list_tombstones(self, tenant_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT resource_id FROM deletion_tombstones WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
        return [str(r["resource_id"]) for r in rows]

    def set_mcp_server_enabled(self, tenant_id: str, server_id: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE mcp_servers SET enabled=? WHERE tenant_id=? AND id=?",
                (1 if enabled else 0, tenant_id, server_id),
            )
            self._conn.commit()
        return bool(cur.rowcount)

    def set_skill_enabled(self, tenant_id: str, skill_id: str, version: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE skills SET enabled=? WHERE tenant_id=? AND id=? AND version=?",
                (1 if enabled else 0, tenant_id, skill_id, version),
            )
            self._conn.commit()
        return bool(cur.rowcount)

    def insert_demo_grant(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO demo_grants(tenant_id, id, subject, label, kind, status,"
                " manifest_id, manifest_revision, created_by, expires_at, revoked_at,"
                " created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                (
                    record["tenant_id"],
                    record["id"],
                    record["subject"],
                    record.get("label", ""),
                    record.get("kind", "invite"),
                    record.get("status", "active"),
                    record.get("manifest_id"),
                    int(record.get("manifest_revision", 0)),
                    record["created_by"],
                    record["expires_at"],
                    _now(),
                    _now(),
                ),
            )
            self._conn.commit()

    def get_demo_grant(self, tenant_id: str, grant_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM demo_grants WHERE tenant_id=? AND id=?",
                (tenant_id, grant_id),
            ).fetchone()
        return dict(row) if row else None

    def get_demo_grant_by_subject(self, tenant_id: str, subject: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM demo_grants WHERE tenant_id=? AND subject=? ORDER BY created_at DESC LIMIT 1",
                (tenant_id, subject),
            ).fetchone()
        return dict(row) if row else None

    def list_demo_grants(
        self,
        tenant_id: str,
        *,
        subject: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM demo_grants WHERE tenant_id=?"
        args: list[Any] = [tenant_id]
        if subject:
            sql += " AND subject=?"
            args.append(subject)
        if status:
            sql += " AND status=?"
            args.append(status)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def expire_demo_grants(self, tenant_id: str, kind: str, now_iso: str) -> int:
        """把已过期的 active 访客 grant 标为 expired（免登名额回收的第一步，不改历史）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE demo_grants SET status='expired', updated_at=?"
                " WHERE tenant_id=? AND kind=? AND status='active' AND expires_at<=?",
                (_now(), tenant_id, kind, now_iso),
            )
            self._conn.commit()
        return int(cur.rowcount)

    def set_demo_grant(
        self,
        tenant_id: str,
        grant_id: str,
        *,
        status: str | None = None,
        manifest_id: str | None = None,
        manifest_revision: int | None = None,
        revoked_at: str | None = None,
        clear_revoked_at: bool = False,
        expires_at: str | None = None,
    ) -> dict[str, Any] | None:
        fields: list[str] = []
        args: list[Any] = []
        if status is not None:
            fields.append("status=?")
            args.append(status)
        if manifest_id is not None:
            fields.append("manifest_id=?")
            args.append(manifest_id)
        if manifest_revision is not None:
            fields.append("manifest_revision=?")
            args.append(int(manifest_revision))
        if revoked_at is not None:
            fields.append("revoked_at=?")
            args.append(revoked_at)
        if clear_revoked_at:
            fields.append("revoked_at=NULL")
        if expires_at is not None:
            fields.append("expires_at=?")
            args.append(expires_at)
        if not fields:
            return self.get_demo_grant(tenant_id, grant_id)
        fields.append("updated_at=?")
        args.append(_now())
        args.extend([tenant_id, grant_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE demo_grants SET {', '.join(fields)} WHERE tenant_id=? AND id=?",
                args,
            )
            self._conn.commit()
        return self.get_demo_grant(tenant_id, grant_id)

    def insert_demo_manifest(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO demo_manifests(tenant_id, id, grant_id, revision,"
                " payload_json, payload_hash, published_by, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    record["tenant_id"],
                    record["id"],
                    record["grant_id"],
                    int(record["revision"]),
                    record["payload_json"],
                    record["payload_hash"],
                    record["published_by"],
                    _now(),
                ),
            )
            self._conn.commit()

    def get_demo_manifest(self, tenant_id: str, manifest_id: str, revision: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM demo_manifests WHERE tenant_id=? AND id=? AND revision=?",
                (tenant_id, manifest_id, int(revision)),
            ).fetchone()
        return dict(row) if row else None

    def list_demo_manifests(self, tenant_id: str, grant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM demo_manifests WHERE tenant_id=? AND grant_id=? ORDER BY revision ASC",
                (tenant_id, grant_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_demo_invite(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO demo_invites(tenant_id, id, grant_id, code_hash,"
                " expires_at, redeemed_at, created_at) VALUES(?,?,?,?,?,NULL,?)",
                (
                    record["tenant_id"],
                    record["id"],
                    record["grant_id"],
                    record["code_hash"],
                    record["expires_at"],
                    _now(),
                ),
            )
            self._conn.commit()

    def find_demo_invite_by_hash(self, tenant_id: str, code_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM demo_invites WHERE tenant_id=? AND code_hash=?",
                (tenant_id, code_hash),
            ).fetchone()
        return dict(row) if row else None

    def mark_demo_invite_redeemed(self, tenant_id: str, invite_id: str, redeemed_at: str) -> dict[str, Any] | None:
        with self._lock:
            self._conn.execute(
                "UPDATE demo_invites SET redeemed_at=? WHERE tenant_id=? AND id=? AND redeemed_at IS NULL",
                (redeemed_at, tenant_id, invite_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM demo_invites WHERE tenant_id=? AND id=?",
                (tenant_id, invite_id),
            ).fetchone()
        return dict(row) if row else None


RUN_TERMINAL = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
STEP_TERMINAL = {StepState.COMPLETED, StepState.FAILED, StepState.UNCERTAIN, StepState.CANCELLED}

# Receipt 别名一致性：kernel.types.Receipt 即 kernel.store.Receipt
_ = Receipt
