"""SkillHub（FR-SKL-01/02/03）：SKILL.md 开放格式，三段式渐进加载。

- 元数据（name/description）常驻用于发现；正文命中后按需加载；资产更深一层按需读取。
- 安全校验：来源路径/大小限制、SKILL.md 必须存在、资产路径穿越拒绝、脚本视为程序并记录。
- 安装不等于授权：allowed_tools 只是声明，执行权限始终来自 Policy（ADR-0012）。
- 版本以内容 digest 固定：run 固定版本/hash，升级不影响在途任务（FR-SKL-03 可回滚）。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol
from agentos.kernel.types import AuthContext

MAX_SKILL_BYTES = 2 * 1024 * 1024  # 单文件上限 2MiB
MAX_SKILL_FILES = 200
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class SkillMeta:
    id: str
    version: str
    digest: str
    description: str
    source: str
    allowed_tools: list[str]
    entrypoints: list[str]
    install_path: str


def _parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md：YAML-lite frontmatter（name/description/allowed_tools/entrypoints）+ 正文。

    只解析标准核心字段（FR-SKL-02 跨客户端可移植）；不引入 YAML 依赖，frontmatter
    使用 `key: value` 与逗号列表的受支持子集，未识别字段忽略并保留在正文中。
    """
    meta: dict[str, str] = {}
    body = text
    match = _FRONTMATTER.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "#")):
                key, _, value = line.partition(":")
                meta[key.strip().lower()] = value.strip()
        body = text[match.end():]
    return meta, body.strip()


