# APEX · indices — proxy de données

Mini-backend FastAPI qui sert des **vraies données** au terminal : fetch côté serveur (plus de CORS), clés cachées, cache (Redis ou mémoire). Stack volontairement proche de FastAPI/Celery/Redis.

## Endpoints

| Endpoint | Contenu |
|---|---|
| `GET /api/market_state` | overnight US (proxies ETF), Asie, backdrop taux (FRED), régime (VIX, HY, breadth, score), calendrier + earnings (FMP) |
| `GET /api/sector_breadth` | 11 secteurs (SPDR) + SPY/RSP/IWM pour la rotation et les internals |
| `GET /api/health` | état + quelles clés serveur sont présentes |

Aucune donnée factice : un champ non disponible vaut `null` + un flag dans `freshness`.

## Sources & clés (toutes côté serveur)

- **FRED** — taux & spread crédit (gratuit, illimité, sans CORS côté serveur). Séries : `DGS10`, `DFII10`, `DGS2`, `BAMLH0A0HYM2`.
- **Twelve Data** — quotes ETF/actions US temps réel (gratuit, 8 crédits/min → fetch par paquets de 8 + cache).
- **FMP** — calendrier macro + earnings.

## Lancer en local

```bash
cd proxy
pip install -r requirements.txt
cp .env.example .env        # remplis tes clés
uvicorn main:app --reload
# http://127.0.0.1:8000/api/health
```

## Déployer (Railway / Render)

1. Nouveau service depuis le repo (ou `proxy/` en root directory).
2. Variables d'environnement (onglet Variables) :
   - `TWELVEDATA_KEY`, `FRED_KEY`, `FMP_KEY`
   - `CORS_ORIGINS=https://<user>.github.io`  (l'origine de ton front)
   - `REDIS_URL` (optionnel — sinon cache mémoire)
3. Start command : `uvicorn main:app --host 0.0.0.0 --port $PORT`
   (ou laisse le `Dockerfile` faire le travail).
4. Copie l'URL publique → colle-la dans le front (Paramètres → URL du proxy).

## Cache

TTL configurables : `TTL_QUOTE` (120s), `TTL_RATE` (1h), `TTL_CAL` (6h). Avec Redis, le cache survit aux redémarrages et se partage entre instances.

## Étendre

- **Futures temps réel** : brancher un provider (CME/Databento/Polygon payant) et remplir `overnight.futures` dans `market_state`.
- **News structurées** : ajouter un endpoint `/api/news` (FMP/Finnhub) pour pré-alimenter le moteur catalyseurs au lieu de tout laisser à `web_search`.
- **VIX term structure** : ajouter VIX3M (CBOE) pour le flag contango/backwardation.
