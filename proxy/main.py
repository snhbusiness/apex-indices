"""
APEX · indices — proxy de données (FastAPI)
-------------------------------------------
Fetch côté serveur (plus de CORS, clés cachées), cache (Redis ou mémoire),
sert deux endpoints JSON propres au terminal :
  GET /api/market_state    -> overnight, futures, backdrop taux, régime
  GET /api/sector_breadth  -> rotation sectorielle + internals

Sources : FRED (taux, gratuit, illimité) · Twelve Data (indices/ETF/actions US,
temps réel gratuit) · FMP (news/earnings).
Jamais de données factices : un champ indisponible vaut null + un flag dans
`freshness`/`errors`.

Lancement local :  uvicorn main:app --reload
Variables d'env  :  voir .env.example
"""
import os, json, time, asyncio
from typing import Optional
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

TWELVE = os.getenv("TWELVEDATA_KEY", "")
FRED   = os.getenv("FRED_KEY", "")
FMP    = os.getenv("FMP_KEY", "")
ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
REDIS_URL = os.getenv("REDIS_URL", "")
TTL_QUOTE = int(os.getenv("TTL_QUOTE", "120"))   # 2 min
TTL_RATE  = int(os.getenv("TTL_RATE",  "3600"))  # 1 h
TTL_CAL   = int(os.getenv("TTL_CAL",   "21600")) # 6 h

# ---------- cache : Redis si dispo, sinon mémoire ----------
_r = None
if REDIS_URL:
    try:
        import redis.asyncio as aioredis
        _r = aioredis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _r = None
_mem: dict = {}

async def cget(k):
    if _r:
        v = await _r.get(k)
        return json.loads(v) if v else None
    e = _mem.get(k)
    return e[1] if e and e[0] > time.time() else None

async def cset(k, val, ttl):
    if _r:
        await _r.set(k, json.dumps(val), ex=ttl)
    else:
        _mem[k] = (time.time() + ttl, val)

# ---------- fetchers ----------
async def td_quotes(symbols, client):
    """Twelve Data — quotes multi-symboles, par paquets de 8 (limite débit gratuit)."""
    out, todo = {}, []
    for s in symbols:
        c = await cget("q:" + s)
        if c is not None:
            out[s] = c
        else:
            todo.append(s)
    for i in range(0, len(todo), 8):
        chunk = todo[i:i + 8]
        try:
            r = await client.get("https://api.twelvedata.com/quote",
                                  params={"symbol": ",".join(chunk), "apikey": TWELVE}, timeout=15)
            d = r.json()
            items = d if (len(chunk) > 1) else {chunk[0]: d}
            for sym in chunk:
                q = items.get(sym) if isinstance(items, dict) else None
                if q and q.get("status") != "error" and q.get("percent_change") is not None:
                    val = {"chg": round(float(q["percent_change"]), 2),
                           "price": float(q.get("close")) if q.get("close") else None}
                    out[sym] = val
                    await cset("q:" + sym, val, TTL_QUOTE)
                else:
                    out[sym] = None
        except Exception:
            for sym in chunk:
                out[sym] = None
        if i + 8 < len(todo):
            await asyncio.sleep(8)  # respecte 8 crédits / minute
    return out

async def fred(series, client):
    c = await cget("fred:" + series)
    if c is not None:
        return c
    try:
        r = await client.get("https://api.stlouisfed.org/fred/series/observations",
                              params={"series_id": series, "api_key": FRED, "file_type": "json",
                                      "sort_order": "desc", "limit": 1}, timeout=15)
        val = float(r.json()["observations"][0]["value"])
        await cset("fred:" + series, val, TTL_RATE)
        return val
    except Exception:
        return None

async def fmp_calendar(client):
    c = await cget("cal:today")
    if c is not None:
        return c
    today = time.strftime("%Y-%m-%d")
    res = {"macro": [], "earnings": []}
    try:
        r = await client.get("https://financialmodelingprep.com/api/v3/economic_calendar",
                              params={"from": today, "to": today, "apikey": FMP}, timeout=15)
        for e in (r.json() or []):
            if e.get("impact") in ("High", "Medium"):
                res["macro"].append({"time": (e.get("date", "")[11:16]), "event": e.get("event"),
                                     "impact": (e.get("impact") or "").lower()})
    except Exception:
        pass
    try:
        r = await client.get("https://financialmodelingprep.com/api/v3/earning_calendar",
                              params={"from": today, "to": today, "apikey": FMP}, timeout=15)
        res["earnings"] = [e.get("symbol") for e in (r.json() or [])][:12]
    except Exception:
        pass
    await cset("cal:today", res, TTL_CAL)
    return res

