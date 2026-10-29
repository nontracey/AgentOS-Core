import { KeyRound } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, storeSession, storeStaticToken } from "@/lib/api";
import { Button, ErrorNote } from "@/components/ui";

export function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [token, setToken] = useState("");
  const [useToken, setUseToken] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (useToken) {
        storeStaticToken(token.trim());
        await api("/v1/auth/me");
      } else {
        const body = await api<{ access_token: string; refresh_token: string }>("/v1/auth/login", {
          body: { password, remember },
        });
        storeSession(body.access_token, body.refresh_token, remember);
      }
      onSignedIn();
    } catch (err) {
      setError((err as Error).message || "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative flex min-h-full items-center justify-center overflow-hidden px-4 py-10">
      <div
        aria-hidden
        className="animate-rim pointer-events-none absolute top-1/2 left-1/2 h-[560px] w-[560px] -translate-x-1/2 -translate-y-[58%] rounded-full opacity-70 blur-3xl"
        style={{ background: "radial-gradient(circle, rgb(233 169 58 / 0.32), transparent 62%)" }}
      />
      <form
        onSubmit={submit}
        className="animate-rise relative w-full max-w-[380px] rounded-[22px] border border-line bg-surface p-8 shadow-[var(--shadow-pop)]"
      >
        <div className="mb-8">
          <div className="text-[26px] leading-none font-semibold tracking-tight">AgentOS</div>
          <div className="mt-2 text-[13.5px] text-ink-3">本地推理与制作工作室</div>
        </div>
        {useToken ? (
          <label className="flex flex-col gap-1.5">
            <span className="text-[13px] font-medium text-ink-2">访问令牌</span>
            <input className="field font-mono text-[13px]" value={token} onChange={(e) => setToken(e.target.value)} autoFocus />
          </label>
        ) : (
          <label className="flex flex-col gap-1.5">
            <span className="text-[13px] font-medium text-ink-2">密码</span>
            <input
              className="field h-11"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
            />
          </label>
        )}
        {!useToken && (
          <label className="mt-3.5 flex items-center gap-2 text-[13px] text-ink-2 select-none">
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} className="size-4 accent-[var(--accent)]" />
            30 天内保持登录
          </label>
        )}
        {error && <div className="mt-4"><ErrorNote>{error}</ErrorNote></div>}
        <Button type="submit" variant="primary" size="lg" className="mt-6 w-full" loading={busy} disabled={useToken ? !token : !password}>
          登录
        </Button>
        <div className="mt-5 flex items-center justify-between text-[12.5px] text-ink-3">
          <button type="button" onClick={() => setUseToken(!useToken)} className="inline-flex items-center gap-1 hover:text-ink">
            <KeyRound className="size-3.5" />
            {useToken ? "用密码登录" : "用访问令牌登录"}
          </button>
          <a href="/ui/legacy/" className="hover:text-ink">旧版界面</a>
        </div>
        <p className="mt-5 border-t border-line pt-4 text-[12px] leading-relaxed text-ink-3">
          第一次使用时，初始密码在服务数据目录的 owner_password.once 文件里。
        </p>
      </form>
    </div>
  );
}
