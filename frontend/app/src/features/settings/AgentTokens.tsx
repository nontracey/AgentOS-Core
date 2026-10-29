import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, KeyRound, Plus } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { cn } from "@/lib/format";
import { Badge, Button, Dialog, ErrorNote, Field, Select, Switch } from "@/components/ui";

interface AgentToken {
  id: string;
  subject: string;
  label: string;
  scopes: string[];
  created_at: string;
  expires_at: string;
  revoked: boolean;
  active: boolean;
  token?: string;
}

interface AgentTokensPayload {
  tokens: AgentToken[];
  mcp_url: string;
  scopes: string[];
}

const SCOPE_TEXT: Record<string, string> = { read: "查询", write: "写入", exec: "执行命令", egress: "外发" };
const OPTIONAL_SCOPES = [
  { id: "write", title: "写入", hint: "任务可以调用写文件、发布工作流这类写入工具，每一步仍要你批准。" },
  { id: "exec", title: "执行命令", hint: "任务可以调用执行命令的工具，每一步仍要你批准。" },
  { id: "egress", title: "外发", hint: "任务可以调用把数据发到外部的工具，每一步仍要你批准。" },
];

function expiresText(token: AgentToken): string {
  if (token.revoked) return "已撤销";
  const left = (new Date(token.expires_at).getTime() - Date.now()) / 86_400_000;
  if (!Number.isFinite(left)) return "";
  if (left <= 0) return "已过期";
  return left < 1 ? "今天到期" : `还有 ${Math.ceil(left)} 天`;
}

function CopyBlock({ label, text }: { label: string; text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div>
      <div className="mb-1.5 flex items-start justify-between gap-3 text-[12.5px] text-ink-3">
        <span className="min-w-0 leading-relaxed">{label}</span>
        <button
          className="inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 whitespace-nowrap hover:bg-surface-3 hover:text-ink"
          onClick={() => {
            void navigator.clipboard?.writeText(text).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="overflow-x-auto rounded-[10px] bg-surface-2 px-3 py-2.5 font-mono text-[12px] leading-relaxed whitespace-pre-wrap break-all">{text}</pre>
    </div>
  );
}

function IssuedDialog({ token, mcpUrl, onClose }: { token: AgentToken | null; mcpUrl: string; onClose: () => void }) {
  if (!token?.token) return null;
  const secret = token.token;
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={`「${token.label}」的令牌`}
      description="令牌只显示这一次，关闭后无法再查看。丢了就撤销再新建一个。"
      width={620}
      footer={
        <Button variant="primary" onClick={onClose}>
          我已保存
        </Button>
      }
    >
      <div className="flex flex-col gap-4 pb-2">
        <CopyBlock label="令牌" text={secret} />
        <CopyBlock label="Claude Code：在终端运行" text={`claude mcp add --transport http agentos ${mcpUrl} --header "Authorization: Bearer ${secret}"`} />
        <CopyBlock
          label="Codex：写进 ~/.codex/config.toml，并在启动 Codex 的终端里设置 AGENTOS_TOKEN"
          text={`[mcp_servers.agentos]\nurl = "${mcpUrl}"\nbearer_token_env_var = "AGENTOS_TOKEN"\n\n# 终端里：\nexport AGENTOS_TOKEN="${secret}"`}
        />
        <p className="text-[12.5px] leading-relaxed text-ink-3">
          工作台只在这台电脑上监听，所以只有同一台电脑上的 Agent 能连上。令牌不要贴进聊天、文档或代码仓库。
        </p>
      </div>
    </Dialog>
  );
}

function CreateDialog({ open, onOpenChange, onIssued }: { open: boolean; onOpenChange: (v: boolean) => void; onIssued: (t: AgentToken) => void }) {
  const [label, setLabel] = useState("");
  const [scopes, setScopes] = useState<string[]>(["write"]);
  const [days, setDays] = useState("30");
  const [error, setError] = useState("");
  const create = useMutation({
    mutationFn: () => api<AgentToken>("/v1/agent-tokens", { body: { label, scopes: ["read", ...scopes], days: Number(days) } }),
    onSuccess: (token) => {
      setLabel("");
      setScopes(["write"]);
      setError("");
      onOpenChange(false);
      onIssued(token);
    },
    onError: (err) => setError((err as Error).message),
  });
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="新建 Agent 令牌"
      description="每个 Agent 用一个令牌，方便单独撤销。它是独立的身份：能查询和提交任务，不能批准审批，也不能管理设备、令牌和授权。"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button variant="primary" loading={create.isPending} disabled={!label.trim()} onClick={() => create.mutate()}>
            新建令牌
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-5 pb-2">
        <Field label="名称" hint="写上谁会用它，例如“Mac 上的 Claude Code”。">
          <input className="field h-10" value={label} maxLength={40} onChange={(e) => setLabel(e.target.value)} autoFocus />
        </Field>
        <div className="flex flex-col gap-3">
          <span className="text-[13px] font-medium text-ink-2">权限（查询始终包含）</span>
          {OPTIONAL_SCOPES.map((scope) => (
            <label key={scope.id} className="flex items-center justify-between gap-4">
              <span>
                <span className="block text-[13.5px] font-medium">{scope.title}</span>
                <span className="block text-[12px] text-ink-3">{scope.hint}</span>
              </span>
              <Switch
                checked={scopes.includes(scope.id)}
                onChange={(on) => setScopes((current) => (on ? [...current, scope.id] : current.filter((s) => s !== scope.id)))}
              />
            </label>
          ))}
        </div>
        <Field label="有效期">
          <Select
            value={days}
            onChange={setDays}
            options={[
              { value: "7", label: "7 天" },
              { value: "30", label: "30 天" },
              { value: "90", label: "90 天" },
              { value: "365", label: "1 年" },
            ]}
          />
        </Field>
        {error && <ErrorNote>{error}</ErrorNote>}
      </div>
    </Dialog>
  );
}