class SkillService:
    def __init__(self, store: StoreProtocol, skills_dir: Path) -> None:
        self._store = store
        self._dir = skills_dir

    # ------------------------------------------------------------ install
    def install(self, actor: AuthContext, source: str) -> SkillMeta:
        """从本地目录安装技能包（M1：本地目录起步；git revision 走同一入口传入快照路径）。"""
        src = Path(source).expanduser().resolve()
        skill_md = src / "SKILL.md"
        if not skill_md.is_file():
            raise AgentOSError(ErrorCode.INVALID, f"SKILL.md not found in {src}")
        files = sorted(p for p in src.rglob("*") if p.is_file())
        if any(p.is_symlink() or not p.resolve().is_relative_to(src) for p in files):
            raise AgentOSError(ErrorCode.INVALID, "skill files must stay inside the package")
        if len(files) > MAX_SKILL_FILES:
            raise AgentOSError(ErrorCode.INVALID, f"too many files: {len(files)}")
        total = 0
        digest = hashlib.sha256()
        for f in files:
            if f.stat().st_size > MAX_SKILL_BYTES:
                raise AgentOSError(ErrorCode.INVALID, "skill file too large")
            total += f.stat().st_size
            digest.update(f.relative_to(src).as_posix().encode())
            digest.update(hashlib.sha256(f.read_bytes()).digest())
        if total > MAX_SKILL_BYTES * MAX_SKILL_FILES:
            raise AgentOSError(ErrorCode.INVALID, "skill package too large")

        meta, _body = _parse_skill_md(skill_md.read_text(encoding="utf-8"))
        name = meta.get("name") or src.name
        skill_id = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")
        if not skill_id:
            raise AgentOSError(ErrorCode.INVALID, "skill name invalid")
        version = meta.get("version") or digest.hexdigest()[:8]
        description = meta.get("description", "")
        allowed_tools = [t.strip() for t in meta.get("allowed_tools", "").split(",") if t.strip()]
        entrypoints = [e.strip() for e in meta.get("entrypoints", "").split(",") if e.strip()]

        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", version) or version in {".", ".."}:
            raise AgentOSError(ErrorCode.INVALID, "invalid skill version")
        dest = self._dir / hashlib.sha256(actor.tenant_id.encode()).hexdigest()[:16] / skill_id / version
        existing = next((r for r in self._store.list_skills(actor.tenant_id, enabled_only=False)
                         if r["id"] == skill_id and r["version"] == version), None)
        if existing and existing["digest"] != digest.hexdigest()[:16]:
            raise AgentOSError(ErrorCode.CONFLICT, "skill version is immutable; choose a new version")
        if dest.exists() and not existing:
            raise AgentOSError(ErrorCode.CONFLICT, "skill version path already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copytree(src, dest)

        record = {
            "tenant_id": actor.tenant_id, "id": skill_id, "version": version,
            "digest": digest.hexdigest()[:16], "description": description,
            "source": str(src), "install_path": str(dest),
            "allowed_tools_json": json_dumps(allowed_tools),
            "entrypoints_json": json_dumps(entrypoints),
        }
        self._store.upsert_skill(record)
        self._store.audit_append(
            actor.tenant_id, "skill.installed", subject=actor.subject, run_id=None,
            detail={"skill_id": skill_id, "version": version, "digest": record["digest"]},
        )
        return SkillMeta(
            id=skill_id, version=version, digest=record["digest"], description=description,
            source=str(src), allowed_tools=allowed_tools, entrypoints=entrypoints,
            install_path=str(dest),
        )

    def install_text(self, actor: AuthContext, text: str) -> SkillMeta:
        if not text.strip() or len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            raise AgentOSError(ErrorCode.INVALID, "SKILL.md is empty or too large")
        meta, _ = _parse_skill_md(text)
        if not meta.get("name"):
            raise AgentOSError(ErrorCode.INVALID, "SKILL.md requires a name in frontmatter")
        self._dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="agentos-skill-", dir=self._dir) as temporary:
            source = Path(temporary)
            (source / "SKILL.md").write_text(text, encoding="utf-8")
            return self.install(actor, str(source))

    # ------------------------------------------------------ 渐进加载三段
    def list(self, actor: AuthContext, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        """阶段 1：仅元数据常驻（发现用）。"""
        return [
            {
                "id": r["id"], "version": r["version"], "digest": r["digest"],
                "description": r["description"], "enabled": bool(r["enabled"]),
                "allowed_tools": json_loads(r["allowed_tools_json"]),
            }
            for r in self._store.list_skills(actor.tenant_id, enabled_only=not include_disabled)
        ]

    def load_body(self, actor: AuthContext, skill_id: str, version: str | None = None) -> str:
        """阶段 2：正文按需加载（命中后才注入上下文）。"""
        record = self._require(actor.tenant_id, skill_id, version)
        base = Path(record["install_path"])
        digest = hashlib.sha256()
        for file in sorted(p for p in base.rglob("*") if p.is_file()):
            if file.is_symlink() or not file.resolve().is_relative_to(base.resolve()):
                raise AgentOSError(ErrorCode.DENIED, "installed skill file escapes its package")
            digest.update(file.relative_to(base).as_posix().encode())
            digest.update(hashlib.sha256(file.read_bytes()).digest())
        if digest.hexdigest()[:16] != record["digest"]:
            raise AgentOSError(ErrorCode.CONFLICT, "installed skill content no longer matches its fixed version")
        text = (base / "SKILL.md").read_text(encoding="utf-8")
        _meta, body = _parse_skill_md(text)
        self._store.audit_append(
            actor.tenant_id, "skill.body_loaded", subject=actor.subject, run_id=None,
            detail={"skill_id": skill_id, "version": record["version"]},
        )
        return body

    def read_asset(self, actor: AuthContext, skill_id: str, rel_path: str) -> bytes:
        """阶段 3：资产按需读取；路径穿越拒绝（沙箱约束与工具层一致）。"""
        record = self._require(actor.tenant_id, skill_id, None)
        base = Path(record["install_path"]).resolve()
        if not rel_path or Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
            raise AgentOSError(ErrorCode.INVALID, f"path escapes skill dir: {rel_path!r}")
        target = (base / rel_path).resolve()
        if not str(target).startswith(str(base)):
            raise AgentOSError(ErrorCode.INVALID, f"path escapes skill dir: {rel_path!r}")
        if not target.is_file():
            raise AgentOSError(ErrorCode.INVALID, f"asset not found: {rel_path}")
        if target.stat().st_size > MAX_SKILL_BYTES:
            raise AgentOSError(ErrorCode.INVALID, f"asset too large: {rel_path}")
        return target.read_bytes()

    # ------------------------------------------------------------- remove
    def set_enabled(
        self, actor: AuthContext, skill_id: str, version: str, enabled: bool
    ) -> dict[str, Any]:
        """启用/停用某个已安装版本：安装 ≠ 授权，停用后不再进入可用清单。

        已固定的 run 是否继续使用，由调用方按 run 侧记录的版本与准入规则裁定
        （run 的 checkpoint 记的是版本字符串，不会被这里改写）。
        """
        row = self._require(actor.tenant_id, skill_id, version)
        ok = self._store.set_skill_enabled(actor.tenant_id, skill_id, str(row["version"]), enabled)
        if not ok:
            raise AgentOSError(ErrorCode.INVALID, f"skill not found: {skill_id}@{version}")
        self._store.audit_append(
            actor.tenant_id, "skill.toggle", subject=actor.subject, run_id=None,
            detail={"skill_id": skill_id, "version": version, "enabled": enabled},
        )
        return {"skill_id": skill_id, "version": version, "enabled": enabled}

    def remove(self, actor: AuthContext, skill_id: str, version: str | None = None) -> int:
        rows = self._store.list_skills(actor.tenant_id, enabled_only=False)
        targets = [r for r in rows if r["id"] == skill_id and (version is None or r["version"] == version)]
        if not targets:
            raise AgentOSError(ErrorCode.INVALID, f"skill not found: {skill_id}")
        for r in targets:
            shutil.rmtree(r["install_path"], ignore_errors=True)
        self._store.delete_skills(actor.tenant_id, skill_id, version)
        self._store.audit_append(
            actor.tenant_id, "skill.removed", subject=actor.subject, run_id=None,
            detail={"skill_id": skill_id, "version": version},
        )
        return len(targets)

    def _require(self, tenant_id: str, skill_id: str, version: str | None) -> dict[str, Any]:
        rows = [r for r in self._store.list_skills(tenant_id, enabled_only=False) if r["id"] == skill_id]
        if version:
            rows = [r for r in rows if r["version"] == version]
        if not rows:
            raise AgentOSError(ErrorCode.INVALID, f"skill not found: {skill_id}")
        return rows[0]


def json_dumps(value: list[str]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def json_loads(value: str) -> list[str]:
    import json

    loaded: list[str] = json.loads(value)
    return loaded
