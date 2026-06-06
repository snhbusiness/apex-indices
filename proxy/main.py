"""
APEX · indices — proxy de données (FastAPI)
Quotes via FMP STABLE API (/stable/quote) : indices, ETF, secteurs, futures, VIX.
Taux via FRED. Calendrier via FMP /stable. Cache Redis ou mémoire.
Jamais de données factices : un champ indisponible vaut null.
"""
import os, json, time, asyncio
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

FMP  = os.getenv("FMP_KEY", "")
FRED = os.getenv("FRED_KEY", "")
ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
REDIS_URL = os.getenv("REDIS_URL", "")
TTL_QUOTE = int(os.getenv("TTL_QUOTE", "90"))
TTL_RATE  = int(os.getenv("TTL_RATE",  "3600"))
TTL_CAL   = int(os.getenv("TTL_CAL",   "21600"))
BASE = "https://financialmodelingprep.com/stable"

_r = None
if REDIS_URL:
    try:
        import redis.asyncio as aioredis
        _r = aioredis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        _r = None
_mem = {}
async def cget(k):
    if _r:
        v = await _r.get(k); return json.loads(v) if v else None
    e = _mem.get(k); return e[1] if e and e[0] > time.time() else None
async def cset(k, val, ttl):
    if _r: await _r.set(k, json.dumps(val), ex=ttl)
    else: _mem[k] = (time.time() + ttl, val)

# ---------- FMP /stable/quote (un appel par symbole, concurrent) ----------
async def fmp_quote_one(sym, client):
    c = await cget("q:" + sym)
    if c is not None:
        return c
    try:
        r = await client.get(f"{BASE}/quote", params={"symbol": sym, "apikey": FMP}, timeout=12)
        d = r.json()
        q = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) and d.get("symbol") else None)
        if not q:
            return None
        cp = q.get("changePercentage", q.get("changesPercentage"))
        if cp is None:
            return None
        val = {"chg": round(float(cp), 2),
               "price": float(q["price"]) if q.get("price") is not None else None}
        await cset("q:" + sym, val, TTL_QUOTE)
        return val
    except Exception:
        return None

async def fmp_quotes(symbols, client):
    sem = asyncio.Semaphore(10)
    async def one(s):
        async with sem:
            return s, await fmp_quote_one(s, client)
    res = await asyncio.gather(*[one(s) for s in symbols])
    return {s: v for s, v in res}

async def fmp_first(cands, client):
    """Renvoie la 1re quote non nulle parmi des symboles candidats (auto-découverte)."""
    for sym in cands:
        v = await fmp_quote_one(sym, client)
        if v is not None:
            return v
    return None

async def fmp_get(path, params, client):
    """GET générique sur un endpoint /stable, renvoie une liste ou None."""
    try:
        r = await client.get(f"{BASE}/{path}", params={**params, "apikey": FMP}, timeout=15)
        j = r.json()
        return j if isinstance(j, list) and j else None
    except Exception:
        return None

async def fmp_get_first(paths, params, client):
    for p in paths:
        j = await fmp_get(p, params, client)
        if j: return j
    return None

async def fred(series, client):
    c = await cget("fred:" + series)
    if c is not None: return c
    try:
        r = await client.get("https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": series, "api_key": FRED, "file_type": "json",
                                     "sort_order": "desc", "limit": 1}, timeout=15)
        val = float(r.json()["observations"][0]["value"])
        await cset("fred:" + series, val, TTL_RATE); return val
    except Exception:
        return None

async def fmp_calendar(client):
    c = await cget("cal:today")
    if c is not None: return c
    today = time.strftime("%Y-%m-%d")
    res = {"macro": [], "earnings": []}
    try:
        r = await client.get(f"{BASE}/economic-calendar",
                             params={"from": today, "to": today, "apikey": FMP}, timeout=15)
        for e in (r.json() or []):
            if e.get("impact") in ("High", "Medium"):
                res["macro"].append({"time": (e.get("date", "")[11:16]), "event": e.get("event"),
                                     "impact": (e.get("impact") or "").lower()})
    except Exception: pass
    try:
        r = await client.get(f"{BASE}/earnings-calendar",
                             params={"from": today, "to": today, "apikey": FMP}, timeout=15)
        res["earnings"] = [e.get("symbol") for e in (r.json() or [])][:12]
    except Exception: pass
    await cset("cal:today", res, TTL_CAL); return res