/** Owner-only section of the settings page: tokens for Claude Code, Codex and other MCP clients. */
export function AgentTokens() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["agent-tokens"], queryFn: () => api<AgentTokensPayload>("/v1/agent-tokens"), retry: false });
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<AgentToken | null>(null);
  const revoke = useMutation({
    mutationFn: (id: string) => api<AgentToken>(`/v1/agent-tokens/${id}/revoke`, { method: "POST" }),
    onSuccess: (token) => {
      toast.success(`已撤销「${token.label}」`);
      void qc.invalidateQueries({ queryKey: ["agent-tokens"] });
    },
    onError: (err) => toast.error((err as Error).message),
  });
  if (list.isError) return null; // only the owner manages agent tokens
  const tokens = list.data?.tokens || [];
  return (
    <div className="border-b border-line py-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="text-[14px] font-medium">外部 Agent 接入</div>
          <div className="mt-0.5 max-w-md text-[12.5px] leading-relaxed text-ink-3">
            让 Claude Code、Codex 等支持 MCP 的 Agent 查询和提交任务。需要审批的步骤仍由你在这里批准，随时可以撤销。
          </div>
        </div>
        <Button variant="outline" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
          新建令牌
        </Button>
      </div>
      {tokens.length > 0 && (
        <ul className="mt-4 flex flex-col divide-y divide-line rounded-[12px] border border-line">
          {tokens.map((token) => (
            <li key={token.id} className="flex items-center gap-3 px-3.5 py-3">
              <KeyRound className={cn("size-4 shrink-0", token.active ? "text-ink-2" : "text-ink-3")} />
              <div className="min-w-0 flex-1">
                <div className={cn("truncate text-[13.5px] font-medium", !token.active && "text-ink-3")}>{token.label}</div>
                <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3">
                  {token.scopes.map((s) => (
                    <Badge key={s} tone="outline">
                      {SCOPE_TEXT[s] || s}
                    </Badge>
                  ))}
                  <span>{expiresText(token)}</span>
                </div>
              </div>
              {token.active && (
                <Button
                  size="sm"
                  variant="ghost"
                  loading={revoke.isPending && revoke.variables === token.id}
                  onClick={() => {
                    if (window.confirm(`撤销「${token.label}」后，用它的 Agent 立即失去访问，进行中的任务后续步骤也会被拒绝。确定吗？`)) revoke.mutate(token.id);
                  }}
                >
                  撤销
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      <CreateDialog
        open={creating}
        onOpenChange={setCreating}
        onIssued={(token) => {
          setIssued(token);
          void qc.invalidateQueries({ queryKey: ["agent-tokens"] });
        }}
      />
      <IssuedDialog token={issued} mcpUrl={list.data?.mcp_url || ""} onClose={() => setIssued(null)} />
    </div>
  );
}
