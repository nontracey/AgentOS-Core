import "./styles.css";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { lazy, StrictMode, Suspense, useEffect, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Navigate, Route, Routes } from "react-router";
import { Toaster } from "sonner";
import { Shell } from "@/components/Shell";
import { Spinner, TooltipProvider } from "@/components/ui";
import { Login } from "@/features/auth/Login";
import { ChatPage } from "@/features/chat/ChatPage";
import { useTheme } from "@/hooks/useTheme";
import { api, ApiError, hasSession, sessionEvents } from "@/lib/api";

const StudioHome = lazy(() => import("@/features/studio/StudioHome"));
const ProjectPage = lazy(() => import("@/features/studio/ProjectPage"));
const TasksPage = lazy(() => import("@/features/tasks/TasksPage"));
const LibraryPage = lazy(() => import("@/features/library/LibraryPage"));
const DevicesPage = lazy(() => import("@/features/devices/DevicesPage"));
const SettingsPage = lazy(() => import("@/features/settings/SettingsPage"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) => !(error instanceof ApiError && [401, 403, 404, 422].includes(error.status)) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

interface Me {
  subject: string;
  role: "admin" | "member" | "guest" | string;
}

function AuthGate({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState(hasSession());
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/v1/auth/me"), enabled: signedIn, retry: false });

  useEffect(() => {
    const expired = () => {
      setSignedIn(false);
      queryClient.clear();
    };
    sessionEvents.addEventListener("expired", expired);
    return () => sessionEvents.removeEventListener("expired", expired);
  }, []);

  if (!signedIn || me.isError) {
    return (
      <Login
        onSignedIn={() => {
          setSignedIn(true);
          void me.refetch();
        }}
      />
    );
  }
  if (me.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (me.data?.role === "guest") {
    window.location.replace("/ui/legacy/");
    return null;
  }
  return <>{children}</>;
}

function PageLoading() {
  return (
    <div className="flex h-full items-center justify-center">
      <Spinner />
    </div>
  );
}

function App() {
  useTheme();
  return (
    <TooltipProvider>
      <HashRouter>
        <AuthGate>
          <Shell>
            <Suspense fallback={<PageLoading />}>
              <Routes>
                <Route path="/" element={<Navigate to="/chat" replace />} />
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/chat/:id" element={<ChatPage />} />
                <Route path="/studio" element={<StudioHome />} />
                <Route path="/studio/:id" element={<ProjectPage />} />
                <Route path="/studio/:id/:tab" element={<ProjectPage />} />
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/tasks/:id" element={<TasksPage />} />
                <Route path="/library" element={<LibraryPage />} />
                <Route path="/devices" element={<DevicesPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="*" element={<Navigate to="/chat" replace />} />
              </Routes>
            </Suspense>
          </Shell>
        </AuthGate>
      </HashRouter>
      <Toaster
        position="bottom-right"
        toastOptions={{
          style: { background: "var(--surface)", color: "var(--ink)", border: "1px solid var(--line)", borderRadius: 12 },
        }}
      />
    </TooltipProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
