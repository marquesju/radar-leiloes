import os, re, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
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

SP = ZoneInfo("America/Sao_Paulo")
DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}))?\b")
PRICE_RE = re.compile(r"R\$\s*([\d\.]+(?:,\d{1,2})?)")


def now_local():
    return datetime.now(SP)


def parse_dt(s):
    try:
        return datetime.strptime(s, "%d/%m/%Y %H:%M").replace(tzinfo=SP)
    except Exception:
        try:
            return datetime.strptime(s, "%d/%m/%Y").replace(tzinfo=SP)
        except Exception:
            return None


def future_dates(text):
    out = []
    for d, t in DATE_RE.findall(text or ""):
        dt = parse_dt(f"{d} {t or '23:59'}")
        if dt and dt >= now_local():
            out.append((dt, f"{d}{(' ' + t) if t else ''}"))
    return sorted(out, key=lambda x: x[0])


def money_values(s):
    vals = []
    for m in PRICE_RE.findall(s or ""):
        try:
            vals.append(float(m.replace(".", "").replace(",", ".")))
        except Exception:
            pass
    return vals


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def extract_address(text):
    # Keep a useful address fragment when the source exposes it.
    m = re.search(r"\b(Rua|R\.|Avenida|Av\.|Alameda|Praça|Rodovia|Estrada|Travessa|Ladeira)\b[^\n]{5,300}", text or "", re.I)
    return clean(m.group(0)) if m else ""


def modality_from(text):
    t = (text or "").lower()
    if "extrajudicial" in t:
        return "Extrajudicial"
    if "judicial" in t:
        return "Judicial"
    if "caixa" in t:
        return "Caixa"
    if "licitação" in t:
        return "Licitação"
    if "comprei pgfn" in t:
        return "PGFN"
    return ""


def relevant_block(anchor):
    # The previous version only inspected anchor.parent. On Leilão Imóvel the
    # property card is several levels above the link, so walk ancestors and
    # choose the smallest useful card containing both price and auction date.
    best = ""
    node = anchor
    for _ in range(8):
        node = getattr(node, "parent", None)
        if not node:
            break
        text = clean(node.get_text(" ", strip=True))
        if len(text) > 1800:
            continue
        if "R$" in text and ("Data de encerramento" in text or DATE_RE.search(text)):
            best = text
            if len(text) >= 180:
                return text
    return best or clean(anchor.get_text(" ", strip=True))


def is_property_title(text):
    t = (text or "").lower()
    return any(k in t for k in ["apartamento em leilão", "casa em leilão", "terreno", "loja em leilão", "sala comercial", "imóvel comercial", "vaga de garagem", "lote em leilão", "fazenda em leilão", "imóveis em leilão"])


def scrape_source(source, city, url):
    r = requests.get(url, headers=HEADERS, timeout=35)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()

    # First pass: anchors. Walk up the DOM so Leilão Imóvel cards are captured.
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" ", strip=True))
        block = relevant_block(a)
        if not block or "R$" not in block:
            continue
        if not ("leilão" in block.lower() or "leilao" in block.lower() or "Data de encerramento" in block):
            continue
        dates = future_dates(block)
        # Explicitly expired records are discarded. No future date = inactive.
        if not dates:
            continue
        prices = money_values(block)
        if not prices:
            continue

        # On Leilão Imóvel the first non-struck price is normally the current bid.
        price = prices[0]
        if source == "Leilão Imóvel" and len(prices) > 1:
            # If a current/future 2nd/3rd auction is present, prefer the price
            # closest to the future auction text rather than an old crossed-out price.
            price = prices[-1] if len(prices) <= 2 else prices[0]

        href = urljoin(url, a["href"])
        if not title or not is_property_title(title):
            # Use a nearby heading when the link itself is only an image/button.
            parent = a.parent
            headings = parent.find_all(["h1", "h2", "h3", "h4"], limit=3) if parent else []
            title = clean(headings[0].get_text(" ", strip=True)) if headings else title
        if not title:
            continue

        key = (city, source, href or title, dates[0][1])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "city": city,
            "title": title[:500],
            "address": extract_address(block),
            "price": price,
            "appraisal": prices[1] if len(prices) > 1 else None,
            "auction_date": dates[0][1],
            "modality": modality_from(block),
            "source": source,
            "source_url": href or url,
        })

    # Second pass: headings for sources whose property title is not itself a link.
    for node in soup.find_all(["h2", "h3", "h4"]):
        title = clean(node.get_text(" ", strip=True))
        if not is_property_title(title):
            continue
        node2 = node
        block = ""
        for _ in range(6):
            node2 = getattr(node2, "parent", None)
            if not node2:
                break
            txt = clean(node2.get_text(" ", strip=True))
            if "R$" in txt and DATE_RE.search(txt):
                block = txt
                if len(txt) <= 1800:
                    break
        dates = future_dates(block)
        prices = money_values(block)
        if not block or not dates or not prices:
            continue
        link = node.find("a", href=True) or (node2.find("a", href=True) if node2 else None)
        href = urljoin(url, link["href"]) if link else url
        key = (city, source, href, dates[0][1])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "city": city, "title": title[:500], "address": extract_address(block),
            "price": prices[0], "appraisal": prices[1] if len(prices) > 1 else None,
            "auction_date": dates[0][1], "modality": modality_from(block),
            "source": source, "source_url": href,
        })

    return out


