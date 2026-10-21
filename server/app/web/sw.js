// Service worker de la web app : uniquement les notifications. Aucune mise en cache : la page vient toujours du serveur.
"use strict";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (ev) => ev.waitUntil(self.clients.claim()));

self.addEventListener("push", (ev) => {
  let d = {};
  try { d = ev.data ? ev.data.json() : {}; } catch { d = { body: ev.data ? ev.data.text() : "" }; }
  ev.waitUntil(self.registration.showNotification(d.title || "Poêle", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag, icon: "/icon.svg", data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (ev) => {
  ev.notification.close();
  const url = new URL(ev.notification.data?.url || "/", self.location.origin).href;
  ev.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    const open = wins.find((w) => w.url.startsWith(self.location.origin));
    return open ? open.focus() : self.clients.openWindow(url);
  }));
});
