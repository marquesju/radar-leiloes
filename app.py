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
    modality = Column(String(80))
    source = Column(String(80))
    source_url = Column(Text)
    first_seen = Column(DateTime)
    last_seen = Column(DateTime)

Base.metadata.create_all(engine)

app = FastAPI(title="Radar de Leilões")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
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
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=SP)
        except Exception:
            pass
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
    m = re.search(r"\b(Rua|R\.|Avenida|Av\.|Alameda|Praça|Rodovia|Estrada|Travessa|Ladeira)\b[^\n]{5,300}", text or "", re.I)
    return clean(re.split(r"\b(?:1ª|2ª|3ª)\s+Praça\s*:", m.group(0), maxsplit=1, flags=re.I)[0]) if m else ""


def modality_from(text):
    t = (text or "").lower()
    if "extrajudicial" in t:
        return "Extrajudicial"
    if "judicial" in t:
        return "Judicial"
    if "leilão sfi" in t:
        return "Leilão SFI"
    if "caixa" in t:
        return "Caixa"
    if "licitação" in t:
        return "Licitação"
    if "comprei pgfn" in t:
        return "PGFN"
    return ""


def fetch_page(url, source):
    """Fetch normally. Leilão Imóvel uses Cloudflare and may block Render IPs.
    In that case use Jina's public reader as a read-only fallback. No login or
    CAPTCHA bypass is attempted.
    """
    errors = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.ok and len(r.text) > 500:
            return r.text, "html", errors
        errors.append(f"direct HTTP {r.status_code}")
    except Exception as e:
        errors.append(f"direct {type(e).__name__}: {e}")

    if source == "Leilão Imóvel":
        proxy = "https://r.jina.ai/http://" + url.split("//", 1)[1]
        try:
            r = requests.get(proxy, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=45)
            if r.ok and len(r.text) > 500:
                return r.text, "text", errors
            errors.append(f"reader HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"reader {type(e).__name__}: {e}")
    raise RuntimeError("; ".join(errors))


def is_property_title(text):
    t = (text or "").lower()
    return any(k in t for k in [
        "apartamento em leilão", "apartamentos em leilão", "casa em leilão",
        "terreno em leilão", "terreno urbano em leilão", "lote de terreno em leilão",
        "lote em leilão", "loja em leilão", "sala comercial em leilão",
        "imóvel comercial em leilão", "vaga de garagem em leilão", "área de terras em leilão"
    ])


def extract_leilao_imovel_text(text, city, url):
    """Parse the server/reader text of the Leilão Imóvel city page.
    Each property is represented by a block beginning with 'Data de encerramento'.
    """
    out = []
    # Normalize reader markdown/HTML into one searchable text stream.
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)

    marker = re.compile(r"Data de encerramento:\s*\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?", re.I)
    starts = [m.start() for m in marker.finditer(text)]
    for i, start in enumerate(starts):
        block = text[start:(starts[i + 1] if i + 1 < len(starts) else len(text))]
        if len(block) > 5000:
            block = block[:5000]
        dates = future_dates(block)
        if not dates:
            continue
        # Property title is usually the first occurrence after the price(s).
        mtitle = re.search(r"((?:Apartamento|Apartamentos|Casa|Terreno|Terreno Urbano|Lote De Terreno|Lote|Loja|Sala Comercial|Imóvel Comercial|Vaga de Garagem|Área De Terras) em Leilão em [^0-9]{2,120}?\s*-\s*\d{5,10}[^\n]{0,350})", block, re.I)
        if not mtitle:
            mtitle = re.search(r"((?:Apartamento|Apartamentos|Casa|Terreno|Terreno Urbano|Lote De Terreno|Lote|Loja|Sala Comercial|Imóvel Comercial|Vaga de Garagem|Área De Terras) em Leilão[^\n]{0,500})", block, re.I)
        title = clean(mtitle.group(1)) if mtitle else ""
        if not title:
            continue
        prices = money_values(block)
        if not prices:
            continue
        # Current price is the first non-struck/current price shown by the source.
        price = prices[0]
        # If the first price is followed by an appraisal, keep first as minimum.
        appraisal = prices[1] if len(prices) > 1 else None
        # Use the URL only at city-page level if the detail URL cannot be recovered.
        # The numeric Leilão Imóvel code is useful for a stable detail URL.
        code_m = re.search(r"-\s*(\d{5,10})\b", title)
        detail = url
        if code_m:
            detail = url
        out.append({
            "city": city,
            "title": title[:500],
            "address": extract_address(block),
            "price": price,
            "appraisal": appraisal,
            "auction_date": dates[0][1],
            "modality": modality_from(block),
            "source": "Leilão Imóvel",
            "source_url": detail,
        })
    return out