# ---------- régime (déterministe, source unique de vérité) ----------
def score_regime(reg, fut_avg):
    sc = 0
    vix = reg.get("vix")
    if vix is not None:
        sc += 2 if vix < 16 else 0 if vix < 20 else -1 if vix < 26 else -2
    if reg.get("hy_oas") is not None:
        sc += 1 if reg["hy_oas"] < 3.5 else -1 if reg["hy_oas"] > 4.5 else 0
    if (reg.get("rsp_spy") or "").find("négative") >= 0:
        sc -= 1
    if fut_avg is not None:
        sc += 1 if fut_avg > 0.4 else -1 if fut_avg < -0.4 else 0
    return max(-3, min(3, sc))

# ---------- app ----------
app = FastAPI(title="APEX indices proxy")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS or ["*"],
                   allow_methods=["GET"], allow_headers=["*"])

@app.get("/api/health")
async def health():
    return {"ok": True, "redis": bool(_r),
            "keys": {"twelvedata": bool(TWELVE), "fred": bool(FRED), "fmp": bool(FMP)}}

@app.get("/api/market_state")
async def market_state():
    fr = {}
    async with httpx.AsyncClient() as client:
        # proxies ETF US (temps réel gratuit Twelve Data) + indices + breadth + VIX
        qs = await td_quotes(["SPY", "QQQ", "DIA", "RSP", "VIX", "N225", "HSI", "DXY"], client)
        t10, r10, t2, hy = await asyncio.gather(
            fred("DGS10", client), fred("DFII10", client),
            fred("DGS2", client), fred("BAMLH0A0HYM2", client))
        cal = await fmp_calendar(client)

    def chg(s): return qs.get(s, {}).get("chg") if qs.get(s) else None
    us = [{"sym": "S&P 500", "chg": chg("SPY")}, {"sym": "Nasdaq 100", "chg": chg("QQQ")},
          {"sym": "Dow", "chg": chg("DIA")}]
    asia = [{"sym": "Nikkei", "chg": chg("N225")}, {"sym": "Hang Seng", "chg": chg("HSI")}]
    asia = [a for a in asia if a["chg"] is not None]
    fr["indices"] = "live" if any(x["chg"] is not None for x in us) else "indisponible"
    fr["asia"] = "live" if asia else "indisponible (symboles EU/Asie = payant)"
    fr["rates"] = "live" if t10 is not None else "indisponible (clé FRED ?)"

    rsp, spy = chg("RSP"), chg("SPY")
    rsp_spy = ("divergence négative (étroit)" if (rsp is not None and spy is not None and rsp < spy)
               else "breadth saine" if rsp is not None and spy is not None else None)
    regime = {"risk": None, "vix": chg("VIX") and None, "vix_term": None,
              "hy_oas": round(hy, 2) if hy else None, "rsp_spy": rsp_spy}
    # VIX : on veut le niveau, pas le %
    regime["vix"] = round(qs["VIX"]["price"], 1) if qs.get("VIX") and qs["VIX"].get("price") else None
    regime["score"] = score_regime(regime, None)
    regime["risk"] = "risk-on" if regime["score"] >= 1 else "risk-off" if regime["score"] <= -1 else "neutre"

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "demo": False, "freshness": fr,
        "overnight": {
            "us_close": us, "asia": asia,
            "futures": [],  # futures temps réel = flux payant ; brancher ici un provider dédié
        },
        "futures_note": "Futures indices temps réel non disponibles en gratuit. Brancher un flux payant (CME/Databento) dans td/futures.",
        "backdrop": {"us10y": t10, "real_10y": r10,
                     "curve_2s10s": round(t10 - t2, 2) if (t10 is not None and t2 is not None) else None,
                     "dxy": qs.get("DXY", {}).get("price") if qs.get("DXY") else None, "oil": None},
        "calendar_today": cal["macro"],
        "earnings_today": cal["earnings"],
        "news_top": [],
    }

@app.get("/api/sector_breadth")
async def sector_breadth():
    SECT = [("XLK", "Technologie", "off"), ("XLC", "Communication", "off"),
            ("XLY", "Conso. discrétionnaire", "off"), ("XLF", "Finance", "cyc"),
            ("XLI", "Industrie", "cyc"), ("XLB", "Matériaux", "cyc"),
            ("XLE", "Énergie", "cyc"), ("XLV", "Santé", "def"),
            ("XLP", "Conso. de base", "def"), ("XLU", "Services publics", "def"),
            ("XLRE", "Immobilier", "def")]
    async with httpx.AsyncClient() as client:
        qs = await td_quotes([s[0] for s in SECT] + ["SPY", "RSP", "IWM"], client)
    def chg(s): return qs.get(s, {}).get("chg") if qs.get(s) else None
    sectors = [{"sym": s, "name": n, "grp": g, "chg": chg(s)} for s, n, g in SECT if chg(s) is not None]
    return {"demo": False,
            "freshness": "live" if sectors else "indisponible",
            "sectors": sectors,
            "spy": chg("SPY"), "rsp": chg("RSP"), "iwm": chg("IWM")}
