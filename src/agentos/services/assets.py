"""Asset 受控传输（FR-NODE：远程成果必须登记为可下载的 Asset 引用）。

N2a 契约（计划 §2.2）：
- 素材/成果绑定 subject（+ 可选 run/版本/parent），列表/详情/读取/下载按主体过滤；
  同租户第二主体与跨租户一律拒绝；无归属证据的旧数据保持不可见。
- 客户端 DTO 不含内部存储 path 或其他秘密（to_public 投影）。
- 素材文本登记：同主体同内容（sha256+mime）复用既有 Asset（稳定素材 ID）；
  幂等键同键异正文 CONFLICT，同键同内容回原 Asset。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext
from agentos.services.access import ROLE_ADMIN, role_of
from agentos.services.audit import AuditService


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds finite range")
    return number


MAX_MATERIAL_BYTES = 64 * 1024  # 单份素材 64 KiB（计划 §2.1）


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def to_public(row: dict[str, Any]) -> dict[str, Any]:
    """客户端投影：剥离内部存储路径等字段。"""
    return {
        "id": row.get("id"),
        "name": row.get("name") or row.get("id"),
        "mime": row.get("mime"),
        "size": row.get("size"),
        "sha256": row.get("sha256"),
        "source_node": row.get("source_node") or "",
        "subject": row.get("subject") or "",
        "run_id": row.get("run_id"),
        "version": row.get("version") or 0,
        "parent_asset_id": row.get("parent_asset_id"),
        "revision_reason": row.get("revision_reason") or "",
        "created_at": row.get("created_at"),
    }


class AssetService:
    """跨机素材/产物登记与受控传输。"""

    def __init__(self, store: StoreProtocol, audit: AuditService, assets_dir: Path) -> None:
        self._store = store
        self._audit = audit
        self._dir = assets_dir
        self._output_lock = threading.RLock()

    # ---------------------------------------------------------------- 登记
    def _write_and_register(
        self,
        actor: AuthContext,
        data: bytes,
        *,
        mime: str,
        meta: dict[str, Any] | None,
        source_node: str,
        **binding: Any,
    ) -> dict[str, Any]:
        asset_id = binding.get("asset_id") or "asset_" + uuid.uuid4().hex[:16]
        digest = sha256_bytes(data)
        path = self._dir / asset_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._store.upsert_asset(
            actor.tenant_id,
            asset_id,
            sha256=digest,
            size=len(data),
            mime=mime,
            source_node=source_node,
            path=str(path),
            meta_json=json.dumps(meta or {}, ensure_ascii=False),
            subject=actor.subject,
            run_id=binding.get("run_id"),
            name=binding.get("name") or "",
            version=binding.get("version") or 0,
            parent_asset_id=binding.get("parent_asset_id"),
            revision_reason=binding.get("revision_reason") or "",
        )
        self._audit.record(
            actor.tenant_id,
            "asset.registered",
            subject=actor.subject,
            run_id=binding.get("run_id"),
            detail={
                "asset_id": asset_id,
                "sha256": digest,
                "size": len(data),
                "source_node": source_node,
                "kind": binding.get("kind", ""),
            },
        )
        return {"asset_id": asset_id, "sha256": digest, "size": len(data), "mime": mime}

    def register_local(
        self,
        actor: AuthContext,
        data: bytes,
        *,
        mime: str = "application/octet-stream",
        meta: dict[str, Any] | None = None,
        source_node: str = "",
        run_id: str | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        return self._write_and_register(
            actor,
            data,
            mime=mime,
            meta=meta,
            source_node=source_node,
            run_id=run_id,
            name=name,
        )

    def register_material(
        self,
        actor: AuthContext,
        *,
        name: str,
        content_text: str,
        idempotency_key: str | None = None,
        content_type: str = "text/plain",
    ) -> dict[str, Any]:
        """文本素材登记（N2a）：UTF-8/大小校验 → 内容去重 → 幂等键裁定。"""
        if not content_text:
            raise AgentOSError(ErrorCode.INVALID, "material content_text is required")
        try:
            data = content_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            self._audit.record(
                actor.tenant_id,
                "ingress.material_rejected",
                subject=actor.subject,
                detail={"name": name, "reason": "invalid utf-8"},
            )
            raise AgentOSError(ErrorCode.INVALID, "material must be valid UTF-8 text") from exc
        if len(data) > MAX_MATERIAL_BYTES:
            self._audit.record(
                actor.tenant_id,
                "ingress.material_rejected",
                subject=actor.subject,
                detail={"name": name, "size": len(data)},
            )
            raise AgentOSError(
                ErrorCode.INVALID,
                f"material '{name}' exceeds {MAX_MATERIAL_BYTES} bytes limit",
            )
        mime = (content_type if content_type in {"text/plain", "text/markdown", "text/csv", "application/json"}
                else "text/plain")
        digest = sha256_bytes(data)

        # 幂等键：同键同内容回原 Asset；同键异正文 CONFLICT（FR-ING-03 同语义）
        if idempotency_key:
            prior = self._store.find_material_idem(actor.tenant_id, actor.subject, idempotency_key)
            if prior is not None:
                prior_hash, prior_asset = prior
                if prior_hash != digest:
                    self._audit.record(
                        actor.tenant_id,
                        "ingress.material_conflict",
                        subject=actor.subject,
                        detail={"idempotency_key": idempotency_key},
                    )
                    raise AgentOSError(
                        ErrorCode.CONFLICT,
                        "material idempotency key reused with different content",
                    )
                row = self._store.get_asset(actor.tenant_id, prior_asset)
                if row is not None:
                    return self._material_out(row, replayed=True)

        # 内容去重：同主体同内容（+同类型）复用既有素材 → 稳定素材 ID
        existing = self._store.find_asset_by_content(actor.tenant_id, actor.subject, digest, mime)
        if existing is not None:
            if idempotency_key:
                self._store.insert_material_idem(
                    actor.tenant_id, actor.subject, idempotency_key, digest, existing["id"]
                )
            return self._material_out(existing, replayed=True)

        reg = self._write_and_register(
            actor,
            data,
            mime=mime,
            meta={"origin": "material"},
            source_node="",
            name=name,
            kind="material",
        )
        if idempotency_key:
            self._store.insert_material_idem(actor.tenant_id, actor.subject, idempotency_key, digest, reg["asset_id"])
        return {**reg, "name": name, "replayed": False}

    @staticmethod
    def _material_out(row: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
        pub = to_public(row)
        return {
            "asset_id": pub["id"],
            "id": pub["id"],
            "name": pub["name"],
            "sha256": pub["sha256"],
            "size": pub["size"],
            "mime": pub["mime"],
            "replayed": replayed,
        }

    def register_report_version(
        self,
        actor: AuthContext,
        *,
        data: bytes,
        name: str,
        run_id: str,
        parent_asset_id: str | None = None,
        revision_reason: str = "",
        mime: str = "text/markdown",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """成果登记为不可变新版本（N3a/N3b）：version 递增，不覆盖旧行。"""
        version = 1
        if parent_asset_id:
            parent = self._owned(actor, parent_asset_id)
            if parent.get("mime") != "text/markdown" or not revision_reason.strip():
                raise AgentOSError(ErrorCode.INVALID, "report revision requires a report and a reason")
            version = self._store.next_asset_version(actor.tenant_id, parent_asset_id)
        reg = self._write_and_register(
            actor,
            data,
            mime=mime,
            meta=meta,
            source_node="",
            run_id=run_id,
            name=name,
            version=version,
            parent_asset_id=parent_asset_id,
            revision_reason=revision_reason,
            kind="report",
        )
        return {**reg, "name": name, "version": version}

    # ---------------------------------------------------------------- 读取
    def register_tool_output(self, actor: AuthContext, run_id: str, key: str,
                             name: str, content: str, *, legacy: bool = False) -> dict[str, Any]:
        """Immutable, task-owned output; receipt-loss retries return the same Asset.

        Fixed templates retain their own reviewed report/version registration.
        This supplements ordinary sandbox writes without changing their contract.
        """
        run = self._store.get_run(actor.tenant_id, run_id)
        if (not run or run.subject != actor.subject or actor.is_expired
                or not actor.has_scope("write")):
            raise AgentOSError(ErrorCode.DENIED, "output requires an authorized task")
        payload = json.loads(run.envelope_json).get("payload", {})
        if legacy and payload.get("template"):
            return {}
        if run.state != "running":
            raise AgentOSError(ErrorCode.DENIED, "output task is not running")
        if (not name or len(name) > 100 or name in {".", ".."}
                or any(c in name for c in "/\\\r\n\0")):
            raise AgentOSError(ErrorCode.INVALID, "output name must be a filename")
        suffix = Path(name).suffix.lower()
        mime = {".md": "text/markdown", ".txt": "text/plain", ".csv": "text/csv",
                ".json": "application/json"}.get(suffix)
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AgentOSError(ErrorCode.INVALID, "output must be valid UTF-8") from exc
        if not mime or len(data) > 1_000_000:
            if legacy:
                return {}  # Arbitrary legacy sandbox files are not public deliverables.
            raise AgentOSError(ErrorCode.INVALID, "use .md/.txt/.csv/.json, at most one megabyte")
        if suffix == ".json":
            try:
                json.loads(content, parse_constant=_reject_json_constant, parse_float=_finite_json_float)
            except (ValueError, RecursionError) as exc:
                if not legacy:
                    raise AgentOSError(ErrorCode.INVALID, "output must be valid JSON") from exc
        asset_id = "asset_" + hashlib.sha256(
            f"{actor.tenant_id}\0{actor.subject}\0{run_id}\0{key}".encode()).hexdigest()[:24]
        with self._output_lock:
            prior = self._store.get_asset(actor.tenant_id, asset_id)
            if prior:
                if prior["sha256"] != sha256_bytes(data) or prior["name"] != name:
                    raise AgentOSError(ErrorCode.CONFLICT, "output key already used for different content")
                self.read(actor, asset_id)  # Verify persisted content before returning its receipt.
                return {"asset_ref": asset_id, "sha256": prior["sha256"], "bytes": len(data), "replayed": True}
            parent_id = payload.get("parent_asset_id") if mime == "text/markdown" else None
            reason = str(payload.get("revision_reason") or "") if parent_id else ""
            version = 1
            if parent_id:
                parent = self._owned(actor, parent_id)
                if parent.get("mime") != "text/markdown" or not reason.strip():
                    raise AgentOSError(ErrorCode.INVALID, "report revision requires a report and a reason")
                version = self._store.next_asset_version(actor.tenant_id, parent_id)
            result = self._write_and_register(actor, data, mime=mime, meta={"origin": "tool-output"},
                source_node="", run_id=run_id, name=name, version=version, asset_id=asset_id, kind="output",
                parent_asset_id=parent_id, revision_reason=reason)
            self._store.event_append(actor.tenant_id, run_id, "artifact.created",
                {"asset_id": asset_id, "name": name, "mime": mime, "size": len(data), "version": version})
            return {"asset_ref": asset_id, "sha256": result["sha256"], "bytes": len(data), "replayed": False}

    def list_run_materials(self, actor: AuthContext, run_id: str) -> list[dict[str, Any]]:
        run = self._store.get_run(actor.tenant_id, run_id)
        if not run or run.subject != actor.subject or actor.is_expired:
            raise AgentOSError(ErrorCode.DENIED, "task materials not authorized")
        payload = json.loads(run.envelope_json).get("payload", {})
        entries = [*(payload.get("materials") or []),
                   *json.loads(run.checkpoint_json or "{}").get("collaboration", {}).get("additional_materials", [])]
        if isinstance(payload.get("context_asset"), dict):
            entries.append(payload["context_asset"])
        return [{"asset_id": m["asset_id"], "name": m.get("name", ""),
                 "size": m.get("size"), "sha256": m.get("sha256"), "mime": m.get("mime")}
                for m in entries]

    def _owned(self, actor: AuthContext, asset_id: str) -> dict[str, Any]:
        row = self._store.get_asset(actor.tenant_id, asset_id)
        if row is None:
            raise AgentOSError(ErrorCode.INVALID, f"asset not found: {asset_id}")
        # R-D：管理员按部署范围读取本租户全部成果（不冒充归属主体）；
        # 同租户其他主体/无归属旧数据仍拒绝（计划 §2.2）
        if row.get("subject") != actor.subject and role_of(actor) != ROLE_ADMIN:
            self._audit.record(
                actor.tenant_id,
                "asset.cross_subject_denied",
                subject=actor.subject,
                detail={"asset_id": asset_id},
            )
            raise AgentOSError(ErrorCode.DENIED, "not your asset")
        return row

    def get(self, actor: AuthContext, asset_id: str) -> dict[str, Any]:
        return to_public(self._owned(actor, asset_id))

    def get_internal(self, actor: AuthContext, asset_id: str) -> dict[str, Any]:
        """服务端内部读取（含 path）；绝不直接出网关。"""
        return self._owned(actor, asset_id)

    def read(self, actor: AuthContext, asset_id: str) -> bytes:
        row = self._owned(actor, asset_id)
        path = Path(row["path"])
        if not path.exists():
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"asset bytes missing: {asset_id}")
        data = path.read_bytes()
        actual = sha256_bytes(data)
        if actual != row["sha256"]:
            raise AgentOSError(ErrorCode.UNCERTAIN, f"asset corrupted: {asset_id}")
        return data

    def read_internal_bytes(self, asset_id: str, *, tenant_id: str) -> bytes:
        """服务端内部字节读取：调用方必须**先**完成逐资源授权（演示下载走 DemoService）。

        不替代身份与在线核权；仅用于授权判定通过后把字节交给受控响应。
        """
        row = self._store.get_asset(tenant_id, asset_id)
        if row is None:
            raise AgentOSError(ErrorCode.INVALID, f"asset not found: {asset_id}")
        path = Path(row["path"])
        if not path.exists():
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"asset bytes missing: {asset_id}")
        data = path.read_bytes()
        if sha256_bytes(data) != row["sha256"]:
            raise AgentOSError(ErrorCode.UNCERTAIN, f"asset corrupted: {asset_id}")
        return data

    def list(self, actor: AuthContext) -> list[dict[str, Any]]:
        # R-D：管理员列本租户全量成果（含 webhook 等受控主体）；其他主体仍按本人过滤
        subject = None if role_of(actor) == ROLE_ADMIN else actor.subject
        rows = self._store.list_assets(actor.tenant_id, subject=subject)
        return [to_public(r) for r in rows]

    def read_run_material(
        self,
        actor: AuthContext,
        run_id: str,
        asset_id: str,
    ) -> dict[str, Any]:
        """模板执行期的材料读取（material.read 工具后端）：

        双重授权——run 的 envelope 只能引用本次受理时登记的材料（越权引用拒绝），
        Asset 本体再过 subject/hash 校验。返回文本内容与校验信息。
        """
        run = self._store.get_run(actor.tenant_id, run_id)
        if run is None or run.subject != actor.subject:
            raise AgentOSError(ErrorCode.DENIED, "run not visible for material read")
        envelope = json.loads(run.envelope_json) if run.envelope_json else {}
        mats = envelope.get("payload", {}).get("materials") or []
        mats = [
            *mats,
            *json.loads(run.checkpoint_json or "{}").get("collaboration", {}).get("additional_materials", []),
        ]
        context_asset = envelope.get("payload", {}).get("context_asset")
        if isinstance(context_asset, dict):
            mats = [*mats, context_asset]
        entry = next((m for m in mats if m.get("asset_id") == asset_id), None)
        if entry is None:
            self._audit.record(
                actor.tenant_id,
                "asset.material_unauthorized",
                subject=actor.subject,
                run_id=run_id,
                detail={"asset_id": asset_id},
            )
            raise AgentOSError(ErrorCode.DENIED, f"material not authorized for this run: {asset_id}")
        data = self.read(actor, asset_id)  # 属主 + sha256 校验
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentOSError(ErrorCode.INVALID, "material is not valid UTF-8 text") from exc
        return {
            "name": entry.get("name") or asset_id,
            "content": text,
            "sha256": entry.get("sha256"),
            "size": len(data),
            "mime": entry.get("mime"),
        }

    def resolve_run_reference(
        self,
        actor: AuthContext,
        run_id: str,
        source_id: str,
        locator: str,
    ) -> dict[str, Any]:
        """Resolve immutable uploaded material lines in the authorized run, without trusting quote text."""
        run = self._store.get_run(actor.tenant_id, run_id)
        if run is None or (run.subject != actor.subject and role_of(actor) != ROLE_ADMIN):
            raise AgentOSError(ErrorCode.DENIED, "reference task not authorized")
        materials = json.loads(run.envelope_json).get("payload", {}).get("materials") or []
        materials = [
            *materials,
            *json.loads(run.checkpoint_json or "{}").get("collaboration", {}).get("additional_materials", []),
        ]
        entry = next((m for m in materials if m.get("asset_id") == source_id), None)
        if entry is None:
            raise AgentOSError(ErrorCode.DENIED, "source not authorized for this task")
        row = self._owned(actor, source_id)
        if row["sha256"] != entry["sha256"]:
            raise AgentOSError(ErrorCode.UNCERTAIN, "material version no longer matches")
        text = self.read(actor, source_id).decode("utf-8")
        lines = text.splitlines() or [""]
        match = re.fullmatch(r"L([1-9]\d*)(?:-L?([1-9]\d*))?", locator)
        if match is None:
            raise AgentOSError(ErrorCode.INVALID, "reference locator must be an explicit line range")
        first, last = int(match[1]), int(match[2] or match[1])
        if first > last or last > len(lines):
            raise AgentOSError(ErrorCode.INVALID, "reference lines do not exist in this material")
        return {
            "source_id": source_id,
            "source_version": row["sha256"],
            "locator": locator,
            "quote": "\n".join(lines[first - 1 : last]),
            "name": entry.get("name") or source_id,
        }

    # -------------------------------------------------------------- 节点传输
    def fetch_from_node(
        self,
        actor: AuthContext,
        *,
        node_endpoint: str,
        node_token: str,
        remote_ref: str,
        expect_sha256: str,
        mime: str = "application/octet-stream",
        meta: dict[str, Any] | None = None,
        run_id: str | None = None,
        source_node: str = "remote",
    ) -> dict[str, Any]:
        """从节点下载产物：校验 hash 后登记为 gateway 侧 Asset。"""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", remote_ref) or remote_ref in {".", ".."}:
            raise AgentOSError(ErrorCode.INVALID, "invalid remote asset ref")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", expect_sha256):
            raise AgentOSError(ErrorCode.INVALID, "invalid asset sha256")
        # 兼容旧调用方的 meta.run_id，但绑定必须是列字段且核对属主。
        run_id = run_id or (meta or {}).get("run_id")
        if run_id:
            run = self._store.get_run(actor.tenant_id, run_id)
            if run is None or run.subject != actor.subject:
                raise AgentOSError(ErrorCode.DENIED, "not your task")
        for row in self._store.list_assets(actor.tenant_id, subject=actor.subject):
            details = json.loads(row.get("meta_json") or "{}")
            if (
                row.get("run_id") == run_id
                and row.get("source_node") == source_node
                and details.get("remote_ref") == remote_ref
                and row["sha256"] == expect_sha256
            ):
                self.read(actor, str(row["id"]))  # 复用前校验本地字节，禁止伪报已交付。
                return {
                    "asset_id": row["id"],
                    "sha256": row["sha256"],
                    "size": row["size"],
                    "mime": row["mime"],
                    "node_id_ref": remote_ref,
                }
        url = node_endpoint.rstrip("/") + f"/assets/{remote_ref}"
        try:
            resp = httpx.get(url, headers={"Authorization": f"Bearer {node_token}"}, timeout=30.0, trust_env=False)
        except httpx.HTTPError as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"node unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"node asset fetch failed: HTTP {resp.status_code}")
        data = resp.content
        actual = sha256_bytes(data)
        if actual != expect_sha256:
            raise AgentOSError(
                ErrorCode.UNCERTAIN,
                f"asset hash mismatch: expect {expect_sha256[:12]}… got {actual[:12]}…",
            )
        reg = self.register_local(
            actor,
            data,
            mime=mime,
            source_node=source_node,
            run_id=run_id,
            name=remote_ref,
            meta={"remote_ref": remote_ref, **(meta or {})},
        )
        reg["node_id_ref"] = remote_ref
        return reg

    def push_to_node(
        self,
        actor: AuthContext,
        *,
        node_endpoint: str,
        node_token: str,
        data: bytes,
        filename: str,
    ) -> dict[str, Any]:
        """向节点推送素材：节点侧校验 hash，返回节点端引用。"""
        url = node_endpoint.rstrip("/") + "/assets"
        digest = sha256_bytes(data)
        try:
            resp = httpx.post(
                url,
                content=data,
                headers={
                    "Authorization": f"Bearer {node_token}",
                    "Content-Type": "application/octet-stream",
                    "X-Asset-Filename": filename,
                    "X-Asset-Sha256": digest,
                },
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"node unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise AgentOSError(ErrorCode.UNAVAILABLE, f"node asset push failed: HTTP {resp.status_code}")
        body = resp.json()
        if body.get("sha256") != digest:
            raise AgentOSError(ErrorCode.UNCERTAIN, "node-side hash mismatch after upload")
        self._audit.record(
            actor.tenant_id,
            "asset.pushed",
            subject=actor.subject,
            detail={"node_ref": body.get("asset_id"), "sha256": digest, "size": len(data)},
        )
        return {"sha256": digest, "size": len(data), "node_ref": body.get("asset_id")}
