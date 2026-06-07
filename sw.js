/* APEX indices — service worker v3 : réseau d'abord pour la page + mise à jour auto */
const CACHE = 'apex-idx-v3';
const SHELL = ['./', './index.html', './manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  // on N'appelle PAS skipWaiting ici : c'est la page qui décidera (update piloté)
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// La page peut demander l'activation immédiate du nouveau SW
self.addEventListener('message', e => {
  if (e.data === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return; // jamais les API externes

  // Page / HTML : réseau d'abord → toujours la dernière version ; cache en repli (offline)
  if (e.request.mode === 'navigate' || e.request.destination === 'document' || url.pathname.endsWith('.html')) {
    e.respondWith(
      fetch(e.request).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      }).catch(() => caches.match(e.request).then(h => h || caches.match('./index.html')))
    );
    return;
  }
  // Statique : cache d'abord
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
