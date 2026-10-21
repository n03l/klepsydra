// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 the Klepsydra authors

// Service worker: makes Klepsydra installable and usable offline. Network first, so an online
// phone always gets the latest deploy; the cache is only the fallback when the network fails.
'use strict';

const CACHE = 'klepsydra-v4';
const SHELL = [
  './', 'index.html', 'app.js', 'style.css', 'manifest.json', 'timers.json',
  'icons/favicon.png', 'icons/apple-touch-icon.png', 'icons/icon-192.png', 'icons/icon-512.png',
];
const OPTIONAL = ['version.txt'];  // written by build.sh; absent when run from the sources
const NETWORK_TIMEOUT_MS = 4000;  // bad gym Wi-Fi: use the cached copy rather than hang

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.all([
        cache.addAll(SHELL),
        ...OPTIONAL.map((url) => cache.add(url).catch(() => {})),
      ]))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;

  const network = fetch(req).then((res) => {
    if (res.ok && res.type === 'basic') {
      const copy = res.clone();
      event.waitUntil(caches.open(CACHE).then((cache) => cache.put(req, copy)));
    }
    return res;
  });
  network.catch(() => {});  // a late failure after the timeout is expected, not an error

  event.respondWith((async () => {
    try {
      const timeout = new Promise((resolve) => setTimeout(resolve, NETWORK_TIMEOUT_MS));
      const res = await Promise.race([network, timeout]);
      if (res) return res;
    } catch (e) {
      // offline: fall through to the cache
    }
    const cached = await caches.match(req, { ignoreSearch: true })
      || (req.mode === 'navigate' ? await caches.match('./') : undefined);
    return cached || network;  // nothing cached: wait for the network after all (or its error)
  })());
});