def score_regime(reg, fut_avg):
    sc = 0; vix = reg.get("vix")
    if vix is not None:
        sc += 2 if vix < 16 else 0 if vix < 20 else -1 if vix < 26 else -2
    if reg.get("hy_oas") is not None:
        sc += 1 if reg["hy_oas"] < 3.5 else -1 if reg["hy_oas"] > 4.5 else 0
    if (reg.get("rsp_spy") or "").find("négative") >= 0: sc -= 1
    if fut_avg is not None:
        sc += 1 if fut_avg > 0.4 else -1 if fut_avg < -0.4 else 0
    return max(-3, min(3, sc))

app = FastAPI(title="APEX indices proxy")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS or ["*"],
                   allow_methods=["GET"], allow_headers=["*"])

@app.get("/api/health")
async def health():
    return {"ok": True, "redis": bool(_r), "keys": {"fmp": bool(FMP), "fred": bool(FRED)}}

@app.get("/api/debug")
async def debug(symbol: str = "AAPL"):
    """Diagnostic : réponse brute FMP pour un symbole. Ex: /api/debug?symbol=DXY"""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{BASE}/quote", params={"symbol": symbol, "apikey": FMP}, timeout=12)
            return {"symbol": symbol, "status": r.status_code, "body": r.json()}
        except Exception as e:
            return {"error": str(e)}

@app.get("/api/market_state")
async def market_state():
    fr = {}
    async with httpx.AsyncClient() as client:
        qs = await fmp_quotes(["^GSPC","^IXIC","^DJI","^N225","^HSI",
                               "ESUSD","^GDAXI","^FCHI",
                               "^VIX","RSP","SPY","CLUSD"], client)
        t10, r10, t2, hy = await asyncio.gather(
            fred("DGS10", client), fred("DFII10", client),
            fred("DGS2", client), fred("BAMLH0A0HYM2", client))
        nqv  = await fmp_first(["NQUSD","NQ","NDXUSD"], client)
        ymv  = await fmp_first(["YMUSD","YM","DJIUSD"], client)
        dxyv = await fmp_first(["DXY","^DXY","USDX","DXUSD","DX"], client)
        cal = await fmp_calendar(client)
    def chg(s): return qs[s]["chg"] if qs.get(s) else None
    def px(s):  return qs[s]["price"] if qs.get(s) else None
    us   = [{"sym":"S&P 500","chg":chg("^GSPC")},{"sym":"Nasdaq 100","chg":chg("^IXIC")},{"sym":"Dow","chg":chg("^DJI")}]
    asia = [a for a in [{"sym":"Nikkei","chg":chg("^N225")},{"sym":"Hang Seng","chg":chg("^HSI")}] if a["chg"] is not None]
    fut  = [f for f in [{"sym":"ES (S&P)","chg":chg("ESUSD")},
                        {"sym":"NQ (Nasdaq)","chg":(nqv["chg"] if nqv else None)},
                        {"sym":"YM (Dow)","chg":(ymv["chg"] if ymv else None)},
                        {"sym":"DAX (cash)","chg":chg("^GDAXI")},
                        {"sym":"CAC (cash)","chg":chg("^FCHI")}] if f["chg"] is not None]
    fr["indices"]="live" if any(x["chg"] is not None for x in us) else "indisponible"
    fr["futures"]="live" if fut else "indisponible"
    fr["asia"]="live" if asia else "indisponible"
    fr["rates"]="live" if t10 is not None else "indisponible (clé FRED ?)"
    rsp, spy = chg("RSP"), chg("SPY")
    rsp_spy = ("divergence négative (étroit)" if (rsp is not None and spy is not None and rsp < spy)
               else "breadth saine" if (rsp is not None and spy is not None) else None)
    regime = {"vix": round(px("^VIX"),1) if px("^VIX") else None, "vix_term": None,
              "hy_oas": round(hy,2) if hy else None, "rsp_spy": rsp_spy}
    fa = (sum(f["chg"] for f in fut)/len(fut)) if fut else None
    regime["score"] = score_regime(regime, fa)
    regime["risk"]  = "risk-on" if regime["score"]>=1 else "risk-off" if regime["score"]<=-1 else "neutre"
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "demo": False, "freshness": fr,
        "regime": regime,
        "overnight": {"us_close": us, "asia": asia, "futures": fut},
        "futures_note": "Futures ES/NQ/YM via FMP. DAX/CAC en cash (futures Eurex non couverts).",
        "backdrop": {"us10y": t10, "real_10y": r10,
                     "curve_2s10s": round(t10-t2,2) if (t10 is not None and t2 is not None) else None,
                     "dxy": (round(dxyv["price"],2) if (dxyv and dxyv.get("price")) else None), "oil": px("CLUSD")},
        "calendar_today": cal["macro"], "earnings_today": cal["earnings"], "news_top": [],
    }

