"""入口归一服务（FR-ING-02/03）：

1. 先认证（未认证不得形成可执行 Envelope，并记审计）；
2. 附件安全落盘（自生成 ID 命名，大小限制，sha256 登记）；
3. 归一为 Envelope（payload_hash = 规范化 JSON 的 sha256）；
4. accept_once 原子受理：幂等登记 + run/job + 附件关联 + outbox。
   同键同 hash 重放返回原回执；同键不同 hash 返回 CONFLICT（防止误吞新意图）。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RunState
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import Attachment, AuthContext, Envelope, Receipt
from agentos.services.audit import AuditService
from agentos.services.identity import IdentityService

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB，超范围附件拒绝

# N2a 材料引用限额（计划 §2.1：≤3 份、单份 64 KiB、合计 128 KiB；服务端为准确径）
MAX_MATERIALS = 3
MAX_MATERIAL_BYTES = 64 * 1024
MAX_MATERIAL_TOTAL_BYTES = 128 * 1024
MATERIAL_MIMES = {"text/plain", "text/markdown", "text/csv", "application/json"}


def canonical_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IngressService:
    def __init__(
        self,
        store: StoreProtocol,
        identity: IdentityService,
        audit: AuditService,
        *,
        attachment_dir: Path,
        run_budget_microusd: int = 0,
        queue_limit: int = 200,
        assets: Any | None = None,  # AssetService（N2a 材料引用解析；可选注入）
        knowledge: Any | None = None,
        skills: Any | None = None,
        report_skill_source: Path | None = None,
        access: Any | None = None,  # AccessService（U4-01：guest 禁止业务写）
    ) -> None:
        self._store = store
        self._identity = identity
        self._audit = audit
        self._attachment_dir = attachment_dir
        self._run_budget = run_budget_microusd
        self._queue_limit = queue_limit
        self._assets = assets
        self._access = access
        self._knowledge = knowledge
        self._skills = skills
        self._report_skill_source = report_skill_source

    async def accept(
        self,
        raw: dict[str, Any],
        *,
        channel: str,
        credentials: dict[str, Any],
        thread_id: str | None = None,
        actor_override: AuthContext | None = None,
    ) -> Receipt:
        thread_id = thread_id or (str(raw["thread_id"]) if raw.get("thread_id") else None)
        if actor_override is not None:
            actor = actor_override
        else:
            try:
                actor = await self._identity.authenticate(credentials)
            except AgentOSError:
                # 未认证/伪造凭据：拒绝并记审计（FR-ING-03/FR-GOV-01）
                self._audit.record("default", "identity.denied", subject=None, detail={"channel": channel})
                raise
        if actor.is_expired:
            self._audit.record(
                actor.tenant_id,
                "identity.denied",
                subject="<unauthenticated>",
                detail={"channel": channel, "reason": "expired"},
            )
            raise AgentOSError(ErrorCode.DENIED, "credentials rejected")
        # U4-01：游客只读。所有入口（HTTP/CLI/Webhook/voice/工作流）共用这一判定，
        # 不靠路由条件或隐藏 UI；拒绝发生在受理之前，不会入队也不会产生副作用。
        if self._access is not None:
            self._access.require_write_allowed(actor, "task.create")

        payload = raw.get("payload")
        if not isinstance(payload, dict) or not payload.get("goal"):
            raise AgentOSError(ErrorCode.INVALID, "payload.goal is required")
        payload = dict(payload)
        # Context is resolved from authorized persisted runs, never a client supplied snapshot.
        payload.pop("context", None)
        payload.pop("context_asset", None)
        payload.pop("knowledge_binding", None)
        payload.pop("skills", None)
        payload.pop("materials", None)
        source = self._resolve_context(actor, payload)
        if source is not None:
            thread_id = source.thread_id or source.id

        # N2a：材料引用解析（受鉴权素材 → 引用受理；不把 bytes 填进 JSON 伪装上传）
        material_ids = raw.get("material_asset_ids") or payload.get("material_asset_ids")
        if material_ids is not None and (
            not isinstance(material_ids, list) or not all(isinstance(m, str) for m in material_ids)
        ):
            raise AgentOSError(ErrorCode.INVALID, "material_asset_ids must be a list of asset ids")
        if source is not None:
            previous = json.loads(source.envelope_json).get("payload", {})
            inherited = [m["asset_id"] for m in previous.get("materials", [])]
            material_ids = list(dict.fromkeys(inherited + (material_ids or [])))
        if material_ids:
            payload = dict(payload)
            payload["materials"] = self._resolve_materials(actor, material_ids)

        if self._knowledge is not None:
            previous_payload = json.loads(source.envelope_json).get("payload", {}) if source else {}
            payload["knowledge_binding"] = previous_payload.get("knowledge_binding") or self._knowledge.binding(actor)

        if self._skills is not None:
            skill_ids = payload.get("skill_ids") or []
            if not isinstance(skill_ids, list) or not all(isinstance(i, str) for i in skill_ids):
                raise AgentOSError(ErrorCode.INVALID, "skill_ids must be a list")
            inherited_skills = json.loads(source.envelope_json).get("payload", {}).get("skills", []) if source else []
            if (
                payload.get("template") in {"material-report", "activity-collaboration"}
                and self._report_skill_source is not None
            ):
                metas = self._skills.list(actor, include_disabled=True)
                if not any(m["id"] == "material-report" and m["version"] == "1" for m in metas):
                    self._skills.install(actor, str(self._report_skill_source))
                skill_ids = list(dict.fromkeys([*skill_ids, "material-report"]))
            available_skills = self._skills.list(actor)
            picked = []
            for skill_id in skill_ids:
                inherited_skill = next((s for s in inherited_skills if s["id"] == skill_id), None)
                meta = inherited_skill or next(
                    (
                        s
                        for s in available_skills
                        if s["id"] == skill_id and (skill_id != "material-report" or s["version"] == "1")
                    ),
                    None,
                )
                if meta is None:
                    raise AgentOSError(ErrorCode.DENIED, "selected skill unavailable")
                picked.append({k: meta[k] for k in ("id", "version", "digest")})
            payload["skills"] = picked or inherited_skills

        attachment_ids = await self.stage_attachments(actor, raw.get("attachments") or [])

        idempotency_key = str(raw.get("idempotency_key") or f"ing_{uuid.uuid4().hex}")
        intent_payload = {k: v for k, v in payload.items() if k not in {"knowledge_binding", "skills"}}
        payload_hash = canonical_hash({"payload": intent_payload, "attachments": attachment_ids})

        envelope = Envelope(
            id=f"env_{uuid.uuid4().hex}",
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            actor=actor,
            channel=channel,
            thread_id=thread_id,
            payload=payload,
            attachment_ids=attachment_ids,
        )

        existing = self._store.find_idempotent_receipt(actor.tenant_id, actor.subject, channel, idempotency_key)
        if existing is not None:
            prior_hash, receipt_json = existing
            if prior_hash != payload_hash:
                self._audit.record(
                    actor.tenant_id,
                    "ingress.conflict",
                    subject=actor.subject,
                    detail={"channel": channel, "idempotency_key": idempotency_key},
                )
                raise AgentOSError(
                    ErrorCode.CONFLICT,
                    "idempotency key reused with different payload",
                )
            receipt = Receipt.model_validate_json(receipt_json)
            replayed = Receipt(run_id=receipt.run_id, job_id=receipt.job_id, accepted=True, replayed=True)
            self._audit.record(
                actor.tenant_id,
                "ingress.replay",
                subject=actor.subject,
                run_id=receipt.run_id,
                detail={"channel": channel, "idempotency_key": idempotency_key},
            )
            return replayed

        # 背压（NFR-THR）：活动 run + 就绪作业 ≥ queue_limit 时明确拒绝（重试提示）
        active = self._store.count_active_runs(actor.tenant_id)
        if active >= self._queue_limit:
            self._audit.record(
                actor.tenant_id,
                "ingress.queue_full",
                subject=actor.subject,
                detail={"active": active, "limit": self._queue_limit},
            )
            raise AgentOSError(
                ErrorCode.UNAVAILABLE,
                f"queue full: {active} active runs >= limit {self._queue_limit}; retry later",
                detail={"queue_full": True, "active": active, "limit": self._queue_limit},
            )
        receipt = await self._accept_once(envelope)
        self._audit.record(
            actor.tenant_id,
            "run.created",
            subject=actor.subject,
            run_id=receipt.run_id,
            detail={"channel": channel, "idempotency_key": idempotency_key},
        )
        return receipt

    def _resolve_context(self, actor: AuthContext, payload: dict[str, Any]) -> Any:
        source_id = payload.get("context_run_id") or payload.get("parent_run_id")
        parent_id = payload.get("parent_asset_id")
        parent = None
        if parent_id:
            if self._assets is None:
                raise AgentOSError(ErrorCode.UNSUPPORTED, "asset intake not configured")
            parent = self._assets.get_internal(actor, parent_id)
            if parent.get("mime") != "text/markdown" or not parent.get("run_id"):
                raise AgentOSError(ErrorCode.INVALID, "revision requires a report from a task")
            if source_id and parent["run_id"] != source_id:
                raise AgentOSError(ErrorCode.INVALID, "parent report does not belong to context task")
            source_id = parent["run_id"]
        if not source_id:
            return None
        source = self._store.get_run(actor.tenant_id, str(source_id))
        if source is None:
            raise AgentOSError(ErrorCode.INVALID, "context task not found")
        if self._access is not None:
            self._access.require(actor, "task.read", kind="task", resource_id=source.id)
        elif source.subject != actor.subject:
            raise AgentOSError(ErrorCode.DENIED, "not your context task")
        if source.state not in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
            raise AgentOSError(ErrorCode.CONFLICT, "wait for the current task to finish before continuing")
        previous = json.loads(source.envelope_json).get("payload", {})
        if previous.get("template") == "material-report":
            payload["template"] = "material-report"
            if parent is None and self._assets is not None:
                reports = [
                    a
                    for a in self._assets.list(actor)
                    if a.get("run_id") == source.id and a.get("mime") == "text/markdown"
                ]
                if len(reports) > 1:
                    raise AgentOSError(ErrorCode.INVALID, "select the report version to revise")
                if reports:
                    parent = self._assets.get_internal(actor, reports[0]["id"])
                elif previous.get("parent_asset_id"):
                    # A failed revision has no delivery; keep the last immutable report in its lineage.
                    parent = self._assets.get_internal(actor, previous["parent_asset_id"])
        cp = json.loads(source.checkpoint_json or "{}")
        answers = [
            str(m.get("content") or "")
            for m in cp.get("messages", [])
            if m.get("role") == "assistant" and not m.get("tool_calls")
        ]
        payload["context_run_id"] = source.id
        payload["parent_run_id"] = source.id
        payload["context"] = {"run_id": source.id, "goal": source.goal_text, "answer": answers[-1] if answers else ""}
        if parent is not None:
            payload["parent_asset_id"] = parent["id"]
            payload["revision_reason"] = str(payload.get("revision_reason") or payload["goal"])
            payload["context_asset"] = {
                "asset_id": parent["id"],
                "name": parent.get("name") or "report.md",
                "sha256": parent["sha256"],
                "size": parent["size"],
                "mime": parent["mime"],
            }
        return source

    async def _accept_once(self, envelope: Envelope) -> Receipt:
        return self._store.accept_once(
            envelope_json=envelope.model_dump_json(),
            envelope_id=envelope.id,
            idempotency_key=envelope.idempotency_key,
            payload_hash=envelope.payload_hash,
            tenant_id=envelope.actor.tenant_id,
            subject=envelope.actor.subject,
            channel=envelope.channel,
            run_budget_microusd=self._run_budget,
            attachment_ids=envelope.attachment_ids,
        )

    def _resolve_materials(self, actor: AuthContext, material_ids: Any) -> list[dict[str, Any]]:
        """校验材料引用：存在、属主、文本类型、数量/大小限额；返回进入 envelope 的引用集。"""
        from agentos.kernel.errors import AgentOSError, ErrorCode

        if self._assets is None:
            raise AgentOSError(ErrorCode.UNSUPPORTED, "material intake not configured")
        if not isinstance(material_ids, list) or not all(isinstance(m, str) for m in material_ids):
            raise AgentOSError(ErrorCode.INVALID, "material_asset_ids must be a list of asset ids")
        if len(material_ids) > MAX_MATERIALS:
            raise AgentOSError(ErrorCode.INVALID, f"too many materials: {len(material_ids)} > {MAX_MATERIALS}")
        refs: list[dict[str, Any]] = []
        total = 0
        for mid in material_ids:
            row = self._assets.get_internal(actor, mid)  # 不存在/跨主体 → 拒绝
            if row.get("mime") not in MATERIAL_MIMES:
                raise AgentOSError(ErrorCode.INVALID, f"material '{mid}' is not accepted text material")
            total += int(row.get("size") or 0)
            if int(row.get("size") or 0) > MAX_MATERIAL_BYTES:
                raise AgentOSError(ErrorCode.INVALID, f"material '{mid}' exceeds {MAX_MATERIAL_BYTES} bytes")
            refs.append(
                {
                    "asset_id": row["id"],
                    "name": row.get("name") or row["id"],
                    "sha256": row["sha256"],
                    "size": int(row.get("size") or 0),
                    "mime": row.get("mime"),
                }
            )
        if total > MAX_MATERIAL_TOTAL_BYTES:
            raise AgentOSError(ErrorCode.INVALID, f"materials total {total} exceeds {MAX_MATERIAL_TOTAL_BYTES} bytes")
        return refs

    async def stage_attachments(self, actor: AuthContext, files: list[dict[str, Any]]) -> list[str]:
        """附件安全落盘：路径用自生成 ID；原文件名只作展示；超限拒绝（FR-ING-03）。"""
        ids: list[str] = []
        self._attachment_dir.mkdir(parents=True, exist_ok=True)
        for item in files:
            data = item.get("content")
            name = str(item.get("name", "attachment"))
            if not isinstance(data, (bytes, bytearray)):
                raise AgentOSError(ErrorCode.INVALID, f"attachment '{name}' content must be bytes")
            if len(data) > MAX_ATTACHMENT_BYTES:
                self._audit.record(
                    actor.tenant_id,
                    "ingress.attachment_rejected",
                    subject=actor.subject,
                    detail={"name": name, "size": len(data)},
                )
                raise AgentOSError(ErrorCode.INVALID, f"attachment '{name}' exceeds size limit")
            asset_id = f"att_{uuid.uuid4().hex}"
            path = self._attachment_dir / asset_id
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            attachment = Attachment(
                id=asset_id,
                tenant_id=actor.tenant_id,
                owner=actor.subject,
                display_name=name,
                storage_path=str(path),
                size_bytes=len(data),
                sha256=hashlib.sha256(bytes(data)).hexdigest(),
                content_type=str(item.get("content_type", "application/octet-stream")),
            )
            self._store.insert_attachment(attachment.model_dump(mode="json"))
            ids.append(asset_id)
        return ids
