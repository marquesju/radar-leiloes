import os, re, hashlib, json, threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker

SP = ZoneInfo("America/Sao_Paulo")
TARGET_CITIES = ("Guarapari", "Vitória")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
TIMEOUT = (5, 12)
MAX_WORKERS = int(os.getenv("CRAWL_WORKERS", "24"))

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./radar.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

class Listing(Base):
    __tablename__ = "listings"
    id = Column(Integer, primary_key=True)
    uid = Column(String(80), unique=True, index=True)
    city = Column(String(30), index=True)
    title = Column(String(500))
    address = Column(String(500))
    price = Column(Float)
    appraisal = Column(Float)
    auction_date = Column(String(100))
    modality = Column(String(120))
    source = Column(String(160))
    source_url = Column(Text)
    first_seen = Column(DateTime)
    last_seen = Column(DateTime)

class Source(Base):
    __tablename__ = "sources"
    id = Column(Integer, primary_key=True)
    name = Column(String(250), unique=True, index=True)
    url = Column(Text)
    state = Column(String(10))
    registration = Column(String(80))
    official = Column(Boolean, default=True)
    status = Column(String(40), default="discovered")
    last_checked = Column(DateTime)
    last_error = Column(Text)
    hits = Column(Integer, default=0)

class Meta(Base):
    __tablename__ = "meta"
    key = Column(String(100), primary_key=True)
    value = Column(Text)

Base.metadata.create_all(engine)
app = FastAPI(title="Radar Nacional de Leilões — Guarapari e Vitória")

DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})(?:\s*(?:às|as|at)?\s*(\d{1,2}:\d{2}))?\b", re.I)
PRICE_RE = re.compile(r"R\$\s*([\d\.]+(?:,\d{1,2})?)", re.I)
CITY_PATTERNS = {
    "Guarapari": re.compile(r"\bguarapari\b", re.I),
    "Vitória": re.compile(r"\bvit[oó]ria\b", re.I),
}

# Institutional / national sources are deliberately kept in addition to every official auctioneer site.
INSTITUTIONAL = [
    ("Leilão Imóvel", "https://www.leilaoimovel.com.br/leilao-de-imovel/guarapari", "AGG"),
    ("Leilão Imóvel", "https://www.leilaoimovel.com.br/leilao-de-imovel/vitoria-es", "AGG"),
    ("LeilôAI", "https://www.leiloai.com/leiloes/imovel/es/guarapari", "AGG"),
    ("LeilôAI", "https://www.leiloai.com/leiloes/imovel/es/vitoria", "AGG"),
    ("Núcleo Leilões", "https://nucleoleiloes.com.br/", "AGG"),
    ("INNLEI", "https://innlei.org.br/", "AGG"),
    ("Central de Leilões", "https://centraleiloes.com.br/", "DIR"),
    ("Caixa", "https://www.caixa.gov.br/voce/habitacao/imoveis-venda/Paginas/default.aspx", "BANK"),
    ("Comprei / PGFN", "https://comprei.pgfn.gov.br/", "GOV"),
]

APIFY_ACTOR = "getascraper~leilaoimovel-scraper"

def now_local():
    return datetime.now(SP)

def parse_dt(s):
    if not s: return None
    s = str(s).strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=SP) if dt.tzinfo is None else dt.astimezone(SP)
        except Exception: pass
    return None

def clean(s): return re.sub(r"\s+", " ", str(s or "")).strip()

def money_values(s):
    out=[]
    for x in PRICE_RE.findall(s or ""):
        try: out.append(float(x.replace(".", "").replace(",", ".")))
        except: pass
    return out

def future_dates(text):
    out=[]
    for d,t in DATE_RE.findall(text or ""):
        dt=parse_dt(f"{d} {t or '23:59'}")
        if dt and dt >= now_local(): out.append((dt, f"{d}{(' '+t) if t else ''}"))
    return sorted(out, key=lambda x:x[0])

def modality_from(text):
    t=clean(text).lower()
    if "extrajudicial" in t: return "Extrajudicial"
    if "judicial" in t: return "Judicial"
    if "sfi" in t: return "Leilão SFI"
    if "licitação" in t or "licitacao" in t: return "Licitação"
    if "venda online" in t: return "Venda Online"
    return ""