@app.get("/api/sector_breadth")
async def sector_breadth():
    SECT = [("XLK","Technologie","off"),("XLC","Communication","off"),("XLY","Conso. discrétionnaire","off"),
            ("XLF","Finance","cyc"),("XLI","Industrie","cyc"),("XLB","Matériaux","cyc"),("XLE","Énergie","cyc"),
            ("XLV","Santé","def"),("XLP","Conso. de base","def"),("XLU","Services publics","def"),("XLRE","Immobilier","def")]
    async with httpx.AsyncClient() as client:
        qs = await fmp_quotes([s[0] for s in SECT] + ["SPY","RSP","IWM"], client)
    def chg(s): return qs[s]["chg"] if qs.get(s) else None
    sectors = [{"sym":s,"name":n,"grp":g,"chg":chg(s)} for s,n,g in SECT if chg(s) is not None]
    return {"demo": False, "freshness": "live" if sectors else "indisponible",
            "sectors": sectors, "spy": chg("SPY"), "rsp": chg("RSP"), "iwm": chg("IWM")}


@app.get("/api/intel")
async def intel():
    """Feed catalyseurs FMP : news, upgrades/downgrades, surprises de résultats, M&A,
    + secteur/pairs des sociétés concernées. Tout défensif : ce qui échoue est omis."""
    c = await cget("intel:today")
    if c is not None:
        return c
    out = {"news": [], "grades": [], "earnings": [], "mna": [], "entities": {}}
    async with httpx.AsyncClient() as client:
        news = await fmp_get_first(["news/general-latest", "news/stock-latest"], {"limit": 25, "page": 0}, client)
        for n in (news or [])[:18]:
            out["news"].append({"title": n.get("title"), "site": n.get("site") or n.get("publisher"),
                                "date": n.get("publishedDate") or n.get("date"), "symbol": n.get("symbol")})
        grades = await fmp_get_first(["grades-latest-news", "grades-news"], {"limit": 25, "page": 0}, client)
        for g in (grades or [])[:18]:
            out["grades"].append({"symbol": g.get("symbol"), "action": g.get("action") or g.get("newsType"),
                                  "from": g.get("previousGrade"), "to": g.get("newGrade"),
                                  "by": g.get("gradingCompany") or g.get("analystCompany"),
                                  "date": g.get("publishedDate") or g.get("date")})
        mna = await fmp_get_first(["mergers-acquisitions-latest"], {"page": 0}, client)
        for m in (mna or [])[:8]:
            out["mna"].append({"acquirer": m.get("companyName") or m.get("symbol"),
                               "target": m.get("targetedCompanyName") or m.get("targetedSymbol"),
                               "date": m.get("transactionDate") or m.get("acceptedDate")})
        today = time.strftime("%Y-%m-%d")
        ec = await fmp_get("earnings-calendar", {"from": today, "to": today}, client)
        for e in (ec or [])[:20]:
            est, act = e.get("epsEstimated"), e.get("epsActual")
            surprise = None
            try:
                if est not in (None, 0) and act is not None:
                    surprise = round((act - est) / abs(est) * 100, 1)
            except Exception:
                surprise = None
            out["earnings"].append({"symbol": e.get("symbol"), "epsEstimated": est,
                                    "epsActual": act, "surprisePct": surprise})
        # entités : secteur + pairs pour les tickers cités (grades + earnings), max 8
        seen = []
        for x in out["grades"] + out["earnings"]:
            t = x.get("symbol")
            if t and t not in seen:
                seen.append(t)
        seen = seen[:8]
        async def ent(t):
            prof = await fmp_get("profile", {"symbol": t}, client)
            peers = await fmp_get("stock-peers", {"symbol": t}, client)
            sector = prof[0].get("sector") if prof else None
            plist = []
            if peers:
                p0 = peers[0]
                plist = (p0.get("peersList") if isinstance(p0, dict) else None) or \
                        ([p for p in peers if isinstance(p, str)])
            return t, {"sector": sector, "peers": (plist or [])[:6]}
        if seen:
            for t, v in await asyncio.gather(*[ent(t) for t in seen]):
                out["entities"][t] = v
    await cset("intel:today", out, 1800)
    return out
