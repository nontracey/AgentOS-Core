"""前端安全回归：令牌存放、连接判定、XSS 转义（旧控制台 + 新工作台双覆盖）。

行为测试用 Node 直接执行 frontend/safe.js 与新 UI 模块的转义/渲染（无 DOM 依赖）；
静态断言守住新旧入口的关键安全约定。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
LEGACY_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
SAFE_JS = (FRONTEND / "safe.js").read_text(encoding="utf-8")
LEGACY_HTML = (FRONTEND / "legacy.html").read_text(encoding="utf-8")
NEW_INDEX = (FRONTEND / "index.html").read_text(encoding="utf-8")
API_JS = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")
UI_JS = (FRONTEND / "js" / "ui.js").read_text(encoding="utf-8")
MAIN_JS = (FRONTEND / "js" / "main.js").read_text(encoding="utf-8")


# ---------------- 新工作台（index.html + js/ 模块） ----------------

def test_new_ui_token_storage_policy() -> None:
    """凭据存储策略：access/粘贴令牌只在 sessionStorage；refresh 保持登录时才进
    localStorage（双 token 轮换 + 服务端撤销），并配 401 自动续期。"""
    assert "sessionStorage" in API_JS
    assert "localStorage" in API_JS  # refresh 持久化（保持登录）
    assert "REFRESH_PERSIST_KEY" in API_JS and "ACCESS_KEY" in API_JS
    # 401 → 静默续期并重放
    assert "refreshSession" in API_JS and "refreshInFlight" in API_JS


def test_new_ui_connect_uses_authenticated_endpoint() -> None:
    assert '"/healthz"' not in API_JS, "无鉴权 /healthz 不能作为已连接依据"
    assert "/v1/runs" in API_JS


def test_new_ui_downloads_never_put_token_in_url() -> None:
    assert "?token=" not in API_JS + UI_JS + MAIN_JS + LEGACY_JS, \
        "主令牌不得出现在 URL（signed-url / 鉴权 fetch 替代）"
    assert "signed-url" in LEGACY_JS  # 旧控制台也已切换


def test_new_ui_dynamic_text_escaped() -> None:
    # 用户可控文本插值必须过 esc（goal/审批摘要/错误）——扫全部工作区模块
    js_files = [Path(p) for p in sorted(
        str(x) for x in (FRONTEND / "js").rglob("*.js") if "sw.js" not in x.name)]
    corpus = UI_JS + MAIN_JS + "".join(p.read_text(encoding="utf-8") for p in js_files)
    for needle in (
        'esc(run.goal)',                      # 任务卡目标
        'esc(shortGoal(run.goal_text || run.goal',  # 详情短标题仍须转义
        'esc(what)',                          # 审批摘要（approvalCard，a.summary 归一）
        'esc(e.message)',                     # 错误提示
    ):
        assert needle in corpus, f"缺少转义插值: {needle}"


def test_legacy_entry_still_loads_safe_first() -> None:
    assert 'src="/ui/safe.js"' in LEGACY_HTML
    assert LEGACY_HTML.index("/ui/safe.js") < LEGACY_HTML.index("/ui/app.js")


def test_legacy_injection_points_escaped() -> None:
    for needle in (
        "esc((r.goal || r.id).slice(0, 26))",
        "esc(run.goal_text || run.id)",
        "esc(run.error_json)",
        "esc(a.summary)",
        "esc(JSON.stringify(last.payload.delivery, null, 2))",
    ):
        assert needle in LEGACY_JS, f"缺少转义插值: {needle}"


# ---------------- 行为测试（Node 执行转义与渲染纯函数） ----------------

def _run_node(expr: str) -> str:
    node = shutil.which("node")
    assert node, "需要 node 执行行为测试"
    eval_src = SAFE_JS + f"\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.run(
        [node, "-e", f"eval({json.dumps(eval_src)})"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return out.stdout.strip()


def test_esc_neutralizes_html_injection() -> None:
    payloads = [
        "<img src=x onerror=alert(1)>",
        "<script>fetch('/steal?'+document.cookie)</script>",
        "').appendChild(document.createElement('script'))//",
    ]
    result = json.loads(_run_node(f"{json.dumps(payloads)}.map(esc)"))
    for escaped in result:
        assert "<" not in escaped and ">" not in escaped
        assert any(tok in escaped for tok in ("&lt;", "&amp;", "&#39;"))


def test_jsq_neutralizes_attr_context_escape() -> None:
    evil = "x'); alert('pwned"
    result = json.loads(_run_node(f"jsq({json.dumps(evil)})"))
    assert "'" not in result
    result2 = json.loads(_run_node(f"jsq({json.dumps('<img onerror=&quot;x&quot;>')})"))
    assert "'" not in result2 and "<" not in result2 and ">" not in result2


def test_esc_roundtrip_safe_text_unchanged() -> None:
    result = json.loads(_run_node("[esc('杭州 西湖 雨天 2026'), jsq('正常-id_123')]"))
    assert result == ["杭州 西湖 雨天 2026", "正常-id_123"]


def test_md_renderer_blocks_html_and_js_links() -> None:
    """成果预览的 Markdown 渲染：原始 HTML 不执行、javascript: 链接被拦截。"""
    node = shutil.which("node")
    assert node
    script = (
        "import('file://" + str(FRONTEND / "js" / "md.js") + "').then(({render}) => {"
        "const out1 = render('<img src=x onerror=alert(1)>');"
        "const out2 = render('[x](javascript:alert(1))');"
        "const out3 = render('[ok](https://example.com)');"
        "console.log(JSON.stringify([out1, out2, out3]));})"
    )
    out = subprocess.run([node, "--input-type=module", "-e", script],
                         capture_output=True, text=True, timeout=30, check=True)
    o1, o2, o3 = json.loads(out.stdout.strip())
    # 原始标签必须被实体化（&lt;img …&gt; 以纯文本显示，不再构成元素/属性）
    assert "<img src=x" not in o1 and "&lt;img" in o1
    # javascript: 链接必须被拦截（不得生成 href="javascript:…"）
    assert 'href="javascript:' not in o2 and "javascript:" not in o2.replace("&gt;", ">").split("title=")[0]
    assert 'href="https://example.com"' in o3