def extract_address(text):
    m=re.search(r"\b(?:Rua|R\.|Avenida|Av\.|Alameda|Praça|Rodovia|Estrada|Travessa|Ladeira)\b.{5,300}", text or "", re.I)
    if not m: return ""
    return clean(re.split(r"\b(?:1ª|2ª|3ª|1a|2a|3a)\s+Praça\s*:?", m.group(0), maxsplit=1, flags=re.I)[0])

def page_get(url):
    r=requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    if not r.ok or len(r.text)<80: raise RuntimeError(f"HTTP {r.status_code}")
    return r.text, r.url

def target_city(text):
    for city,pat in CITY_PATTERNS.items():
        if pat.search(text or ""): return city
    return None

def candidate_links(base_url, html):
    soup=BeautifulSoup(html,"html.parser")
    out=[]
    for a in soup.find_all("a", href=True):
        href=urljoin(base_url,a.get("href"))
        p=urlparse(href)
        if p.scheme not in ("http","https") or p.netloc != urlparse(base_url).netloc: continue
        txt=clean(a.get_text(" ",strip=True))
        low=(href+" "+txt).lower()
        if any(x in low for x in ("guarapari","vitoria","vitória")):
            out.append(href.split("#")[0])
    return list(dict.fromkeys(out))[:20]

def sitemap_links(base_url):
    """Read robots/sitemap hints once. We only keep URLs that visibly contain a target city."""
    found=[]
    try:
        robots,_=page_get(urljoin(base_url,"/robots.txt"))
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                found.append(line.split(":",1)[1].strip())
    except Exception: pass
    found += [urljoin(base_url,"/sitemap.xml"), urljoin(base_url,"/sitemap_index.xml")]
    urls=[]
    for sm in list(dict.fromkeys(found))[:3]:
        try:
            xml,_=page_get(sm)
            soup=BeautifulSoup(xml,"xml")
            locs=[clean(x.get_text()) for x in soup.find_all("loc")]
            for u in locs:
                if any(CITY_PATTERNS[c].search(u) for c in TARGET_CITIES): urls.append(u)
                elif u.endswith(".xml") and len(urls)<20:
                    try:
                        sub,_=page_get(u)
                        ss=BeautifulSoup(sub,"xml")
                        for z in ss.find_all("loc"):
                            zz=clean(z.get_text())
                            if any(CITY_PATTERNS[c].search(zz) for c in TARGET_CITIES): urls.append(zz)
                    except Exception: pass
        except Exception: pass
    return list(dict.fromkeys(urls))[:20]

def parse_listing_block(node, base_url):
    text=clean(node.get_text(" ",strip=True))
    if len(text)<45 or "R$" not in text: return None
    city=target_city(text)
    if not city: return None
    dates=future_dates(text)
    if not dates: return None
    prices=money_values(text)
    if not prices: return None
    # Avoid treating an enormous container as one listing.
    if len(text)>3500: return None
    title=""
    for h in node.find_all(["h1","h2","h3","h4","strong"],limit=4):
        t=clean(h.get_text(" ",strip=True))
        if 8<=len(t)<=500: title=t; break
    if not title: title=clean(text[:260])
    a=node.find("a",href=True)
    href=urljoin(base_url,a.get("href")) if a else base_url
    return {"city":city,"title":title[:500],"address":extract_address(text)[:500],"price":prices[0],"appraisal":prices[1] if len(prices)>1 else None,"auction_date":dates[0][1],"modality":modality_from(text),"source_url":href}

def generic_extract(base_url, html, source_name):
    soup=BeautifulSoup(html,"html.parser")
    nodes=soup.find_all(["article","li","section","div"])
    out=[]; seen=set()
    for n in nodes:
        item=parse_listing_block(n,base_url)
        if not item: continue
        key=(item["city"],item["source_url"],item["title"],item["auction_date"],item["price"])
        if key in seen: continue
        seen.add(key); item["source"]=source_name; out.append(item)
    return out[:200]

