/* AgentOS 工作台 Service Worker：v60 起停用离线外壳缓存。
 * 新工作台（/ui/）使用带哈希的静态资源和 no-cache 入口页，不再需要 SW。
 * 本脚本只做一次性清理：删除旧外壳缓存、注销自身，并让停在 /ui/ 的旧页面重新载入新界面。
 * 旧版界面（/ui/legacy/）不会被强制刷新，也不会再注册 SW。 */
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k.startsWith("agentos-shell-")).map((k) => caches.delete(k)));
    await self.registration.unregister();
    const windows = await self.clients.matchAll({ type: "window" });
    for (const client of windows) {
      const path = new URL(client.url).pathname;
      if (path === "/ui/" || path === "/ui/index.html") client.navigate(client.url);
    }
  })());
});