def uid(item):
    raw = "|".join(str(item.get(k, "")) for k in ("city", "source", "title", "address", "auction_date", "price"))
    return hashlib.sha1(raw.lower().encode()).hexdigest()


def refresh():
    all_items, errors = [], []
    for source, cities in SOURCES.items():
        for city, url in cities.items():
            try:
                all_items.extend(scrape_source(source, city, url))
            except Exception as e:
                errors.append(f"{source}/{city}: {e}")

    now = datetime.utcnow()
    db = SessionLocal()
    # Remove old records from this source on each successful source scrape.
    # This prevents expired listings from remaining forever in SQLite.
    for source, cities in SOURCES.items():
        for city in cities:
            try:
                if not any(e.startswith(f"{source}/{city}:") for e in errors):
                    db.query(Listing).filter(Listing.source == source, Listing.city == city).delete()
            except Exception:
                pass
    db.commit()

    for x in all_items:
        db.add(Listing(uid=uid(x), first_seen=now, last_seen=now, **x))
    db.commit()
    db.close()
    return len(all_items), len(all_items), errors


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
    db = SessionLocal()
    n = db.query(Listing).count()
    db.close()
    if n == 0:
        try:
            refresh()
        except Exception:
            pass

    rows = snapshot_rows(None if city == "Todas as cidades" else city)
    if source != "Todos":
        rows = [r for r in rows if r.source == source]

    # Cross-source matching: same city + normalized address/title.
    green_keys = set()
    for r in rows:
        if r.source == "Leilão Imóvel":
            green_keys.add((r.city, re.sub(r"\W+", "", (r.address or r.title).lower())[:140]))

    cards = []
    for r in rows:
        key = (r.city, re.sub(r"\W+", "", (r.address or r.title).lower())[:140])
        status = "🟢" if r.source == "Leilão Imóvel" or key in green_keys else "🟠"
        cards.append(f"""
        <div class="card">
          <div class="top"><b>{status} {r.city}</b><span>{r.source}</span></div>
          <h3>{r.title}</h3>
          <div>{r.address}</div>
          <div class="price">R$ {r.price:,.2f}</div>
          <div>Próximo leilão: {r.auction_date or "não informado"} · {r.modality or "não informado"}</div>
          <a href="{r.source_url}" target="_blank">Abrir fonte</a>
        </div>""")

    html = f"""<!doctype html><html lang="pt-br"><head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta http-equiv="refresh" content="300">
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
      <div class="stat"><b>{len(rows)}</b> imóveis ativos exibidos</div>
      <div class="stat"><b>{sum(1 for r in rows if r.source=="Leilão Imóvel")}</b> no Leilão Imóvel</div>
      <div class="stat"><b>{sum(1 for r in rows if r.source!="Leilão Imóvel")}</b> fora do Leilão Imóvel</div>
    </div>
    <div class="filters">
      <form>
      <select name="city"><option>Todas as cidades</option><option {"selected" if city=="Guarapari" else ""}>Guarapari</option><option {"selected" if city=="Vitória" else ""}>Vitória</option></select>
      <select name="source"><option>Todos</option><option {"selected" if source=="Leilão Imóvel" else ""}>Leilão Imóvel</option><option {"selected" if source=="LeilôAI" else ""}>LeilôAI</option></select>
      <button>Filtrar</button>
      </form>
      <p>Atualizar agora: <a href="/run?token={os.getenv('RADAR_RUN_TOKEN','')}">executar coleta</a></p>
    </div>
    {''.join(cards) if cards else '<div class="card">Nenhum imóvel ativo encontrado.</div>'}
    </main></body></html>"""
    return HTMLResponse(html)