def crawl_official_source(src):
    """Search one official auctioneer website. Every registered site reaches this function."""
    name=src.name; base=src.url
    if not base: return [], "sem site"
    try:
        html,final=page_get(base)
        items=generic_extract(final,html,name)
        # If homepage itself does not expose target lots, follow city-specific links and sitemap URLs.
        links=candidate_links(final,html)
        links += sitemap_links(final)
        for link in list(dict.fromkeys(links))[:20]:
            try:
                h,u=page_get(link)
                items.extend(generic_extract(u,h,name))
            except Exception: continue
        uniq={}
        for x in items: uniq[(x["city"],x["source_url"],x["title"],x["auction_date"])]=x
        return list(uniq.values())[:200], None
    except Exception as e:
        return [], str(e)[:240]

def discover_fenaju():
    """Build/refresh the national registry from FENAJU. It is the master list of official auctioneers."""
    sources={}
    first="https://www.fenaju.org.br/leiloeiros"
    try:
        html,_=page_get(first)
    except Exception as e:
        return 0, str(e)
    soup=BeautifulSoup(html,"html.parser")
    # FENAJU currently exposes pagination links. We discover the largest page rather than hard-code 141.
    pages={1}
    for a in soup.find_all("a",href=True):
        m=re.search(r"[?&]page=(\d+)",a.get("href",""))
        if m: pages.add(int(m.group(1)))
    max_page=min(max(pages),180)
    for page in range(1,max_page+1):
        url=first if page==1 else f"{first}?page={page}"
        try: h,_=page_get(url)
        except Exception: continue
        ss=BeautifulSoup(h,"html.parser")
        # Official leilao.br domains and explicit website links are both accepted.
        for a in ss.find_all("a",href=True):
            href=urljoin(url,a.get("href"))
            txt=clean(a.get_text(" ",strip=True))
            domain=urlparse(href).netloc.lower()
            if not domain or domain in {"www.fenaju.org.br","fenaju.org.br"}: continue
            if domain.startswith("www."): domain=domain[4:]
            bad=("facebook.com","instagram.com","linkedin.com","youtube.com","twitter.com","x.com","whatsapp.com","google.com","registro.br","fenaju.org.br")
            if any(domain==b or domain.endswith("."+b) for b in bad): continue
            # FENAJU cards can point to a normal .com/.com.br site or to leilao.br.
            # Keep any plausible external site, because the goal is national coverage.
            if "." in domain and not href.lower().endswith((".pdf",".jpg",".jpeg",".png",".gif",".webp")):
                sources[domain]=(txt or domain,href)
        # Also parse visible domain text because FENAJU sometimes renders the verified site as plain text.
        for tag in ss.find_all(string=re.compile(r"[a-z0-9.-]+\.leilao\.br",re.I)):
            m=re.search(r"([a-z0-9.-]+\.leilao\.br)",str(tag),re.I)
            if m: sources[m.group(1).lower()]=(m.group(1),"https://"+m.group(1))
    db=SessionLocal(); count=0
    for domain,(name,url) in sources.items():
        existing=db.query(Source).filter(Source.url.ilike(f"%{domain}%")).first()
        if existing:
            if existing.official is None: existing.official=True
            continue
        db.add(Source(name=name[:250],url=url,state="BR",official=True,status="discovered")); count+=1
    db.commit(); db.close()
    return count, None

def ensure_registry(force=False):
    db=SessionLocal(); m=db.query(Meta).filter(Meta.key=="fenaju_last_sync").first()
    due=force or not m
    if m and not due:
        try: due=(datetime.fromisoformat(m.value) < datetime.utcnow()-timedelta(days=7))
        except: due=True
    db.close()
    if due:
        n,err=discover_fenaju()
        db=SessionLocal();
        m=db.query(Meta).filter(Meta.key=="fenaju_last_sync").first()
        if not m: db.add(Meta(key="fenaju_last_sync",value=datetime.utcnow().isoformat()))
        else: m.value=datetime.utcnow().isoformat()
        if err: db.add(Meta(key="fenaju_last_error",value=err))
        db.commit(); db.close()
        return n,err
    return 0,None

