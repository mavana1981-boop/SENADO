# Painel do Senado — Liderança da Oposição

App Flask que replica, para o Senado Federal, o painel de governismo × oposicionismo da Câmara.
**Etapa 1:** abas **Orientações** e **Rankings** prontas. Perfil, Proposições, Cota e Votações vêm nas próximas etapas.

## Fonte dos dados
API de Dados Abertos Legislativos do Senado (pública, sem chave):
- `/plenario/votacao/orientacaoBancada/{inicio}/{fim}.json` — votações nominais do Plenário, voto de cada senador e orientação de cada liderança (partidos, **Governo**, **Oposição**).
- `/senador/lista/legislatura/57.json` e `/senador/lista/atual.json` — cadastro, foto, partido e UF.

## Deploy no Railway (≈10 min)
1. Suba esta pasta para um repositório no GitHub.
2. No Railway: **New Project → Deploy from GitHub repo**.
3. No mesmo projeto: **+ New → Database → PostgreSQL**. No serviço do app, em *Variables*, adicione
   `DATABASE_URL` = `${{ Postgres.DATABASE_URL }}`.
4. Adicione também `COLETA_TOKEN` = uma senha qualquer (protege a rota de coleta manual).
5. Faça o deploy. **No primeiro boot, o app detecta o banco vazio e baixa toda a legislatura (fev/2023 → hoje)
   em segundo plano** — leva alguns minutos; a página já abre e vai se preenchendo.

### Atualização automática (diária)
Crie um segundo serviço no mesmo projeto apontando para o mesmo repositório, com:
- *Start command*: `python collector.py`
- *Cron schedule*: `17 9 * * 1-6` (UTC → 6h17 de Brasília, seg–sáb)
- a mesma variável `DATABASE_URL`.

Ele busca os últimos 45 dias e regrava (idempotente). Alternativa sem segundo serviço:
`POST https://SEU-APP.up.railway.app/api/coletar?token=SEU_TOKEN` (aceita `&desde=2023-02-01` para recarga total).

## Rodar localmente
```bash
pip install -r requirements.txt
python collector.py --desde 2023-02-01   # cria senado.db (SQLite) com tudo
flask --app app run
```

## Testes
```bash
python tests/test_pipeline.py
```
Usam votações reais de 12/12/2023 (PL 3.626/2023) no formato exato da API. Conferem o placar oficial (45 × 23),
as orientações de Governo/Oposição, a normalização de partidos e o cruzamento de nomes
(“Astr. Marcos Pontes” → Astronauta Marcos Pontes, “Prof. Dorinha Seabra”, etc.).
Rodam em SQLite e em PostgreSQL (`TEST_DATABASE_URL=postgresql://...`).

## Regras de cálculo
- **Governismo/Oposicionismo** = votos SIM/NÃO iguais à orientação ÷ votos SIM/NÃO do senador nas votações em que a
  liderança orientou SIM ou NÃO. Orientação “livre”, abstenção, obstrução e voto secreto ficam fora.
- **Só orientações opostas** (padrão ligado): considera apenas votações em que Governo e Oposição orientaram em
  sentidos contrários — o recorte que distingue governistas de opositores. Desmarque para ver tudo.
- **Por partido / por estado**: usa o partido do senador **na data do voto** (quem trocou de legenda conta no partido antigo
  para votos antigos). As tabelas de senadores mostram o partido **atual**.
- **Participação**: votações em que registrou voto (inclui secretas) ÷ votações realizadas entre o primeiro e o último voto
  do senador no período — suplentes não são penalizados pelo tempo fora.
- **Fidelidade partidária**: votos iguais à orientação do próprio partido na data da votação.
- Selo “fora”: senador que não está em exercício hoje (licenciado, suplente que devolveu a vaga, etc.).

## Estrutura
```
app.py        rotas Flask
collector.py  coleta da API → banco
metrics.py    cálculos (SQL compatível com SQLite e PostgreSQL)
db.py         tabelas e normalização de nomes/partidos/votos
templates/ static/ tests/
```
