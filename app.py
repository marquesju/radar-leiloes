import os, re, json
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, desc
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./radar.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql+psycopg://', 1)
elif DATABASE_URL.startswith('postgresql://'):
    DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
connect_args = {'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class Property(Base):
    __tablename__ = 'properties'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    city = Column(String(120), nullable=False)
    state = Column(String(10), default='ES')
    neighborhood = Column(String(180), default='')
    address = Column(String(500), default='')
    area_m2 = Column(Float, nullable=True)
    current_bid = Column(Float, nullable=True)
    appraisal = Column(Float, nullable=True)
    auction_date = Column(String(100), default='')
    auction_type = Column(String(100), default='')
    source = Column(String(180), default='')
    source_url = Column(Text, default='')
    official_auctioneer = Column(String(180), default='')
    status = Column(String(40), default='active')
    leilao_imovel_status = Column(String(20), default='unknown')
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)
    fingerprint = Column(String(300), unique=True, index=True)

Base.metadata.create_all(engine)

app = FastAPI(title='Radar de Leilões', version='2.1')
RUN_TOKEN = os.getenv('RADAR_RUN_TOKEN', '')

CSS = '''body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f4f6f8;color:#17202a}header{background:#111827;color:#fff;padding:18px 20px}main{max-width:1100px;margin:auto;padding:18px}.cards{display:flex;gap:10px;flex-wrap:wrap}.card{background:white;border-radius:12px;padding:14px 18px;box-shadow:0 1px 4px #0001;min-width:130px}.filters{background:#fff;padding:14px;border-radius:12px;margin:15px 0;display:flex;gap:8px;flex-wrap:wrap}input,select,button{padding:9px;border:1px solid #ccd3da;border-radius:8px}button{background:#111827;color:white}.grid{display:grid;gap:12px}.item{background:#fff;border-left:7px solid #9ca3af;padding:15px;border-radius:10px;box-shadow:0 1px 4px #0001}.yes{border-left-color:#16a34a}.no{border-left-color:#f97316}.inst{border-left-color:#2563eb}.muted{color:#6b7280;font-size:.9em}.price{font-size:1.25em;font-weight:700}a{color:#2563eb}'''

HTML_HEAD='''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar de Leilões</title><style>'''+CSS+'''</style></head><body><header><b>🔎 Radar de Leilões</b><div class="muted" style="color:#d1d5db">Guarapari + Vitória · judicial · extrajudicial</div></header><main>'''

@app.get('/', response_class=HTMLResponse)
def dashboard(city:str='', max_price:str='', lim:str=''):
    db=SessionLocal()
    try:
        q=db.query(Property).filter(Property.status=='active')
        if city in ('Guarapari','Vitória'): q=q.filter(Property.city==city)
        if max_price:
            try:q=q.filter(Property.current_bid<=float(max_price))
            except:pass
        if lim=='no': q=q.filter(Property.leilao_imovel_status=='no')
        props=q.order_by(desc(Property.last_seen)).all()
        total=db.query(Property).filter(Property.status=='active').count()
        outside=db.query(Property).filter(Property.status=='active',Property.leilao_imovel_status=='no').count()
        today=datetime.utcnow().replace(hour=0,minute=0,second=0,microsecond=0)
        new_count=db.query(Property).filter(Property.first_seen>=today).count()
        cards=f'''<div class="cards"><div class="card"><b>{total}</b><br>ativos</div><div class="card"><b>{new_count}</b><br>novos hoje</div><div class="card"><b>{outside}</b><br>fora do Leilão Imóvel</div></div>'''
        filters=f'''<form class="filters"><select name="city"><option value="">Todas as cidades</option><option {'selected' if city=='Guarapari' else ''}>Guarapari</option><option {'selected' if city=='Vitória' else ''}>Vitória</option></select><input name="max_price" placeholder="Lance máximo" value="{max_price}"><select name="lim"><option value="">Todos</option><option value="no" {'selected' if lim=='no' else ''}>Só fora do Leilão Imóvel</option></select><button>Filtrar</button><a href="/" style="padding:9px">Limpar</a></form>'''
        rows=[]
        for p in props:
            cls='yes' if p.leilao_imovel_status=='yes' else 'no' if p.leilao_imovel_status=='no' else 'inst' if 'Caixa' in p.source or 'Banco' in p.source else ''
            price=f'R$ {p.current_bid:,.2f}'.replace(',','X').replace('.',',').replace('X','.') if p.current_bid is not None else '—'
            appraise=f'R$ {p.appraisal:,.2f}'.replace(',','X').replace('.',',').replace('X','.') if p.appraisal is not None else '—'
            rows.append(f'''<article class="item {cls}"><h3>{p.title}</h3><div>{p.city} · {p.neighborhood or '—'} · {p.area_m2 or '—'} m²</div><p class="price">{price}</p><div>Avaliação: {appraise} · {p.auction_type or '—'} · {p.auction_date or '—'}</div><div class="muted">Fonte: {p.source} · Leilão Imóvel: {p.leilao_imovel_status}</div>{('<div>Leiloeiro oficial: '+p.official_auctioneer+'</div>') if p.official_auctioneer else ''}{('<p><a href="'+p.source_url+'" target="_blank">Abrir fonte</a></p>') if p.source_url else ''}</article>''')
        body=HTML_HEAD+cards+filters+'<div class="grid">'+''.join(rows or ['<div class="item">Nenhum imóvel encontrado.</div>'])+'</div></main></body></html>'
        return body
    finally: db.close()


def demo_collect():
    # Starter data only. Real collectors will replace this list in the next phase.
    return [
      dict(title='Apartamento — Praia do Morro',city='Guarapari',state='ES',neighborhood='Praia do Morro',address='Rua Mônaco',area_m2=97.86,current_bid=199232.47,appraisal=492092.26,auction_date='09/09/2026',auction_type='2ª praça',source='Leilão Imóvel (demo)',source_url='',official_auctioneer='Picelli Leilões',leilao_imovel_status='yes'),
      dict(title='Apartamento — Jardim Camburi',city='Vitória',state='ES',neighborhood='Jardim Camburi',area_m2=55,current_bid=251203.26,appraisal=502406.52,auction_date='04/11/2026',auction_type='2ª praça',source='Leilão Imóvel (demo)',source_url='',official_auctioneer='',leilao_imovel_status='yes'),
    ]

def run_radar():
    db=SessionLocal(); found=0; new=0; updated=0
    try:
        for item in demo_collect():
            found+=1
            fp='|'.join(str(item.get(k,'')) for k in ('city','address','title','area_m2'))
            p=db.query(Property).filter(Property.fingerprint==fp).first()
            now=datetime.utcnow()
            if not p:
                p=Property(**item,fingerprint=fp,first_seen=now,last_seen=now,status='active'); db.add(p); new+=1
            else:
                for k,v in item.items(): setattr(p,k,v)
                p.last_seen=now; p.status='active'; updated+=1
        db.commit()
        return {'found':found,'new':new,'updated':updated,'errors':0}
    finally: db.close()

@app.post('/run')
def run_now(token:str=''):
    if RUN_TOKEN and token!=RUN_TOKEN: raise HTTPException(status_code=403,detail='Invalid token')
    return JSONResponse(run_radar())

@app.get('/health')
def health(): return {'ok':True,'service':'radar-leiloes','version':'2.1'}
