# APEX · indices

Terminal institutionnel pour indices boursiers — version single-HTML, hébergeable sur GitHub Pages, sur le modèle d'APEX FX. Le cœur du produit : le **morning brief** et le **moteur de catalyseurs**.

## Principe d'architecture

Deux couches, strictement séparées (c'est ce qui tue les hallucinations) :

1. **Couche factuelle — `market_state`** (déterministe, en JS). Monte les chiffres en cascade de sources, calcule les deltas et le score de régime par des règles. L'IA n'y touche jamais.
2. **Couche narrative — IA**. Reçoit le `market_state` en entrée, n'écrit que la narration et ne peut citer **que** les chiffres présents dans l'entrée.

Si pas de clé IA → un **template de secours** remplit le brief depuis le `market_state`. Si pas de clé data → **mode démo**. Le terminal s'affiche toujours.

## Les fichiers

| Fichier | Rôle |
|---|---|
| `index.html` | Toute l'app : shell, vues, data layer, prompts, appels Claude |
| `manifest.webmanifest` | PWA (installable écran d'accueil iPhone) |
| `sw.js` | Service worker — cache du shell, jamais les API |
| `icon-192.png` / `icon-512.png` | Icônes PWA |

## Structure interne (`index.html`)

- `CFG` — clés & modèle (localStorage)
- `STRUCTURE` — base de référence injectée à l'IA : poids indices, graphe pairs (fournisseur/concurrent/secteur), playbook liquidité. **C'est la brique qualité du moteur catalyseurs — à enrichir.**
- `DEMO_STATE` / `DEMO_CATALYSTS` — fallback
- Data layer : `fredSeries`, `finnhubQuote`, `buildMarketState` (cascade par catégorie), `scoreRegime` (règles déterministes)
- `callClaude` — appel API navigateur (header `anthropic-dangerous-direct-browser-access`), avec `web_search` activable
- `BRIEF_SYSTEM` / `CATALYST_SYSTEM` — les system prompts
- Vues : `overview`, `brief`, `catalysts`, `calendar`, `breadth`(v2), `sectors`(v2), `journal`, `settings`

## Sources de données (cascade)

- **Taux & macro** : FRED (source de vérité — `DGS10`, `DFII10`, `DGS2`)
- **Indices / VIX / breadth** : Finnhub (proxies ETF `SPY`/`QQQ`/`DIA` en pré-market ; breadth via ratio `RSP`/`SPY`)
- **News & earnings** : FMP + `web_search` de Claude pour le live (OPA, contrats, pivots…)
- **Calendrier macro** : widget Investing (visuel) — *pas d'API publique Investing*, donc FMP pour les données

## Déploiement (GitHub Pages)

1. Pousse les 5 fichiers dans un repo (ex. `apex-indices`).
2. Settings → Pages → branche `main`, dossier `/root`.
3. Ouvre `https://<user>.github.io/apex-indices/`.
4. Sur iPhone : Safari → Partager → « Sur l'écran d'accueil » (les push PWA iOS exigent l'app installée).
5. Renseigne tes clés dans **Paramètres**. Sans clés, tout tourne en démo.

## Roadmap (v2)

Modules déjà prévus dans la nav : breadth dédié, rotation sectorielle (heatmap 11 secteurs = l'équivalent actions de la « force des devises »), COT futures indices, saisonnalité, déclenchement auto du brief aux horaires de session.

> Le moteur produit des **lectures de desk et des scénarios**, pas des recommandations d'investissement.
