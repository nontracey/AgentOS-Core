import { ExternalLink, LogOut, Monitor, Moon, Sun } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { api, clearSession } from "@/lib/api";
import { Button, ErrorNote, Field, Segmented } from "@/components/ui";
import { useTheme, type ThemeChoice } from "@/hooks/useTheme";
import { ModelPicker } from "@/features/chat/ModelPicker";
import { AgentTokens } from "./AgentTokens";

function Row({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-3 border-b border-line py-5 last:border-b-0 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <div className="text-[14px] font-medium">{title}</div>
        {hint && <div className="mt-0.5 max-w-md text-[12.5px] leading-relaxed text-ink-3">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

export default function SettingsPage() {
  const [theme, setTheme] = useTheme();
  const [model, setModel] = useState(() => {
    try {
      return localStorage.getItem("agentos_chat_model") || "";
    } catch {
      return "";
    }
  });
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function changePassword() {
    setError("");
    setBusy(true);
    try {
      await api("/v1/auth/password", { body: { current_password: current, new_password: next } });
      toast.success("密码已修改，请重新登录");
      clearSession();
      setTimeout(() => window.location.reload(), 800);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[760px] px-6 py-7 md:px-10">
        <h1 className="text-[24px] font-semibold tracking-tight">设置</h1>
        <div className="mt-4">
          <Row title="外观" hint="默认跟随系统的浅色或深色。">
            <Segmented<ThemeChoice>
              value={theme}
              onChange={setTheme}
              options={[
                { value: "system", label: "跟随系统", icon: <Monitor className="size-3.5" /> },
                { value: "light", label: "浅色", icon: <Sun className="size-3.5" /> },
                { value: "dark", label: "深色", icon: <Moon className="size-3.5" /> },
              ]}
            />
          </Row>
          <Row title="新对话默认模型" hint="每条对话也可以单独选择模型。">
            <ModelPicker
              value={model}
              align="end"
              onChange={(ref) => {
                setModel(ref);
                try {
                  localStorage.setItem("agentos_chat_model", ref);
                } catch {
                  /* ignore */
                }
                toast.success("已保存");
              }}
            />
          </Row>
          <AgentTokens />
          <Row title="更多管理功能" hint="能力中心（MCP、Skills、知识库）、工作流编辑器和演示访问仍在旧版界面里。">
            <a href="/ui/legacy/" className="inline-flex h-9 items-center gap-2 rounded-[8px] border border-line-strong px-3.5 text-[14px] hover:bg-surface-2">
              打开旧版界面
              <ExternalLink className="size-3.5 text-ink-3" />
            </a>
          </Row>
          <div className="border-b border-line py-5">
            <div className="text-[14px] font-medium">修改密码</div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <Field label="当前密码">
                <input type="password" className="field h-10" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
              </Field>
              <Field label="新密码" hint="至少 8 位。修改后所有设备需要重新登录。">
                <input type="password" className="field h-10" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
              </Field>
            </div>
            {error && (
              <div className="mt-3">
                <ErrorNote>{error}</ErrorNote>
              </div>
            )}
            <Button className="mt-3" variant="primary" loading={busy} disabled={!current || next.length < 8} onClick={() => void changePassword()}>
              修改密码
            </Button>
          </div>
          <Row title="退出登录">
            <Button
              variant="outline"
              icon={<LogOut className="size-4" />}
              onClick={() => {
                const refresh = localStorage.getItem("agentos_refresh") || sessionStorage.getItem("agentos_refresh_session") || "";
                void api("/v1/auth/logout", { body: { refresh_token: refresh } }).finally(() => {
                  clearSession();
                  window.location.reload();
                });
              }}
            >
              退出
            </Button>
          </Row>
        </div>
      </div>
    </div>
  );
}
