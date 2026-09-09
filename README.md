# Radar Nacional de Leilões — Guarapari e Vitória

O radar filtra pela **localização do imóvel**, não pela localização do leiloeiro.

## Fontes
- Cadastro nacional de leiloeiros oficiais: FENAJU/Juntas Comerciais.
- Todos os sites oficiais descobertos no cadastro são armazenados na tabela `sources` e tentados nas coletas.
- Agregadores/institucionais: Leilão Imóvel (via Apify), LeilôAI, Núcleo, INNLEI, Central de Leilões, Caixa e Comprei/PGFN.

## Regras
- Só entram imóveis em Guarapari/ES ou Vitória/ES.
- Só entram lotes com data futura identificável.
- Deduplicação global.
- Falha temporária de um site não apaga o cadastro de fontes.
- Sites que retornam 403/timeout ficam registrados como erro para diagnóstico.

## Render
Variáveis recomendadas:
- `APIFY_TOKEN`: token da conta Apify, necessário apenas para Leilão Imóvel.
- `RADAR_RUN_TOKEN`: opcional, protege `/run`.
- `DATABASE_URL`: opcional; SQLite funciona para teste, Postgres é recomendado para persistência.

O endpoint `/sources` mostra o cadastro de sites oficiais e o estado da última tentativa.
