"""
APEX · indices — proxy de données (FastAPI)
Quotes via FMP STABLE API (/stable/quote) : indices, ETF, secteurs, futures, VIX.
Taux via FRED. Calendrier via FMP /stable. Cache Redis ou mémoire.
Jamais de données factices : un champ indisponible vaut null.
"""
import os, json, time, asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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
               "price": float(q["price"]) if q.get("price") is not None else None,
               "high": q.get("dayHigh"), "low": q.get("dayLow"), "prev": q.get("previousClose")}
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

async def fmp_first_valid(cands, client, ok):
    """Comme fmp_first, mais ne garde que la 1re quote qui passe le test ok(v)
    (filtre les prix aberrants : mauvais symbole renvoyant 0.001, etc.)."""
    for sym in cands:
        v = await fmp_quote_one(sym, client)
        if v is not None and ok(v):
            return v
    return None

# ---------- Yahoo Finance (futures live que FMP n'a pas : NQ, YM, Russell...) ----------
YH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ApexProxy/1.0)"}
async def yahoo_quote(sym, client):
    c = await cget("yq:" + sym)
    if c is not None:
        return c
    try:
        r = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                             params={"range": "1d", "interval": "1d"},
                             headers=YH_HEADERS, timeout=12)
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None or not prev:
            return None
        val = {"chg": round((price/prev - 1)*100, 2), "price": float(price),
               "high": meta.get("regularMarketDayHigh"), "low": meta.get("regularMarketDayLow"), "prev": prev}
        await cset("yq:" + sym, val, TTL_QUOTE)
        return val
    except Exception:
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
                               "^GDAXI","^FCHI","RSP","SPY","CLUSD"], client)
        t10, r10, t2, hy = await asyncio.gather(
            fred("DGS10", client), fred("DFII10", client),
            fred("DGS2", client), fred("BAMLH0A0HYM2", client))
        y_es  = await yahoo_quote("ES=F", client)
        y_nq  = await yahoo_quote("NQ=F", client)
        y_ym  = await yahoo_quote("YM=F", client)
        y_rty = await yahoo_quote("RTY=F", client)
        y_vix = await yahoo_quote("^VIX", client)
        dxyv = await fmp_first_valid(["DX-Y.NYB","USDX","^DXY","DXY","DXUSD","DX=F"], client, lambda v: v.get("price") and 70 < v["price"] < 130)
        cal = await fmp_calendar(client)
    def chg(s): return qs[s]["chg"] if qs.get(s) else None
    def px(s):  return qs[s]["price"] if qs.get(s) else None
    def hi(s):  return qs[s].get("high") if qs.get(s) else None
    def lo(s):  return qs[s].get("low") if qs.get(s) else None
    def pv(s):  return qs[s].get("prev") if qs.get(s) else None
    us   = [{"sym":"S&P 500","chg":chg("^GSPC"),"price":px("^GSPC"),"high":hi("^GSPC"),"low":lo("^GSPC"),"prev":pv("^GSPC"),"inv":"/indices/us-spx-500"},
            {"sym":"Nasdaq 100","chg":chg("^IXIC"),"price":px("^IXIC"),"high":hi("^IXIC"),"low":lo("^IXIC"),"prev":pv("^IXIC"),"inv":"/indices/nq-100"},
            {"sym":"Dow","chg":chg("^DJI"),"price":px("^DJI"),"high":hi("^DJI"),"low":lo("^DJI"),"prev":pv("^DJI"),"inv":"/indices/us-30"}]
    asia = [a for a in [{"sym":"Nikkei","chg":chg("^N225"),"price":px("^N225"),"high":hi("^N225"),"low":lo("^N225"),"prev":pv("^N225"),"inv":"/indices/japan-ni225"},
                        {"sym":"Hang Seng","chg":chg("^HSI"),"price":px("^HSI"),"high":hi("^HSI"),"low":lo("^HSI"),"prev":pv("^HSI"),"inv":"/indices/hang-sen-40"}] if a["chg"] is not None]
    yf=lambda y:{"chg":(y["chg"] if y else None),"price":(y["price"] if y else None),"high":(y.get("high") if y else None),"low":(y.get("low") if y else None),"prev":(y.get("prev") if y else None)}
    cac_imp=(y_es["chg"] if y_es else None)
    fut  = [f for f in [{"sym":"ES (S&P 500)","inv":"/indices/us-spx-500-futures",**yf(y_es)},
                        {"sym":"NQ (Nasdaq 100)","inv":"/indices/nq-100-futures",**yf(y_nq)},
                        {"sym":"YM (Dow)","inv":"/indices/us-30-futures",**yf(y_ym)},
                        {"sym":"RTY (Russell 2000)","inv":"/indices/smallcap-2000-futures",**yf(y_rty)},
                        {"sym":"DAX (cash)","chg":chg("^GDAXI"),"price":px("^GDAXI"),"high":hi("^GDAXI"),"low":lo("^GDAXI"),"prev":pv("^GDAXI"),"inv":"/indices/germany-30"},
                        {"sym":"CAC 40 (cash)","chg":chg("^FCHI"),"price":px("^FCHI"),"high":hi("^FCHI"),"low":lo("^FCHI"),"prev":pv("^FCHI"),"inv":"/indices/france-40"},
                        {"sym":"CAC 40 (ouv. implicite via ES)","chg":cac_imp,"price":None,"inv":"/indices/france-40"}] if f["chg"] is not None]
    fr["indices"]="live" if any(x["chg"] is not None for x in us) else "indisponible"
    fr["futures"]="live" if fut else "indisponible"
    fr["asia"]="live" if asia else "indisponible"
    fr["rates"]="live" if t10 is not None else "indisponible (clé FRED ?)"
    rsp, spy = chg("RSP"), chg("SPY")
    rsp_spy = ("divergence négative (étroit)" if (rsp is not None and spy is not None and rsp < spy)
               else "breadth saine" if (rsp is not None and spy is not None) else None)
    vix_val=(y_vix["price"] if y_vix else None)
    regime = {"vix": round(vix_val,1) if vix_val else None, "vix_term": None,
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

@app.get("/api/yahoo")
async def yahoo_debug(symbol: str = "ES=F"):
    """Test Yahoo : /api/yahoo?symbol=NQ=F"""
    async with httpx.AsyncClient() as client:
        v = await yahoo_quote(symbol, client)
        return {"symbol": symbol, "quote": v}


# ---------- Passe-plat FMP générique (pour l'app Actions) ----------
# La clé FMP est injectée ici, jamais exposée au navigateur. CORS déjà restreint à l'origine.
@app.get("/api/fmp/{fmp_path:path}")
async def fmp_passthrough(fmp_path: str, request: Request):
    params = {k: v for k, v in request.query_params.items() if k != "apikey"}
    params["apikey"] = FMP
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{BASE}/{fmp_path}", params=params, timeout=25)
            try:
                return JSONResponse(content=r.json(), status_code=r.status_code)
            except Exception:
                return JSONResponse(content={"error": "non-json", "status": r.status_code}, status_code=r.status_code)
        except Exception as e:
            return JSONResponse(content={"error": str(e)}, status_code=502)


# ---------- Yahoo "actions" : cotation + fondamentaux + bougies (pour le CAC, absent de FMP) ----------
_YAUTH = {"cookie": "", "crumb": "", "ts": 0.0}
async def _yahoo_auth(client):
    """Yahoo exige un cookie + crumb pour quoteSummary. Mis en cache 30 min."""
    if _YAUTH["crumb"] and (time.time() - _YAUTH["ts"] < 1800):
        return _YAUTH["cookie"], _YAUTH["crumb"]
    ck, crumb = "", ""
    try:
        r = await client.get("https://fc.yahoo.com/", headers=YH_HEADERS, timeout=12, follow_redirects=True)
        sc = r.headers.get("set-cookie", "")
        ck = sc.split(";")[0] if sc else ""
    except Exception:
        pass
    try:
        h = dict(YH_HEADERS)
        if ck:
            h["cookie"] = ck
        r2 = await client.get("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=h, timeout=12)
        crumb = (r2.text or "").strip()
    except Exception:
        pass
    _YAUTH.update(cookie=ck, crumb=crumb, ts=time.time())
    return ck, crumb

def _yraw(o, k):
    v = o.get(k)
    if isinstance(v, dict):
        return v.get("raw")
    return v

async def yahoo_stock(sym, client):
    """Renvoie un objet normalisé pour un titre : prix, fondamentaux, bougies (recent->ancien)."""
    c = await cget("ys:" + sym)
    if c is not None:
        return c
    out = {"symbol": sym}
    # 1) chart : prix + bougies (sans crumb)
    try:
        r = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                             params={"range": "6mo", "interval": "1d"}, headers=YH_HEADERS, timeout=15)
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        out["price"] = meta.get("regularMarketPrice")
        out["prev"] = meta.get("previousClose") or meta.get("chartPreviousClose")
        out["high"] = meta.get("regularMarketDayHigh")
        out["low"]  = meta.get("regularMarketDayLow")
        out["name"] = meta.get("longName") or meta.get("shortName") or sym
        q = res["indicators"]["quote"][0]
        cl, hi, lo = q.get("close", []), q.get("high", []), q.get("low", [])
        bars = []
        for i in range(len(cl)):
            if cl[i] is None:
                continue
            bars.append({"close": cl[i],
                         "high": hi[i] if i < len(hi) and hi[i] is not None else cl[i],
                         "low":  lo[i] if i < len(lo) and lo[i] is not None else cl[i]})
        out["candles"] = list(reversed(bars))[:160]   # recent -> ancien
        if out.get("price") and out.get("prev"):
            out["changePct"] = round((out["price"]/out["prev"] - 1)*100, 2)
    except Exception as e:
        out["chart_error"] = str(e)[:120]
    # 2) quoteSummary : fondamentaux (avec crumb)
    try:
        ck, crumb = await _yahoo_auth(client)
        h = dict(YH_HEADERS)
        if ck:
            h["cookie"] = ck
        r = await client.get(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}",
                             params={"modules": "summaryDetail,defaultKeyStatistics,financialData,price", "crumb": crumb},
                             headers=h, timeout=15)
        d = r.json()["quoteSummary"]["result"][0]
        sd, ks, fd, pr = d.get("summaryDetail", {}), d.get("defaultKeyStatistics", {}), d.get("financialData", {}), d.get("price", {})
        out["marketCap"] = _yraw(pr, "marketCap") or _yraw(sd, "marketCap")
        out["pe"]       = _yraw(sd, "trailingPE")
        out["pb"]       = _yraw(ks, "priceToBook")
        out["roe"]      = _yraw(fd, "returnOnEquity")    # décimal -> ×100 côté scorer
        out["margin"]   = _yraw(fd, "profitMargins")     # décimal -> ×100
        out["evEbitda"] = _yraw(ks, "enterpriseToEbitda")
        out["epsG"]     = _yraw(fd, "earningsGrowth")    # décimal -> ×100
        out["target"]   = _yraw(fd, "targetMeanPrice")
        out["recoMean"] = _yraw(fd, "recommendationMean")
        out["recoKey"]  = fd.get("recommendationKey")
        out["numAnalysts"] = _yraw(fd, "numberOfAnalystOpinions")
        if out.get("target") and out.get("price"):
            out["upside"] = round((out["target"]/out["price"] - 1)*100, 2)
    except Exception as e:
        out["fund_error"] = str(e)[:120]
    await cset("ys:" + sym, out, TTL_QUOTE)
    return out

@app.get("/api/ystock")
async def ystock_debug(symbol: str = "MC.PA"):
    """Test : /api/ystock?symbol=MC.PA"""
    async with httpx.AsyncClient() as client:
        return await yahoo_stock(symbol, client)
