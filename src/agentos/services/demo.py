"""U4-01/02 演示访问：admin 逐资源发布 + guest 只读投影与撤权。

边界：

- 本服务只做**演示只读授权**；它产出的 grant 永远不能被当作执行权限使用
  （执行授权走 PolicyService/approvals，两者不互通）。
- 一条任务不等于其关联资源已授权：run/asset/ref/版本/动作都要逐项显式列出。
- 游客投影是白名单 DTO：不在名单的字段一律不下发，而不是"删掉看起来敏感的键"。
- 撤销立即生效：每次读请求重新核对 grant 状态与到期，不缓存、不信任会话快照。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import timedelta
from typing import Any

from agentos.kernel.dto import (
    GUEST_ASSET_FIELDS,
    GUEST_RUN_FIELDS,
    GUEST_STEP_FIELDS,
    DemoAssetEntry,
    DemoManifestPayload,
    DemoRefEntry,
    DemoRunEntry,
)
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.states import RUN_TERMINAL_STATES
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext, utc_now
from agentos.services.access import ROLE_GUEST, AccessService, role_of
from agentos.services.audit import AuditService

INVITE_TTL_HOURS_DEFAULT = 24
INVITE_TTL_HOURS_MAX = 24 * 7
MANIFEST_ID_PREFIX = "mf_"
#: 默认发布只接受已完成任务（Q6）；终态即完成（succeeded/failed/cancelled）
_TERMINAL_STATES = {s.value for s in RUN_TERMINAL_STATES}
#: U5-01：游客是默认访问角色——一条免登“默认演示”通道 + 每位访客一份会话
PUBLIC_CHANNEL_GRANT_ID = "grant_public"
PUBLIC_CHANNEL_SUBJECT = "guest:public"
PUBLIC_CHANNEL_LABEL = "默认演示"
PUBLIC_KIND = "public"
#: 通道本身的到期只用于“管理员长期开放”，不是访客会话生命周期
PUBLIC_CHANNEL_DAYS = 3650


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DemoService:
    """演示发布与游客只读投影。"""

    def __init__(
        self,
        store: StoreProtocol,
        audit: AuditService,
        access: AccessService,
        assets: Any,
        identity_backend: Any,
        knowledge: Any = None,
        revocation_ledger: Any = None,
        *,
        public_enabled: bool = True,
        public_ttl_hours: int = INVITE_TTL_HOURS_DEFAULT,
        public_max_sessions: int = 200,
    ) -> None:
        self._store = store
        self._audit = audit
        self._access = access
        self._assets = assets
        self._identity = identity_backend
        # 内置知识服务（可选）：引用发布验证与失效联动（R-B/R-C）。WeKnora 后端
        # 的等价联动归 U4-07 验收。
        self._knowledge = knowledge
        # 撤销事实账本（Q2/Q7）：**库外**追加文件——恢复旧数据库快照不会连带回滚
        # 撤销事实；启动重放据此作废复活 grant。写失败必须失败关闭。
        self._ledger_path = revocation_ledger
        # U5-01：免登游客的服务端裁定参数（总开关来自配置，管理员可再暂停通道）
        self._public_enabled = bool(public_enabled)
        self._public_ttl_hours = max(1, min(int(public_ttl_hours), 24 * 30))
        self._public_max_sessions = max(1, int(public_max_sessions))

    # ------------------------------------------------------------ 库外撤权账本
    def _ledger_append(self, grant_id: str, *, state: str = "revoked") -> None:
        """追写撤销/重开事实（Q7 失败关闭）：写不下去就抛错，调用方不得改库。

        旧行没有 state 字段，语义等价 `revoked`；重开写 `active` 行，重放取每个
        grant 的**最后一行**状态，不会把“先撤后开”误判成仍撤销。
        """
        import json as _json
        from pathlib import Path

        if self._ledger_path is None:
            return
        path = Path(self._ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps(
            {"grant_id": grant_id, "state": state, "at": utc_now().isoformat(),
             "revoked_at": utc_now().isoformat() if state == "revoked" else None},
            ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def _ledger_state(self) -> dict[str, str]:
        """每个 grant 的最后账本状态（`revoked`/`active`）；无记录则不在表中。"""
        import json as _json
        from pathlib import Path

        if self._ledger_path is None:
            return {}
        path = Path(self._ledger_path)
        if not path.exists():
            return {}
        state: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue  # 旧行/半行不可读：不因此认定它已撤销
            grant_id = str(entry.get("grant_id") or "")
            if not grant_id:
                continue
            state[grant_id] = str(entry.get("state") or "revoked")
        return state

    # ================================================================ admin 侧
    def _require_admin(self, actor: AuthContext, action: str) -> None:
        self._access.require_admin(actor, action)

    def list_grants(self, actor: AuthContext, *, kind: str | None = None) -> list[dict[str, Any]]:
        self._require_admin(actor, "demo.list")
        rows = self._store.list_demo_grants(actor.tenant_id, kind=kind)
        out = []
        for row in rows:
            manifest = self._manifest_payload(actor.tenant_id, row)
            out.append({
                "id": row["id"], "subject": row["subject"], "label": row["label"],
                "kind": str(row.get("kind") or "invite"),
                "status": row["status"], "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
                "manifest_id": row["manifest_id"],
                "manifest_revision": row["manifest_revision"],
                "runs": sorted(manifest.run_ids()) if manifest else [],
                "assets": [
                    {"asset_id": a.asset_id, "version": a.version,
                     "preview": a.allow_preview, "download": a.allow_download}
                    for a in (manifest.assets if manifest else [])
                ],
            })
        return out

    def create_grant(
        self,
        actor: AuthContext,
        *,
        label: str,
        ttl_hours: int = INVITE_TTL_HOURS_DEFAULT,
    ) -> dict[str, Any]:
        """建立游客 grant + 一次性邀请码（库内只存 hash，不共享管理员凭据）。"""
        self._require_admin(actor, "demo.invite")
        ttl_hours = max(1, min(int(ttl_hours), INVITE_TTL_HOURS_MAX))
        now = utc_now()
        expires_at = now + timedelta(hours=ttl_hours)
        grant_id = "grant_" + secrets.token_hex(8)
        subject = f"guest:{secrets.token_hex(6)}"
        self._store.insert_demo_grant({
            "tenant_id": actor.tenant_id, "id": grant_id, "subject": subject,
            "label": label[:80], "kind": "invite", "status": "active",
            "created_by": actor.subject, "expires_at": expires_at.isoformat(),
        })
        code = secrets.token_urlsafe(24)
        invite_id = "inv_" + secrets.token_hex(8)
        self._store.insert_demo_invite({
            "tenant_id": actor.tenant_id, "id": invite_id, "grant_id": grant_id,
            "code_hash": _hash(code), "expires_at": expires_at.isoformat(),
        })
        self._audit.record(
            actor.tenant_id, "demo.grant_created", subject=actor.subject,
            detail={"grant_id": grant_id, "guest_subject": subject,
                    "expires_at": expires_at.isoformat(), "ttl_hours": ttl_hours},
        )
        return {
            "grant_id": grant_id, "guest_subject": subject,
            "invite_code": code, "expires_at": expires_at.isoformat(),
        }

    def publish(
        self,
        actor: AuthContext,
        *,
        grant_id: str,
        runs: list[dict[str, Any]],
        assets: list[dict[str, Any]] | None = None,
        refs: list[dict[str, Any]] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """发布新 revision 的不可变 manifest（revision 只增，旧 revision 不可改）。"""
        self._require_admin(actor, "demo.publish")
        if grant_id == PUBLIC_CHANNEL_GRANT_ID:
            # U5-01：默认演示通道按需创建/重开，管理员不需要先“建邀请”才能发布
            self.ensure_public_channel(actor)
        grant = self._store.get_demo_grant(actor.tenant_id, grant_id)
        if grant is None:
            raise AgentOSError(ErrorCode.INVALID, "grant not found")
        if str(grant["status"]) != "active":
            raise AgentOSError(ErrorCode.CONFLICT, "grant is not active")

        # 逐项校验资源真实存在且归属本租户（不能"与选中任务有关"就自动扩大）
        run_entries: list[DemoRunEntry] = []
        for item in runs:
            run_id = str(item.get("run_id") or "")
            run = self._store.get_run(actor.tenant_id, run_id)
            if run is None or run.tenant_id != actor.tenant_id:
                raise AgentOSError(ErrorCode.INVALID, f"run not publishable: {run_id}")
            include = [str(x) for x in item["include"]] if item.get("include") else None
            # 直播正文流必须逐任务显式开启（R-A）；默认发布只接受**已完成**任务并
            # 冻结事件高水位（Q6）——进行中观察须 live=true 且经管理员显式选择。
            live = bool(item.get("live", False))
            if not live and str(run.state.value) not in _TERMINAL_STATES:
                raise AgentOSError(
                    ErrorCode.INVALID,
                    f"run {run_id} is not completed (state={run.state.value}); "
                    "default publish only accepts completed runs, use live=true "
                    "for explicit live observation",
                )
            high_water = 0 if live else self._store.event_high_water(actor.tenant_id, run_id)
            if include is None:
                run_entries.append(DemoRunEntry(
                    run_id=run_id, live=live, event_high_water=high_water))
            else:
                run_entries.append(DemoRunEntry(
                    run_id=run_id, include=include, live=live, event_high_water=high_water))
        asset_entries: list[DemoAssetEntry] = []
        published_run_ids = {e.run_id for e in run_entries}
        for item in assets or []:
            asset_id = str(item.get("asset_id") or "")
            row = self._store.get_asset(actor.tenant_id, asset_id)
            if row is None:
                raise AgentOSError(ErrorCode.INVALID, f"asset not publishable: {asset_id}")
            # R-B：成果必须属于本次发布清单中的任务——不能借发布别任务之机夹带其他 run 的成果
            if str(row.get("run_id") or "") not in published_run_ids:
                raise AgentOSError(
                    ErrorCode.INVALID,
                    f"asset {asset_id} belongs to run '{row.get('run_id')}', "
                    "which is not among the published runs",
                )
            version = int(item.get("version") or row.get("version") or 1)
            if int(row.get("version") or 1) != version:
                raise AgentOSError(
                    ErrorCode.INVALID,
                    f"asset {asset_id} has version {row.get('version')}, requested {version}",
                )
            asset_entries.append(DemoAssetEntry(
                asset_id=asset_id, version=version,
                allow_preview=bool(item.get("preview", True)),
                allow_download=bool(item.get("download", False)),
            ))
        ref_entries: list[DemoRefEntry] = []
        for r in refs or []:
            run_id = str(r.get("run_id") or "")
            if run_id not in published_run_ids:
                raise AgentOSError(
                    ErrorCode.INVALID,
                    "ref run_id must be one of the published runs (R-B)",
                )
            source_id = str(r.get("source_id") or "")
            chunk_id = str(r.get("chunk_id") or "")
            quote = str(r.get("quote") or "")[:400]
            acl_snapshot, chunk_acl_snapshot = self._verify_ref(
                actor.tenant_id, source_id, chunk_id, quote, run_id)
            ref_entries.append(DemoRefEntry(
                source_id=source_id,
                chunk_id=chunk_id,
                quote=quote,
                run_id=run_id,
                acl_snapshot=acl_snapshot,
                chunk_acl_snapshot=chunk_acl_snapshot,
            ))
        previous = self._store.list_demo_manifests(actor.tenant_id, grant_id)
        revision = (previous[-1]["revision"] + 1) if previous else 1
        manifest_id = str(grant.get("manifest_id") or MANIFEST_ID_PREFIX + grant_id)
        payload = DemoManifestPayload(
            revision=revision, runs=run_entries, assets=asset_entries,
            refs=ref_entries, note=note[:200],
        )
        payload_json = payload.model_dump_json()
        self._store.insert_demo_manifest({
            "tenant_id": actor.tenant_id, "id": manifest_id, "grant_id": grant_id,
            "revision": revision, "payload_json": payload_json,
            "payload_hash": _hash(payload_json), "published_by": actor.subject,
        })
        self._store.set_demo_grant(
            actor.tenant_id, grant_id, manifest_id=manifest_id, manifest_revision=revision
        )
        self._audit.record(
            actor.tenant_id, "demo.published", subject=actor.subject,
            detail={"grant_id": grant_id, "manifest_id": manifest_id,
                    "revision": revision, "runs": sorted(payload.run_ids()),
                    "assets": [a.asset_id for a in asset_entries],
                    "payload_hash": _hash(payload_json)},
        )
        return {
            "grant_id": grant_id, "manifest_id": manifest_id, "revision": revision,
            "payload_hash": _hash(payload_json),
            "runs": sorted(payload.run_ids()),
            "assets": [{"asset_id": a.asset_id, "version": a.version,
                        "preview": a.allow_preview, "download": a.allow_download}
                       for a in asset_entries],
        }

    def revoke(self, actor: AuthContext, grant_id: str) -> dict[str, Any]:
        """撤销：立即阻止后续 API/刷新/预览下载/引用/新流连接。

        Q7 失败关闭：撤销事实**先**落库外账本；账本写不下去（只读/磁盘满/权限）
        就报错并返回失败，不能先改库后“口头说已撤销”——恢复旧快照时只剩一个
        无事实依据的 active grant。
        """
        self._require_admin(actor, "demo.revoke")
        grant = self._store.get_demo_grant(actor.tenant_id, grant_id)
        if grant is None:
            raise AgentOSError(ErrorCode.INVALID, "grant not found")
        now_iso = utc_now().isoformat()
        try:
            self._ledger_append(grant_id)
        except OSError as exc:
            self._audit.record(
                actor.tenant_id, "demo.revocation_ledger_write_failed", subject=actor.subject,
                detail={"grant_id": grant_id, "error": type(exc).__name__},
            )
            raise AgentOSError(
                ErrorCode.UNCERTAIN,
                "撤销未生效：库外撤权账本不可写（恢复旧快照会使该游客复活）；"
                "请修复数据目录后重试",
                detail={"grant_id": grant_id},
            ) from exc
        self._store.set_demo_grant(
            actor.tenant_id, grant_id, status="revoked", revoked_at=now_iso
        )
        # 会话与主体两层失效：已签发 access 也会因主体 revoked 立即拒绝
        self._store.revoke_all_auth_sessions(actor.tenant_id, str(grant["subject"]))
        self._identity.revoke_subject(str(grant["subject"]))
        self._audit.record(
            actor.tenant_id, "demo.revoked", subject=actor.subject,
            detail={"grant_id": grant_id, "guest_subject": grant["subject"]},
        )
        return {"grant_id": grant_id, "status": "revoked", "revoked_at": now_iso}

    def void_all_for_restore(self, actor: AuthContext, *, reason: str = "backup_restore") -> dict[str, Any]:
        """恢复旧快照前统一作废游客邀请与会话（R08）：已撤销的 grant 不因快照复活。

        备份里的授权/manifest 元数据会随快照回来，但撤销是“事实”而非“数据”；
        因此恢复后必须重放撤销事实——这里统一作废，管理员按需重新发布。
        """
        self._require_admin(actor, "demo.void_all")
        rows = self._store.list_demo_grants(actor.tenant_id, status="active")
        # Q7：先把全部撤销事实写入账本再改库；中途失败不会留下“部分作废”的库
        try:
            for row in rows:
                self._ledger_append(str(row["id"]))
        except OSError as exc:
            self._audit.record(
                actor.tenant_id, "demo.revocation_ledger_write_failed", subject=actor.subject,
                detail={"reason": reason, "error": type(exc).__name__},
            )
            raise AgentOSError(
                ErrorCode.UNCERTAIN,
                "作废未生效：库外撤权账本不可写；请先修复数据目录后重试",
            ) from exc
        voided: list[str] = []
        for row in rows:
            self._store.set_demo_grant(
                actor.tenant_id, str(row["id"]), status="revoked",
                revoked_at=utc_now().isoformat(),
            )
            self._store.revoke_all_auth_sessions(actor.tenant_id, str(row["subject"]))
            self._identity.revoke_subject(str(row["subject"]))
            voided.append(str(row["id"]))
        self._audit.record(
            actor.tenant_id, "demo.voided_on_restore", subject=actor.subject,
            detail={"reason": reason, "grants": voided[:50], "count": len(voided)},
        )
        return {"voided": voided, "count": len(voided), "reason": reason}

    def _run_ref_pairs(self, tenant_id: str, run_id: str) -> set[tuple[str, str]]:
        """服务端读取该 run 步骤记录里的真实引用对 (source_id, chunk_locator)。

        引用关系由服务端记录建立（Q1）：客户端给的 source/chunk 必须能在
        任务自己的工具回执 refs 里找到，不能借发布夹带无关来源。
        """
        pairs: set[tuple[str, str]] = set()
        for step in self._store.list_steps(tenant_id, run_id):
            raw = step.result_json
            if not raw:
                continue
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for ref in result.get("refs") or []:
                if isinstance(ref, dict) and ref.get("source_id"):
                    pairs.add((str(ref["source_id"]), str(ref.get("locator") or "")))
        for event in self._store.event_list(tenant_id, run_id, limit=10000):
            if event["kind"] == "report.references":
                for ref in event["payload"].get("refs", []):
                    pairs.add((str(ref["source_id"]), str(ref["locator"])))
        return pairs

    def _verify_ref(self, tenant_id: str, source_id: str, chunk_id: str,
                    quote: str, run_id: str) -> tuple[list[str], list[str]]:
        """发布端引用验证（R-B）：片段必须服务端可回查；返回 source/chunk ACL 快照。

        - 知识后端没有本地校验器时 **fail-closed**（拒绝发布引用），不做无验证放行；
        - quote 非空时必须命中 chunk 正文子串——不接受请求凭空给的展示文本。
        """
        if not source_id:
            raise AgentOSError(ErrorCode.INVALID, "ref source_id is required")
        # Q1：片段必须是该 run 自己记录过的引用（来源关系由服务端建立）
        pairs = self._run_ref_pairs(tenant_id, run_id)
        if (source_id, chunk_id) not in pairs and (source_id, "") not in pairs:
            raise AgentOSError(
                ErrorCode.INVALID,
                f"ref {source_id}/{chunk_id or '*'} is not among run '{run_id}' recorded references",
            )
        material = self._store.get_asset(tenant_id, source_id)
        if material is not None:
            run = self._store.get_run(tenant_id, run_id)
            assert run is not None
            actor = AuthContext.model_validate(json.loads(run.envelope_json)["actor"])
            resolved = self._assets.resolve_run_reference(actor, run_id, source_id, chunk_id)
            if quote and quote not in resolved["quote"]:
                raise AgentOSError(ErrorCode.INVALID, "ref quote is not part of the referenced material")
            return [str(material["subject"])], []
        k = self._knowledge
        if k is None:
            raise AgentOSError(ErrorCode.INVALID, "ref verification unavailable for the configured knowledge backend")
        if k.is_tombstoned(tenant_id, source_id):
            raise AgentOSError(ErrorCode.INVALID, f"ref source deleted: {source_id}")
        acl = list(k.source_acl(tenant_id, source_id))
        if not acl:
            # 来源不存在或 ACL 为空：无法证明片段可公开
            raise AgentOSError(ErrorCode.INVALID, f"ref source unknown: {source_id}")
        chunk_acl: list[str] = []
        if chunk_id:
            meta = k.chunk_meta(tenant_id, chunk_id)
            if meta is None:
                raise AgentOSError(ErrorCode.INVALID, f"ref chunk not found: {chunk_id}")
            if str(meta.get("source_id") or "") != source_id:
                raise AgentOSError(ErrorCode.INVALID, "ref chunk does not belong to source")
            chunk_acl = list(meta.get("acl") or [])
            if quote and quote not in str(meta.get("text") or ""):
                raise AgentOSError(
                    ErrorCode.INVALID, "ref quote is not part of the referenced chunk")
        elif quote:
            raise AgentOSError(
                ErrorCode.INVALID, "ref quote requires chunk_id for server-side verification")
        return acl, chunk_acl

    def _ref_still_live(self, tenant_id: str, ref: DemoRefEntry) -> bool:
        """读端失效联动（R-C）：来源删除/任一级 ACL 收紧后，已发布引用随新读失效。

        无本地校验器时 fail-closed：已发布引用不再对外展示。
        """
        material = self._store.get_asset(tenant_id, ref.source_id)
        if material is not None:
            try:
                run = self._store.get_run(tenant_id, ref.run_id)
                if run is None or str(material.get("subject")) not in ref.acl_snapshot:
                    return False
                actor = AuthContext.model_validate(json.loads(run.envelope_json)["actor"])
                resolved = self._assets.resolve_run_reference(actor, ref.run_id, ref.source_id, ref.chunk_id)
                return not ref.quote or ref.quote in resolved["quote"]
            except AgentOSError:
                return False
        k = self._knowledge
        if k is None:
            return False
        if ref.source_id and k.is_tombstoned(tenant_id, ref.source_id):
            return False
        chunk_meta: dict[str, Any] | None = None
        if ref.chunk_id:
            chunk_meta = k.chunk_meta(tenant_id, ref.chunk_id)
            if chunk_meta is None or str(chunk_meta.get("source_id") or "") != ref.source_id:
                return False
        if ref.acl_snapshot:
            current = set(k.source_acl(tenant_id, ref.source_id))
            if not set(ref.acl_snapshot) <= current:
                return False  # 来源级 ACL 收紧
        if ref.chunk_acl_snapshot and chunk_meta is not None:
            # chunk 级 ACL 收紧 → 失效
            return set(ref.chunk_acl_snapshot) <= set(chunk_meta.get("acl") or [])
        return True

    def replay_revocations(self, tenant_id: str) -> int:
        """启动/恢复后重放撤销事实（R-C/Q2/Q7）：已撤销的 grant 不得因恢复旧快照复活。

        撤销是“事实”而非“数据”：恢复出的旧库里 grant 可能回到 active 且审计
        一同回滚，因此撤销事实同时保存在**库外账本**；这里合并账本与库内审计
        统一再撤销。账本不可读且库内仍有 active grant 时**失败关闭**：
        作废全部游客面并给出可诊断原因，不静默放行可能已撤销的授权。
        """
        ledger_readable = True
        revoked_ids: set[str] = set()
        try:
            state = self._ledger_state()
        except OSError as exc:
            ledger_readable = False
            state = {}
            self._audit.record(
                tenant_id, "demo.revocation_ledger_unreadable", subject="system",
                detail={"error": type(exc).__name__},
            )
        revoked_ids.update(gid for gid, st in state.items() if st == "revoked")
        # 审计兜底（账本缺失时仍能重放）；上限取大避免长审计漏掉靠后的记录
        for kind_prefix in ("demo.revoked", "demo.voided_on_restore"):
            for a in self._store.audit_query(tenant_id, kind_prefix=kind_prefix, limit=100000):
                detail = a.detail or {}
                if detail.get("grant_id"):
                    revoked_ids.add(str(detail["grant_id"]))
                for gid in detail.get("grants") or []:
                    revoked_ids.add(str(gid))
        active = self._store.list_demo_grants(tenant_id, status="active")
        if not ledger_readable and active:
            # 失败关闭：账本读不出时无法证明 active grant 未被撤销，一律作废
            revoked_ids.update(str(row["id"]) for row in active)
        replayed = 0
        if not revoked_ids:
            return 0
        for row in active:
            if str(row["id"]) in revoked_ids and state.get(str(row["id"])) != "active":
                self._store.set_demo_grant(
                    tenant_id, str(row["id"]), status="revoked",
                    revoked_at=utc_now().isoformat(),
                )
                self._store.revoke_all_auth_sessions(tenant_id, str(row["subject"]))
                self._identity.revoke_subject(str(row["subject"]))
                replayed += 1
        if replayed:
            self._audit.record(
                tenant_id, "demo.revocation_replayed", subject="system",
                detail={"count": replayed, "reason": "restored_grant_must_not_revive",
                        "ledger_readable": ledger_readable},
            )
        return replayed

    def preview_payload(
        self, actor: AuthContext, *, runs: list[dict[str, Any]],
        assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """发布前的游客可见内容预览（脱敏后正文由管理员确认可公开）。"""
        self._require_admin(actor, "demo.preview")
        return {
            "runs": [self._run_projection(actor, str(r.get("run_id") or "")) for r in runs],
            "assets": [
                {"asset_id": str(a.get("asset_id") or ""), "version": int(a.get("version") or 1)}
                for a in (assets or [])
            ],
            "note": "预览为白名单投影；成果正文需管理员确认可公开，脱敏不只依赖正则。",
        }

    # ================================================================ U5-01 免登游客
    def public_channel_status(self, tenant_id: str = "default") -> dict[str, Any]:
        """默认演示通道的当前事实（管理员与登录页共用）：不含任何正文或凭据。"""
        grant = self._store.get_demo_grant(tenant_id, PUBLIC_CHANNEL_GRANT_ID)
        manifest = self._manifest_payload(tenant_id, grant) if grant else None
        visitors = [
            row for row in self._store.list_demo_grants(
                tenant_id, kind=PUBLIC_KIND, status="active")
            if str(row["subject"]) != PUBLIC_CHANNEL_SUBJECT
        ]
        status = str(grant["status"]) if grant else "absent"
        revision = int(grant["manifest_revision"]) if grant else 0
        return {
            "configured": self._public_enabled,
            "open": status == "active" and self._public_enabled,
            "grant_id": PUBLIC_CHANNEL_GRANT_ID,
            "status": status,
            "manifest_revision": revision,
            "published": manifest is not None,
            "runs": sorted(manifest.run_ids()) if manifest else [],
            "note": manifest.note if manifest else "",
            "ttl_hours": self._public_ttl_hours,
            "max_sessions": self._public_max_sessions,
            "active_visitors": len(visitors),
        }

    def ensure_public_channel(self, actor: AuthContext) -> dict[str, Any]:
        """幂等建立/重开默认演示通道（仅管理员）：保留已发布的 manifest 引用。"""
        self._require_admin(actor, "demo.publish")
        tenant_id = actor.tenant_id
        expires = (utc_now() + timedelta(days=PUBLIC_CHANNEL_DAYS)).isoformat()
        grant = self._store.get_demo_grant(tenant_id, PUBLIC_CHANNEL_GRANT_ID)
        if grant is None:
            self._store.insert_demo_grant({
                "tenant_id": tenant_id, "id": PUBLIC_CHANNEL_GRANT_ID,
                "subject": PUBLIC_CHANNEL_SUBJECT, "label": PUBLIC_CHANNEL_LABEL,
                "kind": PUBLIC_KIND, "status": "active",
                "created_by": actor.subject, "expires_at": expires,
            })
            self._audit.record(
                tenant_id, "demo.public_channel_created", subject=actor.subject,
                detail={"grant_id": PUBLIC_CHANNEL_GRANT_ID},
            )
        elif str(grant["status"]) != "active":
            # 重开：先追写 active 事实，再改库（Q7：不留“库改了、事实没记”的空洞）
            try:
                self._ledger_append(PUBLIC_CHANNEL_GRANT_ID, state="active")
            except OSError as exc:
                raise AgentOSError(
                    ErrorCode.UNCERTAIN,
                    "默认演示未能重开：库外撤权账本不可写；请先修复数据目录后重试",
                ) from exc
            self._store.set_demo_grant(
                tenant_id, PUBLIC_CHANNEL_GRANT_ID, status="active",
                clear_revoked_at=True, expires_at=expires,
            )
            self._audit.record(
                tenant_id, "demo.public_channel_reopened", subject=actor.subject,
                detail={"grant_id": PUBLIC_CHANNEL_GRANT_ID},
            )
        return self.public_channel_status(tenant_id)

    def set_public_open(self, actor: AuthContext, *, open_access: bool) -> dict[str, Any]:
        """管理员开关：关闭时立即作废全部免登访客会话（失败关闭）。"""
        self._require_admin(actor, "demo.publish")
        if open_access:
            return self.ensure_public_channel(actor)
        # 账本先行：通道与每位访客都留下撤销事实，恢复旧快照不能复活
        rows = self._store.list_demo_grants(actor.tenant_id, kind=PUBLIC_KIND, status="active")
        try:
            for row in rows:
                self._ledger_append(str(row["id"]))
        except OSError as exc:
            self._audit.record(
                actor.tenant_id, "demo.revocation_ledger_write_failed", subject=actor.subject,
                detail={"reason": "public_close", "error": type(exc).__name__},
            )
            raise AgentOSError(
                ErrorCode.UNCERTAIN,
                "未能关闭免登演示：库外撤权账本不可写；请先修复数据目录后重试",
            ) from exc
        voided: list[str] = []
        for row in rows:
            self._store.set_demo_grant(
                actor.tenant_id, str(row["id"]), status="revoked",
                revoked_at=utc_now().isoformat(),
            )
            self._store.revoke_all_auth_sessions(actor.tenant_id, str(row["subject"]))
            self._identity.revoke_subject(str(row["subject"]))
            voided.append(str(row["id"]))
        self._audit.record(
            actor.tenant_id, "demo.public_closed", subject=actor.subject,
            detail={"revoked": voided[:50], "count": len(voided)},
        )
        return self.public_channel_status(actor.tenant_id)

    def bootstrap_public_session(self, tenant_id: str = "default") -> dict[str, Any]:
        """游客免登：无凭据换取一份只读会话（默认角色，不要求访问码）。

        服务端裁定：必须总开关开 + 通道 active + 已发布 manifest；每位访客拿到
        **独立 subject** 与当前 revision 的不可变快照，仍逐资源受白名单与开关约束。
        """
        if not self._public_enabled:
            raise AgentOSError(
                ErrorCode.DENIED, "演示访问未开放",
                detail={"reason": "public_disabled"},
            )
        now = utc_now()
        template = self._store.get_demo_grant(tenant_id, PUBLIC_CHANNEL_GRANT_ID)
        if template is None or str(template["status"]) != "active":
            raise AgentOSError(
                ErrorCode.DENIED, "演示访问未开放",
                detail={"reason": "public_disabled"},
            )
        manifest = self._manifest_payload(tenant_id, template)
        if manifest is None:
            raise AgentOSError(
                ErrorCode.CONFLICT, "管理员尚未发布演示内容",
                detail={"reason": "not_published"},
            )
        # 名额回收：先标过期、再清主体行；仍满则明确拒绝，不默默挤掉已有访客
        self._store.expire_demo_grants(tenant_id, PUBLIC_KIND, now.isoformat())
        visitors = [
            row for row in self._store.list_demo_grants(
                tenant_id, kind=PUBLIC_KIND, status="active")
            if str(row["subject"]) != PUBLIC_CHANNEL_SUBJECT
        ]
        if len(visitors) >= self._public_max_sessions:
            self._identity.prune_expired_subjects()
            if len(visitors) >= self._public_max_sessions:
                raise AgentOSError(
                    ErrorCode.UNAVAILABLE, "演示名额已满，请稍后重试",
                    detail={"reason": "quota_exhausted",
                            "max_sessions": self._public_max_sessions},
                )
        grant_id = "grant_" + secrets.token_hex(8)
        subject = f"guest:{secrets.token_hex(6)}"
        ttl_expires = now + timedelta(hours=self._public_ttl_hours)
        channel_expires = str(template["expires_at"])
        expires_at = min(ttl_expires.isoformat(), channel_expires)
        self._store.insert_demo_grant({
            "tenant_id": tenant_id, "id": grant_id, "subject": subject,
            "label": PUBLIC_CHANNEL_LABEL, "kind": PUBLIC_KIND, "status": "active",
            "manifest_id": str(template["manifest_id"]),
            "manifest_revision": int(template["manifest_revision"]),
            "created_by": "anonymous", "expires_at": expires_at,
        })
        self._identity.register_guest_subject(
            subject, grant_id=grant_id, expires_at=expires_at, tenant_id=tenant_id
        )
        self._audit.record(
            tenant_id, "demo.public_entry", subject=subject,
            detail={"grant_id": grant_id,
                    "manifest_revision": int(template["manifest_revision"]),
                    "expires_at": expires_at},
        )
        return {
            "subject": subject, "grant_id": grant_id, "expires_at": expires_at,
            "manifest_revision": manifest.revision,
        }

    # ================================================================ guest 侧
    def _manifest_payload(self, tenant_id: str, grant: dict[str, Any]) -> DemoManifestPayload | None:
        manifest_id = str(grant.get("manifest_id") or "")
        revision = int(grant.get("manifest_revision") or 0)
        if not manifest_id or revision <= 0:
            return None
        row = self._store.get_demo_manifest(tenant_id, manifest_id, revision)
        if row is None:
            return None
        return DemoManifestPayload.model_validate_json(str(row["payload_json"]))

    def _require_grant(self, actor: AuthContext) -> tuple[dict[str, Any], DemoManifestPayload]:
        if role_of(actor) != ROLE_GUEST:
            self._audit.record(
                actor.tenant_id, "access.denied", subject=actor.subject,
                detail={"reason": "not a guest session"},
            )
            raise AgentOSError(ErrorCode.DENIED, "guest session required")
        grant = self._access.active_grant(actor)
        if grant is None:
            self._audit.record(
                actor.tenant_id, "demo.denied", subject=actor.subject,
                detail={"reason": "no active grant"},
            )
            raise AgentOSError(ErrorCode.DENIED, "demo access ended")
        manifest = self._manifest_payload(actor.tenant_id, grant)
        if manifest is None:
            raise AgentOSError(ErrorCode.DENIED, "demo manifest unavailable")
        return grant, manifest

    def guest_home(self, actor: AuthContext) -> dict[str, Any]:
        grant, manifest = self._require_grant(actor)
        return {
            "role": "guest",
            "label": str(grant.get("label") or ""),
            "expires_at": grant.get("expires_at"),
            "manifest_revision": manifest.revision,
            "note": manifest.note,
            "runs": [self._run_projection(actor, rid) for rid in sorted(manifest.run_ids())],
        }

    def guest_runs(self, actor: AuthContext) -> list[dict[str, Any]]:
        _grant, manifest = self._require_grant(actor)
        return [self._run_projection(actor, rid) for rid in sorted(manifest.run_ids())]

    def _run_projection(self, actor: AuthContext, run_id: str) -> dict[str, Any]:
        """任务卡片投影：只含白名单字段；列表与详情共用同一投影，计数也走它。"""
        run = self._store.get_run(actor.tenant_id, run_id)
        if run is None:
            return {"id": run_id, "state": "unavailable"}
        dto = run.model_dump(mode="json", exclude={"envelope_json", "checkpoint_json", "error_json"})
        out = AccessService.project(dto, GUEST_RUN_FIELDS)
        out["goal"] = str(dto.get("goal_text") or "")[:200]
        out.pop("goal_text", None)
        return out

    def guest_run_detail(self, actor: AuthContext, run_id: str) -> dict[str, Any]:
        grant, manifest = self._require_grant(actor)
        decision = self._access.check(actor, "task.read", kind="task", resource_id=run_id)
        if not decision.allowed:
            self._audit.record(
                actor.tenant_id, "demo.denied", subject=actor.subject,
                detail={"run_id": run_id[:64], "reason": decision.reason},
            )
            raise AgentOSError(ErrorCode.DENIED, decision.reason)
        run = self._store.get_run(actor.tenant_id, run_id)
        if run is None:
            # 游客面统一 DENIED→404：不区分"不存在"与"未发布"（无存在性预言机）
            raise AgentOSError(ErrorCode.DENIED, "run not published to this guest")
        entry = next((r for r in manifest.runs if r.run_id == run_id), None)
        include = set(entry.include) if entry else set()
        detail: dict[str, Any] = {"run": self._run_projection(actor, run_id)}

        if "steps" in include:
            steps = self._store.list_steps(actor.tenant_id, run_id)
            detail["steps"] = [
                AccessService.project(s.model_dump(mode="json"), GUEST_STEP_FIELDS)
                for s in steps
            ]
        if "plan" in include:
            envelope = json.loads(run.envelope_json) if run.envelope_json else {}
            raw_plan = envelope.get("payload", {}).get("plan")
            detail["plan"] = _safe_plan(raw_plan) if isinstance(raw_plan, dict) else None
        if "artifacts" in include:
            artifacts: list[dict[str, Any]] = []
            for a in manifest.assets:
                row = self._store.get_asset(actor.tenant_id, a.asset_id) or {}
                if row.get("run_id") != run_id:
                    continue  # R-B：成果只随归属任务出现
                # Q2/Q8：成果所属任务存在失效来源 → 整份撤下（不覆盖原不可变 Asset）
                if self._asset_pull_reason(actor.tenant_id, run_id, manifest, row):
                    continue
                artifacts.append(
                    AccessService.project(row, GUEST_ASSET_FIELDS)
                    | {"preview": a.allow_preview, "download": a.allow_download}
                )
            detail["artifacts"] = artifacts
        if "approvals" in include:
            detail["approvals"] = [
                {"id": p.id, "state": p.state.value, "summary": p.summary[:120]}
                for p in self._store.list_approvals(actor.tenant_id)
                if str(p.call_id).startswith(f"{run_id}:")
            ]
        # R-B：引用只随其来源任务可见；R-C：删除/ACL 收紧后随新读失效
        detail["refs"] = [
            {"source_id": r.source_id, "chunk_id": r.chunk_id, "quote": r.quote}
            for r in manifest.refs
            if r.run_id == run_id and self._ref_still_live(actor.tenant_id, r)
        ]
        detail["manifest_revision"] = manifest.revision
        return detail

    def guest_events(
        self, actor: AuthContext, run_id: str, *, cursor: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        grant, manifest = self._require_grant(actor)
        decision = self._access.check(actor, "task.read", kind="task", resource_id=run_id)
        if not decision.allowed:
            self._audit.record(
                actor.tenant_id, "demo.denied", subject=actor.subject,
                detail={"run_id": run_id[:64], "reason": decision.reason},
            )
            raise AgentOSError(ErrorCode.DENIED, decision.reason)
        limit = max(1, min(int(limit), 500))
        # 直播只对 manifest 中显式开启的任务生效（R-A/Q6）：默认发布快照既不含
        # 正文流，也不含发布时刻之后的新事件（高水位冻结）。
        entry = next((r for r in manifest.runs if r.run_id == run_id), None)
        live = bool(entry.live) if entry else False
        high_water = int(entry.event_high_water) if entry and not live else 0
        # Q9：非 live 任务的游标不能越过本 revision 高水位——包括**请求**里
        # 带的旧游标（上一个 revision 可能已把它带到更高位置）。
        start = int(cursor)
        if high_water > 0:
            start = min(start, high_water)
        events = self._store.event_list(actor.tenant_id, run_id, after_seq=start, limit=limit)
        # 游标基于原始事件序列（不因投影丢事件导致重复拉取），但受高水位封顶
        projected = [
            AccessService.public_event(e, live=live)
            for e in events
            if high_water <= 0 or int(e.get("seq") or 0) <= high_water
        ]
        safe = [e for e in projected if e is not None]
        raw_cursor = int(events[-1]["seq"]) if events else start
        next_cursor = min(raw_cursor, high_water) if high_water > 0 else raw_cursor
        return {
            "events": safe,
            "cursor": next_cursor,
            "manifest_revision": manifest.revision,
        }

    def _asset_pull_reason(self, tenant_id: str, run_id: str,
                           manifest: DemoManifestPayload,
                           row: dict[str, Any]) -> str | None:
        """Q8：任务内任一已发布来源失效 → 整份已发布成果撤下（返回原因）。

        不再只靠 quote 精确子串：成果正文可以包含未被选为展示片段的同一来源
        内容，无法逐字证明正文不含失效材料时就不能继续对外读。二进制/媒体成果
        本来就不能做内容扫描，因此一律拒绝并要求管理员另发干净 revision。
        """
        dead = [
            r for r in manifest.refs
            if r.run_id == run_id and not self._ref_still_live(tenant_id, r)
        ]
        if not dead:
            return None
        mime = str(row.get("mime") or "")
        if not mime.startswith("text/"):
            return "dead source in run; published bytes cannot be content-verified"
        try:
            self._assets.read_internal_bytes(
                str(row.get("id") or ""), tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 - 字节不可核对时同样失败关闭
            return "dead source in run; published bytes unreadable"
        return "dead source in run; published asset pulled"

    def guest_asset_meta(self, actor: AuthContext, asset_id: str) -> dict[str, Any]:
        _grant, manifest = self._require_grant(actor)
        row = self._store.get_asset(actor.tenant_id, asset_id)
        if row is None:
            # 游客面统一 DENIED→404：不区分"不存在"与"未发布"
            raise AgentOSError(ErrorCode.DENIED, "asset not published to this guest")
        decision = self._access.check(
            actor, "demo.asset.preview", kind="asset", resource_id=asset_id,
            version=int(row.get("version") or 0),
        )
        if not decision.allowed:
            self._audit.record(
                actor.tenant_id, "demo.denied", subject=actor.subject,
                detail={"asset_id": asset_id[:64], "reason": decision.reason},
            )
            raise AgentOSError(ErrorCode.DENIED, decision.reason)
        # Q2/Q8：含失效来源的成果撤下预览
        reason = self._asset_pull_reason(
            actor.tenant_id, str(row.get("run_id") or ""), manifest, row)
        if reason:
            self._audit.record(
                actor.tenant_id, "demo.asset_pulled", subject=actor.subject,
                detail={"asset_id": asset_id[:64], "reason": reason},
            )
            raise AgentOSError(ErrorCode.DENIED, "asset not published to this guest")
        meta = AccessService.project(row, GUEST_ASSET_FIELDS)
        rule = manifest.asset_rule(asset_id, int(row.get("version") or 0))
        meta["download"] = bool(rule.allow_download) if rule else False
        meta["preview"] = bool(rule.allow_preview) if rule else False
        return meta

    def guest_asset_bytes(self, actor: AuthContext, asset_id: str, *,
                         action: str = "demo.asset.download") -> tuple[bytes, dict[str, Any]]:
        """游客读取字节：逐资源在线核权。

        预览与下载是**两个独立判据**（R03）：能预览不等于能下载，因此不能复用
        download 开关服预览；两者都受同一白名单、失效来源撤下与字节校验约束。
        """
        _grant, manifest = self._require_grant(actor)
        row = self._store.get_asset(actor.tenant_id, asset_id)
        if row is None:
            # 游客面统一 DENIED→404：不区分"不存在"与"未发布"
            raise AgentOSError(ErrorCode.DENIED, "asset not published to this guest")
        decision = self._access.check(
            actor, action, kind="asset", resource_id=asset_id,
            version=int(row.get("version") or 0),
        )
        if not decision.allowed:
            self._audit.record(
                actor.tenant_id, "demo.denied", subject=actor.subject,
                detail={"asset_id": asset_id[:64], "action": action,
                        "reason": decision.reason},
            )
            raise AgentOSError(ErrorCode.DENIED, decision.reason)
        # Q2/Q8：含失效来源的成果撤下（预览与下载同口径）
        reason = self._asset_pull_reason(
            actor.tenant_id, str(row.get("run_id") or ""), manifest, row)
        if reason:
            self._audit.record(
                actor.tenant_id, "demo.asset_pulled", subject=actor.subject,
                detail={"asset_id": asset_id[:64], "action": action, "reason": reason},
            )
            raise AgentOSError(ErrorCode.DENIED, "asset not published to this guest")
        data = self._assets.read_internal_bytes(asset_id, tenant_id=actor.tenant_id)
        digest = hashlib.sha256(data).hexdigest()
        if digest != row["sha256"]:
            raise AgentOSError(ErrorCode.UNCERTAIN, "published asset bytes changed")
        self._audit.record(
            actor.tenant_id, "demo.asset_read", subject=actor.subject,
            detail={"asset_id": asset_id, "action": action,
                    "sha256": digest[:16], "size": len(data)},
        )
        return data, {"sha256": digest, "mime": row.get("mime"), "size": len(data)}


def _safe_plan(raw_plan: dict[str, Any]) -> dict[str, Any]:
    """计划投影：只留结构与标题，不带内部参数/提示词。"""
    tasks = []
    for task in raw_plan.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        tasks.append({
            "task_id": task.get("task_id"),
            "title": task.get("title") or task.get("task_id"),
            "role_id": task.get("role_id") or "",
            "depends_on": list(task.get("depends_on") or []),
        })
    return {"plan_id": raw_plan.get("plan_id"), "tasks": tasks}
