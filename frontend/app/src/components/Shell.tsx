import { Clapperboard, Cpu, FolderOpen, ListChecks, MessageSquare, PanelLeftClose, PanelLeftOpen, Settings } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router";
import { subscribeLive, lastGpu } from "@/lib/live";
import type { GpuSnapshot } from "@/lib/types";
import { cn, timecode } from "@/lib/format";
import { useIsPhone } from "@/hooks/useMedia";
import { Tooltip } from "./ui";

const NAV = [
  { to: "/chat", label: "对话", icon: MessageSquare },
  { to: "/studio", label: "工作室", icon: Clapperboard },
  { to: "/tasks", label: "任务", icon: ListChecks },
  { to: "/library", label: "成果", icon: FolderOpen },
  { to: "/devices", label: "设备", icon: Cpu },
];

export function useGpu(): GpuSnapshot | null {
  const [gpu, setGpu] = useState<GpuSnapshot | null>(lastGpu);
  const [, tick] = useState(0);
  useEffect(() => {
    const off = subscribeLive((event) => {
      if (event.type === "hello" || event.type === "gpu") setGpu(event.gpu);
      if (event.type === "job" && event.job.gpu) {
        // Job transitions change the holder; the next heartbeat brings exact numbers.
        setGpu((current) => {
          if (event.job.state === "running")
            return { busy: true, holder: { kind: "studio", label: event.job.label, elapsed_seconds: 0, job_id: event.job.id }, waiting: current?.waiting || [] };
          if (current?.holder?.job_id === event.job.id) return { busy: false, holder: null, waiting: current.waiting };
          return current;
        });
      }
    });
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => {
      off();
      clearInterval(timer);
    };
  }, []);
  return gpu;
}

function GpuStatus({ compact }: { compact?: boolean }) {
  const gpu = useGpu();
  const [since] = useState(() => Date.now());
  const busy = Boolean(gpu?.busy && gpu.holder);
  const elapsed = busy && gpu?.holder ? gpu.holder.elapsed_seconds + (Date.now() - since) / 1000 : 0;
  const text = !gpu ? "GPU 状态读取中" : busy ? gpu.holder!.label : "GPU 空闲";
  const waiting = gpu?.waiting.length || 0;
  const body = (
    <NavLink
      to="/devices"
      className={cn(
        "group flex items-center gap-2.5 rounded-[10px] px-2.5 py-2 text-[12.5px] transition-colors hover:bg-surface-3",
        busy && "bg-accent-soft hover:bg-accent-soft",
      )}
    >
      <span className="relative flex size-2.5 shrink-0">
        {busy && <span className="animate-rim absolute inset-[-3px] rounded-full bg-accent/40" />}
        <span className={cn("relative size-2.5 rounded-full", busy ? "bg-accent" : gpu ? "bg-green" : "bg-line-strong")} />
      </span>
      {!compact && (
        <span className="min-w-0 flex-1">
          <span className={cn("block truncate font-medium", busy ? "text-accent-ink" : "text-ink-2")}>{text}</span>
          {busy && (
            <span className="tabular block text-[11.5px] text-ink-3">
              {timecode(elapsed)}
              {waiting > 0 && `　排队 ${waiting}`}
            </span>
          )}
        </span>
      )}
    </NavLink>
  );
  return compact ? <Tooltip content={text}>{body}</Tooltip> : body;
}

export function Shell({ children }: { children: ReactNode }) {
  const phone = useIsPhone();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("agentos_rail") === "collapsed";
    } catch {
      return false;
    }
  });
  const toggle = () => {
    setCollapsed((value) => {
      try {
        localStorage.setItem("agentos_rail", value ? "open" : "collapsed");
      } catch {
        /* ignore */
      }
      return !value;
    });
  };

  if (phone) {
    return (
      <div className="flex h-dvh flex-col">
        <main className="min-h-0 flex-1 overflow-hidden bg-surface">{children}</main>
        <nav className="grid grid-cols-5 border-t border-line bg-surface pb-[env(safe-area-inset-bottom)]">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn("flex flex-col items-center gap-0.5 py-2 text-[11px] text-ink-3", isActive && "text-ink")
              }
            >
              {({ isActive }) => (
                <>
                  <Icon className={cn("size-5", isActive && "text-accent-ink")} strokeWidth={isActive ? 2.2 : 1.8} />
                  {label}
                </>
              )}
            </NavLink>
          ))}
        </nav>
      </div>
    );
  }

  return (
    <div className="flex h-dvh">
      <aside className={cn("flex shrink-0 flex-col py-3 transition-[width] duration-200", collapsed ? "w-[68px] px-2.5" : "w-[220px] px-3")}>
        <div className={cn("mb-5 flex h-9 items-center", collapsed ? "justify-center" : "justify-between pl-2")}>
          {!collapsed && <span className="text-[17px] font-semibold tracking-tight">AgentOS</span>}
          <button
            onClick={toggle}
            aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
            className="rounded-md p-1.5 text-ink-3 hover:bg-surface-3 hover:text-ink"
          >
            {collapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeftClose className="size-4" />}
          </button>
        </div>
        <nav className="flex flex-col gap-0.5">
          {NAV.map(({ to, label, icon: Icon }) => {
            const link = (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  cn(
                    "relative flex h-9 items-center gap-3 rounded-[10px] text-[14px] font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink",
                    collapsed ? "justify-center" : "px-3",
                    isActive && "bg-surface text-ink shadow-[0_1px_2px_rgb(0_0_0/0.06)] hover:bg-surface",
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && <span className="absolute top-2 bottom-2 left-0 w-[3px] rounded-full bg-accent" />}
                    <Icon className="size-[18px]" strokeWidth={isActive ? 2.1 : 1.8} />
                    {!collapsed && label}
                  </>
                )}
              </NavLink>
            );
            return collapsed ? (
              <Tooltip key={to} content={label} side="right">
                {link}
              </Tooltip>
            ) : (
              link
            );
          })}
        </nav>
        <div className="mt-auto flex flex-col gap-1">
          <GpuStatus compact={collapsed} />
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              cn(
                "flex h-9 items-center gap-3 rounded-[10px] text-[14px] text-ink-2 hover:bg-surface-3 hover:text-ink",
                collapsed ? "justify-center" : "px-3",
                isActive && "text-ink",
              )
            }
          >
            <Settings className="size-[18px]" strokeWidth={1.8} />
            {!collapsed && "设置"}
          </NavLink>
        </div>
      </aside>
      <main key={location.pathname.split("/")[1]} className="my-2 mr-2 min-w-0 flex-1 overflow-hidden rounded-[var(--radius-panel)] border border-line bg-surface">
        {children}
      </main>
    </div>
  );
}
