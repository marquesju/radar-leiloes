import os, hashlib
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

DB_URL=os.getenv('DATABASE_URL','sqlite:///./radar.db')
if DB_URL.startswith('postgres://'): DB_URL=DB_URL.replace('postgres://','postgresql://',1)
connect_args={'check_same_thread':False} if DB_URL.startswith('sqlite') else {}
engine=create_engine(DB_URL, connect_args=connect_args)
Session=sessionmaker(bind=engine)
Base=declarative_base()

class Listing(Base):
    __tablename__='listings'
    id=Column(Integer,primary_key=True)
    key=Column(String(64),unique=True,index=True)
    city=Column(String(80)); neighborhood=Column(String(120)); address=Column(String(255))
    area=Column(Float); bedrooms=Column(Integer); price=Column(Float); appraisal=Column(Float)
    auction_date=Column(String(40)); auctioneer=Column(String(180)); source=Column(String(255))
    leilao_imovel=Column(String(10)); status=Column(String(30),default='open')
    created_at=Column(DateTime,default=datetime.utcnow); updated_at=Column(DateTime,default=datetime.utcnow)

Base.metadata.create_all(engine)
app=FastAPI(title='Radar de Leilões — Guarapari e Vitória')

DEMO=[
 dict(city='Guarapari',neighborhood='Praia do Morro',address='Rua Mônaco',area=97.86,bedrooms=3,price=199232.47,appraisal=492092.26,auction_date='09/09/2026',auctioneer='Picelli Leilões',source='Leilão Imóvel / fonte oficial a confirmar',leilao_imovel='yes'),
 dict(city='Vitória',neighborhood='Jardim Camburi',address='Jardim Camburi',area=55,bedrooms=2,price=251203.26,appraisal=502406.52,auction_date='04/11/2026',auctioneer='A confirmar',source='Leilão Imóvel / fonte oficial a confirmar',leilao_imovel='yes')]

def run_radar():
    db=Session(); new=0
    for x in DEMO:
        key=hashlib.sha256((x['city']+'|'+x['address']+'|'+x['auction_date']).encode()).hexdigest()
        obj=db.query(Listing).filter_by(key=key).first()
        if not obj:
            obj=Listing(key=key,**x); db.add(obj); new+=1
        else:
            for k,v in x.items(): setattr(obj,k,v)
            obj.updated_at=datetime.utcnow()
    db.commit(); total=db.query(Listing).count(); db.close()
    return {'found':len(DEMO),'new':new,'total':total}

def money(v): return f'R$ {v:,.2f}'.replace(',','X').replace('.',',').replace('X','.') if v is not None else '—'

@app.get('/health')
def health(): return {'ok':True,'service':'radar-leiloes'}

@app.get('/api/run')
def api_run(token:str=''):
    expected=os.getenv('RADAR_TOKEN','')
    if expected and token!=expected: return JSONResponse({'error':'unauthorized'},status_code=401)
    return run_radar()

@app.get('/',response_class=HTMLResponse)
def home(request:Request):
    run_radar(); db=Session(); rows=db.query(Listing).order_by(Listing.updated_at.desc()).all(); db.close()
    cards=''
    for r in rows:
        cls='green' if r.leilao_imovel=='yes' else 'orange' if r.leilao_imovel=='no' else 'blue'
        cards+=f'''<article class="card {cls}"><div class="top"><span>{r.city}</span><b>{'🟢 Leilão Imóvel' if cls=='green' else '🟠 Fora do Leilão Imóvel' if cls=='orange' else '🔵 Institucional'}</b></div><h2>{r.neighborhood or ''}</h2><p>{r.address or ''}</p><div class="grid"><div><small>Lance</small><strong>{money(r.price)}</strong></div><div><small>Avaliação</small><strong>{money(r.appraisal)}</strong></div><div><small>Área</small><strong>{r.area or '—'} m²</strong></div><div><small>Leilão</small><strong>{r.auction_date or '—'}</strong></div></div><p class="meta">Leiloeiro: {r.auctioneer or '—'}<br>Fonte: {r.source or '—'}</p></article>'''
    return f'''<!doctype html><html lang="pt-BR"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Radar de Leilões</title><style>body{{font-family:system-ui;margin:0;background:#f5f6f8;color:#17202a}}header{{background:#17202a;color:white;padding:22px 18px;position:sticky;top:0}}main{{max-width:900px;margin:auto;padding:18px}}.card{{background:white;border-radius:16px;padding:18px;margin:14px 0;border-left:7px solid #999;box-shadow:0 2px 10px #0001}}.green{{border-color:#22a447}}.orange{{border-color:#f08a24}}.blue{{border-color:#2d78c8}}.top{{display:flex;justify-content:space-between;gap:10px;font-size:13px}}h1{{margin:0 0 5px}}h2{{margin:14px 0 2px;font-size:20px}}p{{margin:5px 0;color:#58636f}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}}small{{display:block;color:#7b8794}}strong{{font-size:16px}}.meta{{font-size:12px;margin-top:16px}}button{{padding:10px 14px;border:0;border-radius:9px;cursor:pointer}}</style><header><h1>Radar de Leilões</h1><div>Guarapari + Vitória</div></header><main><button onclick="location.reload()">↻ Atualizar radar</button>{cards}<p style="text-align:center;font-size:12px;margin:30px 0">Versão inicial — os dados exibidos ainda são DEMO.</p></main></html>'''
