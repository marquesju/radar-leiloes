import os, re, hashlib
from datetime import datetime
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./radar.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
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
    modality = Column(String(80))
    source = Column(String(80))
    source_url = Column(Text)
    first_seen = Column(DateTime)
    last_seen = Column(DateTime)

Base.metadata.create_all(engine)

app = FastAPI(title="Radar de Leilões")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/130 Safari/537.36"
}
SOURCES = {
    "Leilão Imóvel": {
        "Guarapari": "https://www.leilaoimovel.com.br/leilao-de-imovel/guarapari-es",
        "Vitória": "https://www.leilaoimovel.com.br/leilao-de-imovel/vitoria-es",
    },
    "LeilôAI": {
        "Guarapari": "https://www.leiloai.com/leiloes/imovel/es/guarapari",
        "Vitória": "https://www.leiloai.com/leiloes/imovel/es/vitoria",
    },
}

def money(s):
    if not s:
        return None
    m = re.search(r'R\$\s*([\d\.\,]+)', s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except:
        return None

def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def uid(city, title, address, price):
    raw = f"{city}|{title}|{address}|{price}".lower()
    return hashlib.sha1(raw.encode()).hexdigest()

def scrape_source(source, city, url):
    out = []
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Strategy 1: cards/links containing property-like text.
    seen = set()
    for a in soup.find_all("a", href=True):
        txt = clean(a.get_text(" ", strip=True))
        if len(txt) < 15 or "leilão" not in txt.lower() and "imóvel" not in txt.lower():
            continue
        block = clean(a.parent.get_text(" ", strip=True)) if a.parent else txt
        if len(block) > 1600:
            block = block[:1600]
        price = money(block)
        if price is None:
            continue
        href = urljoin(url, a["href"])
        title = txt[:500]
        # Try to recover address from the block.
        address = ""
        am = re.search(r"(Rua|R\.|Avenida|Av\.|Alameda|Praça|Rodovia|Estrada)[^|]{5,250}", block, re.I)
        if am:
            address = clean(am.group(0))
        # Auction date.
        dm = re.search(r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2})?", block)
        auction_date = dm.group(0) if dm else ""
        modality = ""
        for word in ("Judicial", "Extrajudicial", "Caixa", "Licitação"):
            if word.lower() in block.lower():
                modality = word
                break
        key = (title, address, price, href)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "city": city, "title": title, "address": address,
            "price": price, "appraisal": None,
            "auction_date": auction_date, "modality": modality,
            "source": source, "source_url": href
        })

    # Strategy 2: headings/containers (especially LeilôAI).
    for node in soup.find_all(["h2", "h3", "h4"]):
        title = clean(node.get_text(" ", strip=True))
        if not title:
            continue
        parent = node.parent
        block = clean(parent.get_text(" ", strip=True)) if parent else title
        price = money(block)
        if price is None:
            continue
        href = ""
        link = node.find("a", href=True) or (parent.find("a", href=True) if parent else None)
        if link:
            href = urljoin(url, link["href"])
        dm = re.search(r"(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2})?", block)
        auction_date = dm.group(0) if dm else ""
        uid0 = (title, price, auction_date)
        if any((x["title"], x["price"], x["auction_date"]) == uid0 for x in out):
            continue
        out.append({
            "city": city, "title": title[:500], "address": "",
            "price": price, "appraisal": None,
            "auction_date": auction_date, "modality": "",
            "source": source, "source_url": href or url
        })
    return out

def refresh():
    all_items = []
    errors = []
    for source, cities in SOURCES.items():
        for city, url in cities.items():
            try:
                all_items.extend(scrape_source(source, city, url))
            except Exception as e:
                errors.append(f"{source}/{city}: {e}")
    now = datetime.utcnow()
    db = SessionLocal()
    count_new = 0
    for x in all_items:
        # Keep source-specific records; same property across sources is later visually grouped.
        u = uid(x["city"], x["title"], x["address"], x["price"]) + hashlib.sha1(x["source"].encode()).hexdigest()[:8]
        row = db.query(Listing).filter_by(uid=u).first()
        if row:
            row.last_seen = now
            row.title, row.address = x["title"], x["address"]
            row.price, row.auction_date = x["price"], x["auction_date"]
            row.modality, row.source_url = x["modality"], x["source_url"]
        else:
            db.add(Listing(uid=u, first_seen=now, last_seen=now, **x))
            count_new += 1
    db.commit()
    db.close()
    return len(all_items), count_new, errors

