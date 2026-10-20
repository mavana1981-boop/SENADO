# Painel do Senado — Liderança da Oposição

App Flask (com login) que replica, para o Senado Federal, o painel da Câmara: **Perfil, Proposições, Cota,
Votações, Orientações e Rankings**, com identidade visual no azul do Senado.

## Deploy no Railway

1. Suba esta pasta para um repositório no GitHub (privado).
2. No Railway: **New Project → Deploy from GitHub repo**.
3. No mesmo projeto: **+ New → Database → PostgreSQL**.
4. No serviço do app, em **Variables**, crie:

| Variável | Valor |
|---|---|
| `DATABASE_URL` | `${{ Postgres.DATABASE_URL }}` |
| `SECRET_KEY` | uma sequência longa e aleatória (assina as sessões de login) |
| `ADMIN_USER` | seu usuário de administrador, ex.: `marcelo` |
| `ADMIN_PASSWORD` | senha forte (mín. 8 caracteres) |
| `ADMIN_NOME` | opcional, nome exibido no topo |
| `COLETA_TOKEN` | opcional, senha para disparar coleta manual pela URL |

5. Faça o deploy. No primeiro boot:
   - o administrador é criado a partir de `ADMIN_USER`/`ADMIN_PASSWORD`;
   - o app detecta o banco vazio e baixa **toda a legislatura em segundo plano** (votações desde fev/2023,
     CEAPS 2023→hoje, proposições e perfis de todos os senadores). Leva de 10 a 20 minutos; o site já abre
     e vai se preenchendo.
   - Se você já tinha publicado a versão anterior, ele baixa **só o que falta** (cota, proposições e perfis).

Depois do primeiro acesso, troque sua senha em **Conta** e crie os usuários da equipe em **Usuários**
(perfil “Leitura” ou “Admin”). `ADMIN_PASSWORD` só é usada para criar o admin na primeira vez.

### Atualização diária
Crie um segundo serviço no mesmo projeto, apontando para o mesmo repositório:
- *Start command*: `python collector.py`
- *Cron schedule*: `17 9 * * 1-6` (UTC = 6h17 em Brasília, seg–sáb)
- variável `DATABASE_URL` igual à do app.

Ele atualiza: votações dos últimos 45 dias, CEAPS do ano (e do ano anterior até março), proposições e perfis.
Alternativa sem segundo serviço: `POST https://SEU-APP.up.railway.app/api/coletar?token=SEU_COLETA_TOKEN`
(com `&desde=2023-02-01` faz a recarga completa).

## Fontes de dados (todas públicas, sem chave)
| Aba | Serviço |
|---|---|
| Votações, Orientações, Rankings | `legis.senado.leg.br/dadosabertos/plenario/votacao/orientacaoBancada/{ini}/{fim}` |
| Senadores (foto, partido, UF) | `/senador/lista/legislatura/57` e `/senador/lista/atual` |
| Perfil | `/senador/{código}`, `/senador/{código}/comissoes`, `/senador/{código}/cargos` |
| Proposições | `/processo?codigoParlamentarAutor={código}` |
| Cota (CEAPS) | `adm.senado.gov.br/adm-dadosabertos/api/v1/senadores/despesas_ceaps/{ano}` (plano B: CSV da Transparência) |

## Regras de cálculo
- **Governismo/Oposicionismo**: votos SIM/NÃO iguais à orientação ÷ votos SIM/NÃO do senador quando a liderança
  orientou SIM ou NÃO. “Livre”, abstenção, obstrução e voto secreto ficam fora. Filtro **só orientações opostas**
  (padrão) usa apenas votações em que Governo e Oposição orientaram em sentidos contrários.
- **Por partido / por estado / aba Votações**: partido **na data do voto**. Listas de senadores: partido **atual**.
- **Participação**: votações em que registrou voto (inclui secretas) ÷ votações realizadas entre o primeiro e o
  último voto do senador no período.
- **Fidelidade**: votos iguais à orientação do próprio partido.
- **Proposições**: autoria e coautoria; “1º autor” = o senador aparece primeiro na autoria. “Virou norma” = situação
  “Transformada em norma jurídica”.
- **Cota**: teto mensal por UF (valores de fev/2023 publicados no Boletim Administrativo, com reajustes de 6% em
  fev/2024 e 6,13% em fev/2025 — `cota.py`). Cota disponível = teto × meses no cargo no ano (estimados pela atividade
  registrada). **Se sair novo reajuste**, defina `CEAPS_REAJUSTES=2026-02-01:0.05` (data:percentual) no Railway.

## Segurança
Senhas com hash scrypt; sessão assinada (cookie HttpOnly, Secure no Railway); proteção CSRF em todos os formulários;
bloqueio de 15 min após 5 tentativas de login erradas; desativar um usuário ou trocar a senha derruba as sessões dele.

## Rodar localmente
```bash
pip install -r requirements.txt
export ADMIN_USER=admin ADMIN_PASSWORD=troque-esta-senha SECRET_KEY=dev
python collector.py --desde 2023-02-01   # cria senado.db (SQLite) com tudo
flask --app app run
```

## Testes
```bash
python tests/test_pipeline.py                       # SQLite
TEST_DATABASE_URL=postgresql://... python tests/test_pipeline.py   # PostgreSQL
```
Cobrem: votação real do PL 3.626/2023 (placar 45 × 23), orientações, cruzamento de nomes, cálculos das seis abas,
tetos da cota, leitura do JSON e do CSV da CEAPS, login, CSRF, permissões e gestão de usuários.
Algumas fixtures são respostas reais da API copiadas do projeto senado-br-mcp (MIT — ver `tests/fixtures/LICENSE-senado-br-mcp.txt`).

## Estrutura
```
app.py        rotas
auth.py       login, sessões, usuários
collector.py  coleta (votações, CEAPS, proposições, perfis)
metrics.py    cálculos de votação
paineis.py    Perfil, Proposições, Cota e rankings correlatos
cota.py       tetos da CEAPS por UF
db.py         tabelas e normalização
templates/ static/ tests/
```