def apify_leilao_imovel(city):
    token=os.getenv("APIFY_TOKEN","").strip()
    if not token: raise RuntimeError("APIFY_TOKEN não configurado")
    payload={"state":"ES","city":city,"includeDetails":False,"maxItems":100,"maxPages":10,"maxConcurrency":2}
    endpoint=f"https://api.apify.com/v2/actors/{APIFY_ACTOR}/run-sync-get-dataset-items"
    r=requests.post(endpoint,headers={**HEADERS,"Authorization":f"Bearer {token}","Content-Type":"application/json"},json=payload,timeout=290)
    if not r.ok: raise RuntimeError(f"Apify HTTP {r.status_code}")
    data=r.json(); out=[]
    for x in data if isinstance(data,list) else []:
        dates=[]
        for f in ("closing_date","first_round_date","second_round_date","third_round_date"):
            dt=parse_dt(x.get(f));
            if dt and dt>=now_local(): dates.append((dt,f))
        if not dates: continue
        dt,field=sorted(dates,key=lambda z:z[0])[0]
        price=x.get("price_brl")
        if field=="second_round_date": price=x.get("second_round_price_brl",price)
        if field=="third_round_date": price=x.get("third_round_price_brl",price)
        try: price=float(price)
        except: continue
        out.append({"city":city,"title":clean(x.get("title"))[:500],"address":clean(x.get("address"))[:500],"price":price,"appraisal":x.get("appraisal_value_brl"),"auction_date":dt.strftime("%d/%m/%Y %H:%M"),"modality":modality_from(x.get("sale_types")),"source":"Leilão Imóvel","source_url":x.get("url") or x.get("listing_url") or ""})
    return out

def crawl_institutional():
    out=[]; errors=[]
    for name,url,kind in INSTITUTIONAL:
        if name=="Leilão Imóvel":
            if not os.getenv("APIFY_TOKEN"): errors.append(f"{name}: APIFY_TOKEN ausente"); continue
            try:
                for city in TARGET_CITIES: out.extend(apify_leilao_imovel(city))
            except Exception as e: errors.append(f"{name}: {e}")
            continue
        try:
            html,final=page_get(url); out.extend(generic_extract(final,html,name))
        except Exception as e: errors.append(f"{name}: {e}")
    return out,errors

def uid(x):
    raw="|".join(str(x.get(k,"")) for k in ("city","source","title","address","auction_date","price"))
    return hashlib.sha1(raw.lower().encode()).hexdigest()

def refresh(force_registry=False):
    ensure_registry(force_registry)
    all_items=[]; errors=[]
    inst,errs=crawl_institutional(); all_items.extend(inst); errors.extend(errs)
    db=SessionLocal(); sources=db.query(Source).filter(Source.official==True,Source.url.isnot(None),Source.url!="").all(); db.close()
    # Every registered official source is attempted. Failures are recorded; they do not erase prior good data.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs={pool.submit(crawl_official_source,s):s for s in sources}
        for fut in as_completed(futs):
            s=futs[fut]
            try: items,err=fut.result()
            except Exception as e: items,err=[],str(e)
            db=SessionLocal(); row=db.query(Source).get(s.id); row.last_checked=datetime.utcnow(); row.last_error=err; row.status="ok" if not err else "error"; row.hits=(row.hits or 0)+len(items); db.commit(); db.close()
            if err: errors.append(f"{s.name}: {err}")
            all_items.extend(items)
    # Deduplicate globally while retaining the best source URL.
    uniq={}
    for x in all_items:
        if x.get("city") not in TARGET_CITIES: continue
        dt=parse_dt(x.get("auction_date"))
        if not dt or dt<now_local(): continue
        k=(x["city"],re.sub(r"\W+","",(x.get("address") or x.get("title") or "").lower())[:180],round(float(x.get("price") or 0),2),x.get("auction_date"))
        uniq[k]=x
    db=SessionLocal(); db.query(Listing).delete(); now=datetime.utcnow()
    for x in uniq.values(): db.add(Listing(uid=uid(x),first_seen=now,last_seen=now,**x))
    db.commit(); db.close()
    return len(uniq),len(uniq),errors

def active_rows(city=None):
    db=SessionLocal(); q=db.query(Listing)
    if city in TARGET_CITIES: q=q.filter(Listing.city==city)
    rows=q.order_by(Listing.price.asc()).all(); db.close()
    return [r for r in rows if (parse_dt(r.auction_date) or datetime.min.replace(tzinfo=SP))>=now_local()]