def snapshot_rows(city=None):
    db = SessionLocal()
    q = db.query(Listing)
    if city in ("Guarapari", "Vitória"):
        q = q.filter(Listing.city == city)
    rows = q.order_by(Listing.price.asc()).all()
    db.close()
    return rows

@app.get("/health")
def health():
    return {"ok": True, "app": "radar-leiloes"}

@app.get("/run")
def run(token: str = Query(default="")):
    expected = os.getenv("RADAR_RUN_TOKEN", "")
    if expected and token != expected:
        return {"ok": False, "error": "token inválido"}
    found, new, errors = refresh()
    return {"ok": True, "found": found, "new": new, "errors": errors}

@app.get("/", response_class=HTMLResponse)
def dashboard(city: str = "Todas as cidades", source: str = "Todos"):
    # Populate automatically if database is empty.
    db = SessionLocal()
    n = db.query(Listing).count()
    db.close()
    if n == 0:
        try: refresh()
        except: pass

    rows = snapshot_rows(None if city == "Todas as cidades" else city)
    if source != "Todos":
        rows = [r for r in rows if r.source == source]

    # Determine which normalized title/address appears on Leilão Imóvel.
    green_keys = set()
    for r in rows:
        if r.source == "Leilão Imóvel":
            green_keys.add((r.city, re.sub(r"\W+", "", (r.address or r.title).lower())[:120]))

    cards = []
    for r in rows:
        key = (r.city, re.sub(r"\W+", "", (r.address or r.title).lower())[:120])
        status = "🟢" if r.source == "Leilão Imóvel" or key in green_keys else "🟠"
        cards.append(f"""
        <div class="card">
          <div class="top"><b>{status} {r.city}</b><span>{r.source}</span></div>
          <h3>{r.title}</h3>
          <div>{r.address}</div>
          <div class="price">R$ {r.price:,.2f}</div>
          <div>Leilão: {r.auction_date or "não informado"} · {r.modality or "não informado"}</div>
          <a href="{r.source_url}" target="_blank">Abrir fonte</a>
        </div>""")

    html = f"""<!doctype html><html lang="pt-br"><head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Radar de Leilões</title>
    <style>
    body{{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;color:#172033}}
    header{{background:#111827;color:white;padding:20px}}
    main{{max-width:1000px;margin:auto;padding:16px}}
    .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
    .stat,.card,.filters{{background:white;border-radius:16px;padding:18px;margin:12px 0;box-shadow:0 2px 10px #0001}}
    .stat b{{font-size:30px;display:block}}
    .top{{display:flex;justify-content:space-between;color:#64748b}}
    .price{{font-size:24px;font-weight:bold;margin:10px 0}}
    a{{color:#2563eb}}
    select,button{{padding:12px;border-radius:10px;border:1px solid #ccd3dd}}
    button{{background:#111827;color:white}}
    @media(max-width:650px){{.stats{{grid-template-columns:1fr}}}}
    </style></head><body>
    <header><h1>🔎 Radar de Leilões</h1><div>Guarapari + Vitória · judicial · extrajudicial</div></header>
    <main>
    <div class="stats">
      <div class="stat"><b>{len(rows)}</b> imóveis exibidos</div>
      <div class="stat"><b>{sum(1 for r in rows if r.source=="Leilão Imóvel")}</b> no Leilão Imóvel</div>
      <div class="stat"><b>{sum(1 for r in rows if r.source!="Leilão Imóvel")}</b> fora do Leilão Imóvel</div>
    </div>
    <div class="filters">
      <form>
      <select name="city"><option>Todas as cidades</option><option {"selected" if city=="Guarapari" else ""}>Guarapari</option><option {"selected" if city=="Vitória" else ""}>Vitória</option></select>
      <select name="source"><option>Todos</option><option {"selected" if source=="Leilão Imóvel" else ""}>Leilão Imóvel</option><option {"selected" if source=="LeilôAI" else ""}>LeilôAI</option></select>
      <button>Filtrar</button>
      </form>
      <p>Atualização: <a href="/run?token={os.getenv("RADAR_RUN_TOKEN","")}">executar coleta agora</a></p>
    </div>
    {''.join(cards) if cards else '<div class="card">Nenhum imóvel encontrado.</div>'}
    </main></body></html>"""
    return html