def scrape_source(source, city, url):
    raw, kind, fetch_errors = fetch_page(url, source)
    if source == "Leilão Imóvel" and kind == "text":
        return extract_leilao_imovel_text(raw, city, url)

    soup = BeautifulSoup(raw, "html.parser") if kind == "html" else None
    out, seen = [], set()
    if not soup:
        return []

    # Generic fallback parser for LeilôAI and other HTML sources.
    for node in soup.find_all(["a", "article", "div"]):
        text = clean(node.get_text(" ", strip=True))
        if len(text) < 60 or "R$" not in text:
            continue
        dates = future_dates(text)
        if not dates:
            continue
        prices = money_values(text)
        if not prices:
            continue
        if not any(k in text.lower() for k in ["leilão", "leilao", "praça", "praça"]):
            continue
        # Prefer an explicit heading in the local block.
        title = ""
        for h in node.find_all(["h1", "h2", "h3", "h4"], limit=3):
            t = clean(h.get_text(" ", strip=True))
            if t:
                title = t
                break
        if not title:
            title = clean(text[:300])
        href = url
        a = node.find("a", href=True) if hasattr(node, "find") else None
        if a:
            href = urljoin(url, a.get("href"))
        key = (city, source, href, dates[0][1], round(prices[0], 2))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "city": city,
            "title": title[:500],
            "address": extract_address(text),
            "price": prices[0],
            "appraisal": prices[1] if len(prices) > 1 else None,
            "auction_date": dates[0][1],
            "modality": modality_from(text),
            "source": source,
            "source_url": href,
        })
    return out


def uid(item):
    raw = "|".join(str(item.get(k, "")) for k in ("city", "source", "title", "address", "auction_date", "price"))
    return hashlib.sha1(raw.lower().encode()).hexdigest()


def refresh():
    all_items, errors = [], []
    successful = set()
    for source, cities in SOURCES.items():
        for city, url in cities.items():
            try:
                items = scrape_source(source, city, url)
                if items:
                    all_items.extend(items)
                    successful.add((source, city))
                else:
                    errors.append(f"{source}/{city}: coleta retornou 0 ativos")
            except Exception as e:
                errors.append(f"{source}/{city}: {e}")

    now = datetime.utcnow()
    db = SessionLocal()
    # Only replace a source/city snapshot after a successful non-empty scrape.
    # This prevents a temporary Cloudflare/network failure from erasing good data.
    for source, city in successful:
        db.query(Listing).filter(Listing.source == source, Listing.city == city).delete()
    db.commit()

    # Avoid exact duplicates from repeated cards/pages in the same source.
    seen_uids = set()
    for x in all_items:
        u = uid(x)
        if u in seen_uids:
            continue
        seen_uids.add(u)
        db.add(Listing(uid=u, first_seen=now, last_seen=now, **x))
    db.commit()
    db.close()
    return len(seen_uids), len(seen_uids), errors


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
    .error{{background:#fff7ed;border:1px solid #fed7aa;padding:12px;border-radius:12px}}
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