@app.get("/health")
def health():
    db=SessionLocal(); n=db.query(Source).count(); ok=db.query(Source).filter(Source.status=="ok").count(); db.close()
    return {"ok":True,"official_sources_registered":n,"official_sources_ok":ok,"apify_configured":bool(os.getenv("APIFY_TOKEN"))}

@app.get("/sources")
def sources():
    db=SessionLocal(); rows=db.query(Source).order_by(Source.name).all(); db.close()
    return [{"name":r.name,"url":r.url,"status":r.status,"last_checked":r.last_checked,"hits":r.hits} for r in rows]

@app.get("/run")
def run(token:str=Query(default="")):
    expected=os.getenv("RADAR_RUN_TOKEN","")
    if expected and token!=expected: return {"ok":False,"error":"token inválido"}
    found,new,errors=refresh(False)
    return {"ok":True,"found":found,"new":new,"official_sources_attempted":SessionLocal().query(Source).count(),"errors":errors[:80],"error_count":len(errors)}

@app.get("/",response_class=HTMLResponse)
def dashboard(city:str="Todas as cidades"):
    rows=active_rows(None if city=="Todas as cidades" else city)
    db=SessionLocal(); source_count=db.query(Source).count(); checked=db.query(Source).filter(Source.status=="ok").count(); db.close()
    cards=[]
    for r in rows:
        color="green" if r.source=="Leilão Imóvel" else "orange"
        cards.append(f'<div class="card"><div class="top"><b class="{color}">● {r.city}</b><span>{r.source}</span></div><h3>{r.title}</h3><div>{r.address}</div><div class="price">R$ {r.price:,.2f}</div><div>Próximo leilão: {r.auction_date} · {r.modality or "não informado"}</div><a href="{r.source_url}" target="_blank">Abrir fonte</a></div>')
    html=f'''<!doctype html><html lang="pt-br"><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300"><title>Radar Nacional</title><style>body{{font-family:Arial;margin:0;background:#f4f6f8;color:#172033}}header{{background:#111827;color:#fff;padding:20px}}main{{max-width:1100px;margin:auto;padding:16px}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.stat,.card,.filters{{background:#fff;border-radius:16px;padding:16px;margin:10px 0;box-shadow:0 2px 10px #0001}}.stat b{{font-size:28px;display:block}}.top{{display:flex;justify-content:space-between;color:#64748b}}.price{{font-size:23px;font-weight:bold;margin:9px 0}}.green{{color:#15803d}}.orange{{color:#ea580c}}a{{color:#2563eb}}select,button{{padding:11px;border-radius:9px;border:1px solid #ccd3dd}}button{{background:#111827;color:#fff}}@media(max-width:700px){{.stats{{grid-template-columns:1fr 1fr}}}}</style></head><body><header><h1>🔎 Radar Nacional de Leilões</h1><div>Imóveis localizados em Guarapari e Vitória — fontes nacionais + leiloeiros oficiais</div></header><main><div class="stats"><div class="stat"><b>{len(rows)}</b> imóveis ativos</div><div class="stat"><b>{sum(1 for r in rows if r.source=="Leilão Imóvel")}</b> no Leilão Imóvel</div><div class="stat"><b>{sum(1 for r in rows if r.source!="Leilão Imóvel")}</b> outras fontes</div><div class="stat"><b>{source_count}</b> sites oficiais cadastrados</div></div><div class="filters"><form><select name="city"><option>Todas as cidades</option><option {'selected' if city=='Guarapari' else ''}>Guarapari</option><option {'selected' if city=='Vitória' else ''}>Vitória</option></select><button>Filtrar</button></form><p><a href="/run?token={os.getenv('RADAR_RUN_TOKEN','')}">Executar coleta nacional</a> · <a href="/sources">ver cadastro de fontes</a></p><small>{checked} sites oficiais já responderam com sucesso em alguma coleta.</small></div>{''.join(cards) if cards else '<div class="card">Nenhum imóvel ativo encontrado.</div>'}</main></body></html>'''
    return HTMLResponse(html)
