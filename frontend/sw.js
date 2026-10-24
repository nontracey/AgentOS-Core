/* AgentOS 工作台 Service Worker：静态外壳缓存（离线可开 UI），API 不缓存。
 * v57：固定本地图片配方；升级预取绕过浏览器旧 HTTP 缓存。
 * API/私有素材永不进入外壳缓存（仅缓存 /ui/ 静态资源）。 */
const SHELL = "agentos-shell-v58";
const SHELL_FILES = [
  "/ui/", "/ui/index.html", "/ui/legacy.html",
  "/ui/style.css", "/ui/workspace.css", "/ui/console.css",
  "/ui/safe.js", "/ui/app.js",
  "/ui/js/workspace-data.js", "/ui/js/main.js", "/ui/js/api.js", "/ui/js/ui.js", "/ui/js/md.js",
  "/ui/js/composer.js", "/ui/js/live.js", "/ui/js/theme.js", "/ui/js/safe-export.js", "/ui/js/execution.js", "/ui/js/conversation.js", "/ui/js/preview.js",
  "/ui/js/views/home.js", "/ui/js/views/tasks.js", "/ui/js/views/thread.js",
  "/ui/js/views/task-new.js", "/ui/js/views/library.js", "/ui/js/views/hub.js",
  "/ui/js/views/aigc.js", "/ui/js/views/aigc-production.js", "/ui/js/views/aigc-face-picker.js",
  "/ui/js/views/inbox.js", "/ui/js/views/settings.js",
  "/ui/js/views/guest.js", "/ui/js/views/demo-admin.js",
  "/ui/icon-192.png", "/ui/icon-512.png", "/ui/manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) =>
    c.addAll(SHELL_FILES.map((url) => new Request(url, { cache: "reload" })))
  ).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k.startsWith("agentos-shell-") && k !== SHELL).map((k) => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener("message", (e) => {
  if (e.data?.type === "ACTIVATE_SHELL") self.skipWaiting();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || !url.pathname.startsWith("/ui/")) return;
  // 缓存优先，后台刷新（离线可打开 UI；数据仍需在线）
  e.respondWith(
    caches.open(SHELL).then(async (cache) => {
      const hit = await cache.match(e.request);
      const fetchP = fetch(e.request, { cache: "no-cache" }).then(async (resp) => {
        if (resp.ok) await cache.put(e.request, resp.clone());
        return resp;
      }).catch(() => hit);
      // Keep the worker alive until a stale response has finished refreshing.
      // Navigation may otherwise terminate the worker before cache.put completes.
      e.waitUntil(fetchP.then(() => undefined));
      return hit || fetchP;
    })
  );
});
