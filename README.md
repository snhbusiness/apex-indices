# APEX · indices

Terminal institutionnel pour indices boursiers. Cœur : **morning brief** (2 couches) et **moteur de catalyseurs** (news → transmission → liquidité → scénarios). Front statique (GitHub Pages) + mini-proxy de données (FastAPI).

## Architecture

```
[ Front statique ]  --(1 appel)-->  [ Proxy FastAPI ]  -->  FRED · Twelve Data · FMP
  GitHub Pages                        Railway/Render          (clés côté serveur)
  - brief (IA, clé Anthropic navigateur)
  - catalyseurs (IA + web_search)
```

- **Couche factuelle** : le proxy monte un `market_state` réel (taux FRED, quotes Twelve Data, calendrier FMP), met en cache, calcule le régime. Aucune donnée factice : un champ indisponible = `null`.
- **Couche narrative** : le front envoie le `market_state` à Claude, qui n'écrit que la narration et ne cite que les chiffres reçus.

## Fichiers

| Fichier | Rôle |
|---|---|
| `index.html` | Terminal complet (shell, vues, IA) |
| `manifest.webmanifest`, `sw.js`, `icon-*.png` | PWA |
| `proxy/` | Le proxy FastAPI (voir `proxy/README.md`) |

## Déploiement

1. **Proxy d'abord** : déploie `proxy/` (voir son README), récupère son URL publique.
2. **Front** : pousse `index.html` + PWA sur un repo, active GitHub Pages.
3. Ouvre le site → **Paramètres** :
   - *URL du proxy* (ex. `https://apex-proxy.up.railway.app`) — bouton « Tester la connexion ».
   - *Clé Anthropic* (pour brief & catalyseurs) — reste dans ton navigateur.
   Les clés FRED/Twelve Data/FMP se mettent dans le `.env` du **proxy**, jamais dans le front.

## Modules

Vue d'ensemble (overnight + régime + taux) · Morning brief · Catalyseurs (IA) · Calendrier · **Breadth** (RSP/SPY, small caps, secteurs en hausse) · **Secteurs** (heatmap rotation + risk-on/off) · Journal · Paramètres.

## Limites assumées

- **Futures temps réel** : pas de source gratuite → champ vide tant qu'un flux payant n'est pas branché dans le proxy (`overnight.futures`).
- **Indices EU/Asie en temps réel** : payant chez Twelve Data ; sinon `null`.
- Le proxy résout le CORS (FRED), cache les clés, et met en cache (Redis ou mémoire).
