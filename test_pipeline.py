"""Testes com dados reais do Senado (sessão de 12/12/2023, PL 3.626/2023) no formato exato da API.
Fixtures ceaps_adm.json, senador_detalhe/comissoes/cargos.json: respostas reais da API, copiadas do
projeto senado-br-mcp (licença MIT, ver LICENSE-senado-br-mcp.txt). processos_5894.json e
ceaps_sintetico.json seguem o formato real dos serviços, com conteúdo montado para o teste.

Rodar:  python -m pytest -q tests   (ou: python tests/test_pipeline.py)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_tmp}/teste.db")
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "senha-de-teste-123"
os.environ["SECRET_KEY"] = "teste"
os.environ.pop("RAILWAY_ENVIRONMENT", None)
sys.path.insert(0, str(RAIZ))

import collector  # noqa: E402
import metrics  # noqa: E402
from db import engine, init_db  # noqa: E402
from sqlalchemy import text  # noqa: E402


def _carregar():
    init_db()
    docs = (json.loads((FIX / "senadores_legislatura.json").read_text()),
            json.loads((FIX / "senadores_atual.json").read_text()))
    with engine.begin() as c:
        collector.coletar_senadores(conn=c, docs=docs)
        collector.gravar_votacoes(json.loads((FIX / "orientacao_202312.json").read_text()), c)
        collector.vincular_senadores(c)
        ceaps = json.loads((FIX / "ceaps_adm.json").read_text()) + \
            json.loads((FIX / "ceaps_sintetico.json").read_text())
        collector.gravar_ceaps(c, 2025, collector.parse_ceaps_adm(ceaps))
        collector.gravar_processos(c, 5894, "flavio bolsonaro",
                                   json.loads((FIX / "processos_5894.json").read_text()))
        collector.gravar_perfil(c, 5672, *(json.loads((FIX / f).read_text()) for f in
                                          ("senador_detalhe.json", "senador_comissoes.json",
                                           "senador_cargos.json")))
        collector.marcar_coleta(c)


_carregar()


def _cliente():
    import app as painel
    cli = painel.app.test_client()
    html = cli.get("/login").get_data(as_text=True)
    import re
    tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    r = cli.post("/login", data={"_csrf": tok, "login": "admin", "senha": "senha-de-teste-123"})
    assert r.status_code == 302, "login falhou"
    return cli


def test_votacao_real_contagens():
    with engine.connect() as c:
        sim, nao = c.execute(text(
            "SELECT SUM(CASE WHEN voto='SIM' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN voto='NÃO' THEN 1 ELSE 0 END) FROM votos WHERE votacao_id=9534")).one()
        assert (sim, nao) == (45, 23)  # bate com o placar oficial da Emenda 147
        assert c.execute(text("SELECT secreta FROM votacoes WHERE id=9557")).scalar() in (1, True)
        assert c.execute(text("SELECT secreta FROM votacoes WHERE id=9534")).scalar() in (0, False)


def test_orientacoes_normalizadas():
    with engine.connect() as c:
        oris = dict(c.execute(text(
            "SELECT lideranca, voto FROM orientacoes WHERE votacao_id=9534")).all())
    assert oris["Governo"] == "NÃO" and oris["Oposição"] == "SIM"
    assert oris["REPUBLICANOS"] == "SIM"   # API manda "Republica"
    assert oris["PODE"] == "LIVRE"         # API manda "Podemos"


def test_cruzamento_de_nomes():
    with engine.connect() as c:
        m = dict(c.execute(text(
            "SELECT nome, codigo_senador FROM votos WHERE votacao_id=9534")).all())
    assert m["Astr. Marcos Pontes"] == 6009          # abreviação
    assert m["Prof. Dorinha Seabra"] == 5386         # abreviação
    assert m["Ivete da Silveira"] == 6010            # espaço sobrando na API
    assert m["Rogério Marinho"] == 4694              # acento divergente no cadastro
    assert m["Rodrigo Pacheco"] == 5732              # saiu do exercício, mas está na legislatura


def test_governismo_oposicionismo():
    f = metrics.Filtros(opostas=True)
    gov = {s["nome"]: s for s in metrics.alinhamento("Governo", f)}
    opo = {s["nome"]: s for s in metrics.alinhamento("Oposição", f)}
    assert gov["Humberto Costa"]["pct"] == 100.0
    assert opo["Flávio Bolsonaro"]["pct"] == 100.0
    assert opo["Romário"]["pct"] == 0.0              # votou NÃO, contra a Oposição
    assert gov["Astronauta Marcos Pontes"]["partido"] == "PL"  # nome e partido do cadastro
    # votação 9581 não tem orientação de Governo/Oposição e não pode entrar na conta
    assert opo["Flávio Bolsonaro"]["total"] == 1
    r = metrics.resumo(f)
    assert r["opostas"] == 1 and r["secretas"] == 1 and r["votacoes"] == 3


def test_filtros_partido_uf():
    f = metrics.Filtros(partido="PL", uf="RJ")
    nomes = {s["nome"] for s in metrics.alinhamento("Oposição", f)}
    assert {"Flávio Bolsonaro", "Carlos Portinho"} <= nomes
    assert "Romário" not in nomes  # hoje sem partido


def test_fidelidade_e_participacao():
    fid = {s["nome"]: s for s in metrics.fidelidade(metrics.Filtros(opostas=False))}
    assert fid["Damares Alves"]["seguiu"] == 1        # Republicanos orientou SIM em 9534
    assert fid["Romário"]["pct"] == 0.0               # PL orientou SIM, votou NÃO
    part = {s["nome"]: s for s in metrics.participacao(metrics.Filtros())}
    assert part["Damares Alves"]["votou"] == 3 and part["Damares Alves"]["pct"] == 100.0


def test_paginas_renderizam():
    cli = _cliente()
    for url in ["/orientacoes", "/orientacoes?opostas=0", "/orientacoes?partido=PL&opostas=0&opostas=1",
                "/rankings", "/rankings?uf=RJ", "/perfil", "/saude"]:
        r = cli.get(url)
        assert r.status_code == 200, url
    html = cli.get("/orientacoes").get_data(as_text=True)
    assert "Flávio Bolsonaro" in html and "Emenda nº 147" in html
    assert _anonimo().post("/api/coletar").status_code == 403


def test_aba_votacoes():
    d = metrics.detalhe_votacao(9534)
    assert d["contagem"]["SIM"] == 45 and d["contagem"]["NÃO"] == 23
    assert d["gov"] == "NÃO" and d["opo"] == "SIM"
    assert d["orientacoes"][0]["lideranca"] == "Governo"       # institucionais primeiro
    pl = next(p for p in d["placar"] if p["partido"] == "PL")
    assert pl["orientacao"] == "SIM" and pl["NÃO"] == 1         # Romário
    nomes = {r["nome"] for r in d["traidores"]}
    assert "Romário" in nomes and "Flávio Bolsonaro" not in nomes
    sec = metrics.detalhe_votacao(9557)
    assert sec["votacao"]["secreta"] and sec["traidores"] == []
    assert sec["contagem"]["SIM"] == 41                         # placar oficial da secreta
    achou = metrics.lista_votacoes(metrics.Filtros(), "PL 3626")
    assert {v["id"] for v in achou} >= {9534}
    assert metrics.lista_votacoes(metrics.Filtros(), "inexistentexyz") == []
    cli = _cliente()
    html = cli.get("/votacoes").get_data(as_text=True)
    assert "Emenda nº 147" in html and "Votos nominais" in html   # padrão = mais recente não secreta
    assert cli.get("/votacoes?id=9557").status_code == 200
    assert cli.get("/votacoes?busca=3626/2023").status_code == 200
    assert cli.get("/votacoes?id=999999").status_code == 200


def _anonimo():
    import app as painel
    return painel.app.test_client()


def test_login_obrigatorio_e_seguranca():
    anon = _anonimo()
    for url in ["/", "/orientacoes", "/perfil", "/cota", "/usuarios"]:
        r = anon.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"], url
    assert anon.get("/saude").status_code == 200
    assert anon.get("/static/style.css").status_code == 200
    # senha errada não entra; post sem CSRF é rejeitado
    r = anon.post("/login", data={"login": "admin", "senha": "senha-de-teste-123"})
    assert r.status_code == 200 and "novamente" in r.get_data(as_text=True)   # sem CSRF não entra
    assert anon.get("/orientacoes").status_code == 302                     # continua deslogado
    html = anon.get("/login").get_data(as_text=True)
    assert "plenario.svg" in html and "Painel do Senado" in html
    import re
    tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    r = anon.post("/login", data={"_csrf": tok, "login": "admin", "senha": "errada"})
    assert "inválidos" in r.get_data(as_text=True)
    # redirecionamento só para caminhos internos
    r = anon.post("/login", data={"_csrf": tok, "login": "admin", "senha": "senha-de-teste-123",
                                  "proximo": "https://site-malicioso.com"})
    assert r.headers["Location"].endswith("/") or "orientacoes" in r.headers["Location"]


def test_gestao_de_usuarios():
    import re
    cli = _cliente()
    html = cli.get("/usuarios").get_data(as_text=True)
    tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    cli.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": "Assessor",
                                "nome": "Assessor", "senha": "outra-senha-1"})
    assert "assessor" in cli.get("/usuarios").get_data(as_text=True)
    # o novo usuário (sem admin) entra, mas não acessa a gestão
    c2 = _anonimo()
    t2 = re.search(r'name="_csrf" value="([^"]+)"', c2.get("/login").get_data(as_text=True)).group(1)
    assert c2.post("/login", data={"_csrf": t2, "login": "assessor", "senha": "outra-senha-1"}).status_code == 302
    assert c2.get("/orientacoes").status_code == 200
    assert c2.get("/usuarios").status_code == 403
    # desativado perde a sessão na hora
    from db import usuarios as tu
    with engine.connect() as c:
        uid = c.execute(text("SELECT id FROM usuarios WHERE login='assessor'")).scalar()
    cli.post("/usuarios", data={"_csrf": tok, "acao": "alternar", "id": uid})
    assert c2.get("/orientacoes").status_code == 302
    assert tu is not None


def test_ceaps_e_cota():
    import paineis
    import cota
    assert cota.teto_mensal("DF", 2023, 6) == 22307.91
    assert cota.teto_mensal("DF", 2024, 6) == round(22307.91 * 1.06, 2)
    assert cota.teto_mensal("AM", 2025, 3) == round(46933.20 * 1.06 * 1.0613, 2)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM despesas WHERE codigo_senador=5973")).scalar() == 3
    d = paineis.cota_senador(5894)
    assert d["total"]["gasto"] == 35000.5
    r25 = d["resumo"][0]
    assert r25["ano"] == 2025 and r25["meses"] == 12          # em exercício: ano inteiro
    assert r25["economia"] == round(r25["disponivel"] - 35000.5, 2)
    assert d["categorias"][0]["tipo"] == "Divulgação da atividade parlamentar"
    rk = paineis.ranking_economia()
    assert any(x["codigo"] == 5894 for x in rk)
    # plano B: CSV da Transparência
    csv_txt = ('"ULTIMA ATUALIZACAO";"01/10/2025"\n'
               'ANO;MES;SENADOR;TIPO_DESPESA;CNPJ_CPF;FORNECEDOR;DOCUMENTO;DATA;DETALHAMENTO;VALOR_REEMBOLSADO;COD_DOCUMENTO\n'
               '2025;3;FLÁVIO BOLSONARO;Aluguel;00.000.000/0001-00;X LTDA;12;15/03/2025;;1.234,56;2200001\n')
    linhas = collector.parse_ceaps_csv(csv_txt, 2025, {"flavio bolsonaro": 5894})
    assert linhas[0]["valor"] == 1234.56 and linhas[0]["codigo_senador"] == 5894
    assert linhas[0]["data"] == "2025-03-15" and linhas[0]["id"] == 2200001


def test_proposicoes():
    import paineis
    p = paineis.proposicoes(5894)
    assert p["total"] == 4 and p["principal"] == 3        # PEC 1/2025 é do Izalci (coautoria)
    assert p["normas"] == 1 and p["tramitando"] == 2
    assert p["projetos"] == 3                              # PDL, PEC, PL (REQ não conta)
    assert paineis.proposicoes(5894, papel="coautor")["total"] == 1
    assert paineis.proposicoes(5894, situacao="norma")["itens"][0]["identificacao"] == "PL 2100/2023"
    assert paineis.proposicoes(5894, busca="decreto")["total"] == 1
    rk = paineis.ranking_proposicoes()
    assert rk[0]["codigo"] == 5894 and rk[0]["n"] == 2     # PDL + PL como 1º autor


def test_perfil():
    import paineis
    pf = paineis.perfil(5672)
    assert pf["basicos"]["naturalidade"] == "Rio Branco"
    assert pf["basicos"]["email"] == "sen.alanrick@senado.leg.br"
    siglas = {c["sigla"] for c in pf["comissoes"]}
    assert "GPARGENTINA" in siglas and "GPBOLIVIA" not in siglas   # encerradas ficam fora
    assert {c["sigla"] for c in pf["cargos"]} == {"FPE", "GPISRAEL"}  # presidência da CRA terminou
    ind = paineis.indicadores_senador(5894)
    assert ind["opo"] == 100.0 and ind["participacao"] is not None


def test_votos_oposicao_em_orientacoes():
    import paineis
    # Flávio Bolsonaro seguiu a Oposição; a contagem bate com o % do indicador
    vo = paineis.votos_oposicao(5894, metrics.Filtros(opostas=True))
    ind = paineis.indicadores_senador(5894)
    assert len(vo["seguiu"]) == 1 and vo["contra"] == [] and vo["total"] == ind["opo_n"]
    v = vo["seguiu"][0]
    assert v["voto"] == v["opo"] and v["gov"] in ("SIM", "NÃO") and v["gov"] != v["opo"]
    # Romário votou contra a Oposição
    cod_rom = next(s["codigo"] for s in paineis.lista_senadores() if s["nome"] == "Romário")
    vr = paineis.votos_oposicao(cod_rom, metrics.Filtros(opostas=True))
    assert vr["seguiu"] == [] and len(vr["contra"]) == 1
    # sem o filtro de orientações opostas nunca há menos votações
    assert paineis.votos_oposicao(5894, metrics.Filtros(opostas=False))["total"] >= vo["total"]
    # filtro de período que exclui a sessão zera o quadro
    from datetime import date
    assert paineis.votos_oposicao(5894, metrics.Filtros(inicio=date(2024, 1, 1)))["total"] == 0
    cli = _cliente()
    html = cli.get("/orientacoes?senador=5894").get_data(as_text=True)
    assert "Favoráveis à Oposição" in html and "Contrários à Oposição" in html
    assert "PL 3626/2023" in html and 'id="votos-senador"' in html
    assert "senador=5894" in html                       # nomes das listas viram links
    html = cli.get(f"/orientacoes?senador={cod_rom}&opostas=0").get_data(as_text=True)
    assert "Contrários à Oposição" in html and 'name="opostas" value="0"' in html
    # o quadro saiu do perfil
    assert "Favoráveis à Oposição" not in cli.get("/perfil?senador=5894").get_data(as_text=True)


def test_abas_novas_renderizam():
    cli = _cliente()
    for url in ["/perfil?senador=5672", "/perfil?senador=5894", "/proposicoes?senador=5894",
                "/proposicoes?senador=5894&papel=autor&situacao=norma", "/cota?senador=5894",
                "/cota?senador=5894&ano=2025", "/cota?senador=5973", "/rankings", "/conta", "/usuarios",
                "/perfil?senador=999999"]:
        r = cli.get(url)
        assert r.status_code == 200, url
    html = cli.get("/cota?senador=5894").get_data(as_text=True)
    assert "IMOBILIARIA X LTDA" in html and "R$ 15.000,00" in html
    html = cli.get("/proposicoes?senador=5894").get_data(as_text=True)
    assert "PL 2100/2023" in html and "TRANSFORMADA" in html
    html = cli.get("/perfil?senador=5672").get_data(as_text=True)
    assert "Rio Branco" in html
    assert cli.get("/inexistente").status_code == 404


def test_coleta_recupera_meses_que_falharam():
    """Reproduz o problema real: o Senado devolve 503 em maio/2026 e a coleta não pode parar ali."""
    from datetime import date as d
    import copy
    base = json.loads((FIX / "orientacao_202312.json").read_text())["votacoes"][1]   # votação 9581

    def votacao(vid, quando):
        v = copy.deepcopy(base)
        v["codigoVotacaoSve"], v["dataInicioVotacao"] = vid, quando
        return v
    dados = {"202604": [votacao(990401, "2026-04-28T16:57:09")],
             "202605": [votacao(990501, "2026-05-20T16:52:11")],
             "202607": [votacao(990701, "2026-07-15T18:00:00")],
             "202610": [votacao(991001, "2026-10-14T17:00:00")]}
    falhar = {"202605"}

    def fake(caminho):
        mes = caminho.split("orientacaoBancada/")[1][:6]
        if mes in falhar:
            raise RuntimeError("HTTP 503")
        return {"votacoes": dados.get(mes, [])}
    original = collector.get_json
    collector.get_json = fake
    try:
        collector.coletar_votacoes(d(2026, 4, 1), d(2026, 10, 20))
        with engine.connect() as c:
            ids = {r[0] for r in c.execute(text("SELECT id FROM votacoes WHERE id >= 990000"))}
        assert ids == {990401, 990701, 991001}              # maio falhou, mas os meses seguintes vieram
        assert "2026-05-01" in collector._meses_pendentes()
        # coleta diária seguinte (só últimos dias) também refaz o mês pendente
        falhar.clear()
        collector.coletar_votacoes(d(2026, 10, 1), d(2026, 10, 20))
        with engine.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM votacoes WHERE id = 990501")).scalar() == 1
        assert collector._meses_pendentes() == set()
        # a janela diária começa antes da última votação do banco (fecha buracos antigos)
        assert collector.inicio_atualizacao_votacoes(d(2026, 10, 20)) <= d(2026, 9, 14)
    finally:
        collector.get_json = original
        with engine.begin() as c:
            for t in ("votos", "orientacoes"):
                c.execute(text(f"DELETE FROM {t} WHERE votacao_id >= 990000"))
            c.execute(text("DELETE FROM votacoes WHERE id >= 990000"))


def test_agendador_diario():
    from datetime import datetime, timedelta, timezone
    import app as painel
    brt = timezone(timedelta(hours=-3))
    manha = datetime(2026, 10, 21, 7, 0, tzinfo=brt)
    assert painel.precisa_atualizar(manha, "2026-10-20T09:00:00")        # coleta de ontem
    assert not painel.precisa_atualizar(manha, "2026-10-21T09:30:00")    # já rodou hoje (UTC)
    assert not painel.precisa_atualizar(datetime(2026, 10, 21, 4, 0, tzinfo=brt), "2026-10-19T09:00:00")
    assert painel.precisa_atualizar(manha, None) is not None


if __name__ == "__main__":
    for nome, fn in list(globals().items()):
        if nome.startswith("test_"):
            fn()
            print("ok", nome)


def test_dias_parado_e_ordenacao():
    import paineis
    from datetime import date
    hoje = date(2026, 1, 1)
    assert paineis.dias_parado({"tramitando": True, "data_situacao": "2025-10-21"}, hoje) == 72
    assert paineis.dias_parado({"tramitando": True, "data_situacao": None,
                                "data_apresentacao": "2025-12-01"}, hoje) == 31
    assert paineis.dias_parado({"tramitando": False, "data_situacao": "2024-06-10"}, hoje) is None
    mais = [p["identificacao"] for p in paineis.proposicoes(5894, ordem="parado")["itens"]]
    menos = [p["identificacao"] for p in paineis.proposicoes(5894, ordem="parado_asc")["itens"]]
    # em tramitação primeiro: PEC 1/2025 (situação de 27/08/2025) está parada há mais tempo que o PDL
    assert mais[:2] == ["PEC 1/2025", "PDL 29/2025"] and menos[:2] == ["PDL 29/2025", "PEC 1/2025"]
    itens = paineis.proposicoes(5894, ordem="parado")["itens"]
    assert itens[0]["dias_parado"] >= itens[1]["dias_parado"] and itens[-1]["dias_parado"] is None
    cli = _cliente()
    html = cli.get("/proposicoes?senador=5894&ordem=parado").get_data(as_text=True)
    assert "Dias parado" in html and "▼" in html and "ordem=parado_asc" in html
    assert cli.get("/proposicoes?senador=5894&ordem=xyz").status_code == 200


def test_monitor_de_situacao_gera_notificacoes():
    import copy
    import paineis
    cli = _cliente()
    assert "Nenhuma mudança registrada ainda" in cli.get("/perfil?senador=5894").get_data(as_text=True)
    itens = json.loads((FIX / "processos_5894.json").read_text())
    # carga inicial (notificar=False) não gera aviso, mesmo com tudo "novo"
    with engine.begin() as c:
        collector.gravar_processos(c, 5894, "flavio bolsonaro", itens, notificar=False)
    assert paineis.notificacoes(5894)["itens"] == []
    # coleta do dia seguinte: PDL 29/2025 mudou de situação e apareceu um PL novo
    novos = copy.deepcopy(itens)
    novos[0]["situacaoAtual"] = "PRONTA PARA A PAUTA NA COMISSÃO"
    novos[0]["dataSituacaoAtual"] = "2026-10-22"
    novo = copy.deepcopy(itens[0])
    novo.update(id=8789999, identificacao="PL 9999/2026", situacaoAtual="AGUARDANDO LEITURA",
                dataApresentacao="2026-10-22", dataSituacaoAtual="2026-10-22")
    novos.append(novo)
    with engine.begin() as c:
        collector.gravar_processos(c, 5894, "flavio bolsonaro", novos, notificar=True)
        # a mesma coleta, repetida, não duplica avisos
        collector.gravar_processos(c, 5894, "flavio bolsonaro", novos, notificar=True)
    n = paineis.notificacoes(5894)
    tipos = {(x["tipo"], x["identificacao"]) for x in n["itens"]}
    assert tipos == {("situacao", "PDL 29/2025"), ("nova", "PL 9999/2026")}
    mud = next(x for x in n["itens"] if x["tipo"] == "situacao")
    assert mud["situacao_anterior"] == "AGUARDANDO DESIGNAÇÃO DO RELATOR"
    assert mud["situacao_nova"] == "PRONTA PARA A PAUTA NA COMISSÃO" and n["recentes"] == 2
    html = cli.get("/perfil?senador=5894").get_data(as_text=True)
    assert "PRONTA PARA A PAUTA NA COMISSÃO" in html and "PL 9999/2026" in html and "2 nos últimos 7 dias" in html
    # outro senador não recebe os avisos do Flávio
    assert paineis.notificacoes(5672)["itens"] == []
    # limpeza de antigas
    from datetime import date, timedelta
    with engine.begin() as c:
        collector.limpar_notificacoes(c, hoje=date.today() + timedelta(days=collector.DIAS_NOTIFICACOES + 1))
    assert paineis.notificacoes(5894)["itens"] == []


# ======================= Meu Parlamentar =======================

def _cliente_usuario(login, senha):
    import re
    import app as painel
    cli = painel.app.test_client()
    tok = re.search(r'name="_csrf" value="([^"]+)"', cli.get("/login").get_data(as_text=True)).group(1)
    r = cli.post("/login", data={"_csrf": tok, "login": login, "senha": senha})
    assert r.status_code == 302, "login falhou"
    return cli


def _tok(cli, url):
    import re
    return re.search(r'name="_csrf" value="([^"]+)"', cli.get(url).get_data(as_text=True)).group(1)


def test_meu_parlamentar_acesso_por_vinculo():
    adm = _cliente()
    tok = _tok(adm, "/usuarios")
    adm.post("/usuarios", data={"_csrf": tok, "acao": "criar", "login": "assessora",
                                "nome": "Assessora", "senha": "senha-segura-1"})
    from db import usuarios
    from sqlalchemy import select
    with engine.connect() as c:
        uid = c.execute(select(usuarios.c.id).where(usuarios.c.login == "assessora")).scalar()
    u = _cliente_usuario("assessora", "senha-segura-1")
    assert "Meu parlamentar" not in u.get("/orientacoes").get_data(as_text=True)
    assert u.get("/meu/").status_code == 403 and u.get("/meu/5894").status_code == 403
    # admin vincula a assessora ao Flávio
    r = adm.post("/usuarios", data={"_csrf": tok, "acao": "vincular", "id": uid, "senadores": ["5894"]})
    assert r.status_code == 302
    assert "1 senador(es)" in adm.get("/usuarios").get_data(as_text=True)
    assert "Meu parlamentar" in u.get("/orientacoes").get_data(as_text=True)
    r = u.get("/meu/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/meu/5894")
    assert u.get("/meu/5894").status_code == 200
    assert u.get("/meu/5672").status_code == 403          # não vinculado
    assert adm.get("/meu/5672").status_code == 200         # admin vê todos


def test_agenda_parse_e_filtro_por_comissao():
    import coleta_meu
    import meu
    from datetime import date, timedelta
    hoje = date.today()
    d1, d2 = (hoje + timedelta(days=1)).isoformat(), (hoje + timedelta(days=2)).isoformat()
    # formato novo (comissões) e antigo (Plenário), como devolvidos pela API
    doc_com = {"reunioes": [
        {"codigo": 111, "dataInicio": f"{d1}T09:00:00", "titulo": "3ª Reunião, Extraordinária",
         "colegiadoCriador": {"sigla": "GPARGENTINA", "nome": "Grupo Parlamentar Brasil-Argentina"},
         "local": "Ala Senador Alexandre Costa, Plenário nº 7", "situacao": "Agendada",
         "partes": [{"itens": [{"materia": {"identificacao": "PL 1/2025", "ementa": "Dispõe sobre X."},
                                "relator": "Senador Alan Rick", "dataPrevista": d1}]}]},
        {"codigo": 222, "dataInicio": f"{d1}T14:00:00", "titulo": "Reunião da CAE",
         "colegiadoCriador": {"sigla": "CAE"}}]}
    doc_plen = {"AgendaPlenario": {"Sessoes": {"Sessao": {
        "CodigoSessao": "9001", "DataSessao": d2, "HoraInicioSessao": "14:00",
        "TipoSessao": {"DescricaoTipoSessao": "Deliberativa Ordinária"},
        "Materias": {"Materia": [{"IdentificacaoMateria": {"DescricaoIdentificacaoMateria": "PEC 1/2025"},
                                  "EmentaMateria": "Altera a Constituição."}]}}}}}
    ev = coleta_meu.parse_agenda(doc_com) + coleta_meu.parse_agenda(doc_plen, plenario=True)
    assert len(ev) == 3
    gp = next(e for e in ev if e["colegiado"] == "GPARGENTINA")
    assert gp["hora"] == "09:00" and gp["data"] == d1 and "PL 1/2025" in gp["descricao"]
    plen = next(e for e in ev if e["colegiado"] == "PLEN")
    assert plen["hora"] == "14:00" and "Deliberativa" in plen["titulo"] and "PEC 1/2025" in plen["descricao"]
    with engine.begin() as c:
        coleta_meu.gravar_agenda_senado(c, ev, hoje - timedelta(days=7), hoje + timedelta(days=45))
        coleta_meu.gravar_agenda_senado(c, ev, hoje - timedelta(days=7), hoje + timedelta(days=45))  # sem duplicar
    # Alan Rick (5672) é do GPARGENTINA, não da CAE; Plenário vale para todos; o nome dele está na pauta
    ag = meu.agenda_senador(5672, hoje, hoje + timedelta(days=10))
    itens = [e for d in ag for e in d["itens"]]
    assert {e["colegiado"] for e in itens} == {"GPARGENTINA", "PLEN"}
    assert next(e for e in itens if e["colegiado"] == "GPARGENTINA")["citado"]
    todos = [e for d in meu.agenda_senador(5672, hoje, hoje + timedelta(days=10), so_dele=False) for e in d["itens"]]
    assert len(todos) == 3
    # compromisso da equipe pela tela
    adm = _cliente()
    tok = _tok(adm, "/meu/5672?secao=agenda")
    adm.post("/meu/5672?secao=agenda", data={"_csrf": tok, "acao": "novo", "data": d1, "hora": "18:30",
                                            "titulo": "Jantar com prefeitos do Acre", "local": "Gabinete"})
    html = adm.get("/meu/5672?secao=agenda").get_data(as_text=True)
    assert "Jantar com prefeitos do Acre" in html and "senador na pauta" in html and "CAE" not in html
    assert "Jantar com prefeitos do Acre" not in adm.get("/meu/5894?secao=agenda").get_data(as_text=True)


def test_solicitacoes_com_anexos():
    import io
    adm = _cliente()
    tok = _tok(adm, "/meu/5894?secao=solicitacoes")
    r = adm.post("/meu/5894?secao=solicitacoes", content_type="multipart/form-data", data={
        "_csrf": tok, "acao": "nova", "titulo": "Nota técnica sobre a PEC 1/2025",
        "descricao": "Pedido do senador na reunião de segunda.", "solicitante": "o senador", "prazo": "2020-01-01",
        "arquivos": [(io.BytesIO(b"%PDF-1.4 conteudo"), "minuta.pdf"), (io.BytesIO(b"a;b\n1;2"), "dados.csv")]})
    assert r.status_code == 302
    html = adm.get("/meu/5894?secao=solicitacoes").get_data(as_text=True)
    assert "Nota técnica sobre a PEC 1/2025" in html and "minuta.pdf" in html and "atrasada" in html
    import re
    aid = int(re.search(r"/meu/5894/anexo/(\d+)", html).group(1))
    sid = int(re.search(r'name="id" value="(\d+)"', html.split("Nota técnica")[1]).group(1))
    baixado = adm.get(f"/meu/5894/anexo/{aid}")
    assert baixado.status_code == 200 and baixado.data.startswith(b"%PDF") and "attachment" in baixado.headers["Content-Disposition"]
    assert adm.get(f"/meu/5672/anexo/{aid}").status_code == 404        # anexo de outro senador
    adm.post("/meu/5894?secao=solicitacoes", data={"_csrf": tok, "acao": "status", "id": sid, "status": "concluida"})
    assert "concluida (1)" in adm.get("/meu/5894?secao=solicitacoes").get_data(as_text=True).lower() or \
        "Concluída (1)" in adm.get("/meu/5894?secao=solicitacoes").get_data(as_text=True)
    r = adm.post("/meu/5894?secao=solicitacoes", content_type="multipart/form-data", data={
        "_csrf": tok, "acao": "nova", "titulo": "Arquivo perigoso", "arquivos": [(io.BytesIO(b"MZ"), "virus.exe")]})
    assert "Tipo de arquivo não aceito" in adm.get("/meu/5894?secao=solicitacoes").get_data(as_text=True)
    assert "Arquivo perigoso" not in adm.get("/meu/5894?secao=solicitacoes").get_data(as_text=True)


def _regimento_falso():
    linhas = []
    for n in range(1, 451):
        if n == 403:
            corpo = "Toda dúvida sobre interpretação deste Regimento, na sua prática, constitui questão de ordem."
        elif n == 412:
            corpo = "A legitimidade na elaboração de norma legal é assegurada pela observância das normas regimentais."
        elif n == 235:
            corpo = "A prorrogação do prazo de emendas perante a comissão depende de requerimento aprovado."
        else:
            corpo = f"Disposição genérica número {n} sobre funcionamento das sessões."
        linhas.append(f"Art. {n}{'º' if n < 10 else '.'} {corpo}")
    return "\n".join(linhas)


def test_questao_de_ordem_por_ia(monkeypatch):
    import io
    import questoes
    texto = _regimento_falso()
    arts = questoes.artigos(texto)
    assert len(arts) == 450 and arts[402][0] == 403
    sel = [n for n, _ in questoes.selecionar_trechos(texto, "prazo de emendas na comissão foi encerrado sem requerimento")]
    assert set(range(403, 409)) <= set(sel) and 235 in sel and len(sel) < 60
    adm = _cliente()
    tok = _tok(adm, "/meu/5894?secao=questoes")
    html = adm.get("/meu/5894?secao=questoes").get_data(as_text=True)
    assert "ANTHROPIC_API_KEY" in html                     # sem chave: avisa e não gera
    r = adm.post("/meu/5894?secao=questoes", content_type="multipart/form-data", data={
        "_csrf": tok, "acao": "regimento", "arquivo": (io.BytesIO(texto.encode()), "RISF.txt")})
    assert "450 artigos" in adm.get("/meu/5894?secao=questoes").get_data(as_text=True)

    enviado = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"content": [{"type": "text", "text": "## Cabimento\nCabe, no Plenário.\n## Questão de ordem (texto para leitura)\n"
                                 "Senhor Presidente, com fundamento no art. 403 do Regimento Interno, formulo a seguinte questão de ordem: **<script>x</script>**\n"
                                 "## Pedido\n- Que seja reaberto o prazo de emendas."}]}

    class _Sessao:
        def post(self, url, json=None, timeout=None, headers=None):
            enviado.update(url=url, corpo=json, headers=headers)
            return _Resp()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste")
    monkeypatch.setattr(questoes, "processar_em_segundo_plano",
                        lambda qid, nome=None: questoes.processar(qid, nome, sessao=_Sessao()))
    adm.post("/meu/5894?secao=questoes", data={"_csrf": tok, "acao": "gerar",
             "situacao": "A comissão encerrou o prazo de emendas sem requerimento aprovado e já vai votar o parecer hoje."})
    assert enviado["url"] == questoes.API_URL and enviado["headers"]["x-api-key"] == "sk-teste"
    assert enviado["corpo"]["model"] == questoes.modelo()
    msg = enviado["corpo"]["messages"][0]["content"]
    assert "<regimento>" in msg and "Art. 403" in msg and "Art. 235" in msg and "Flávio Bolsonaro" in msg
    html = adm.get("/meu/5894?secao=questoes").get_data(as_text=True)
    assert "<h3>Cabimento</h3>" in html and "Que seja reaberto o prazo de emendas" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html   # HTML da IA é escapado
    assert "Artigos do Regimento enviados à IA" in html
    # erro da API aparece para o usuário
    class _Erro(_Resp):
        status_code = 401
        text = "invalid x-api-key"
        def json(self):
            return {"error": {"message": "invalid x-api-key"}}
    qid = questoes.criar_pedido(5894, "admin", "Outra situação qualquer com detalhes suficientes para o teste.")
    questoes.processar(qid, sessao=type("S", (), {"post": lambda self, *a, **k: _Erro()})())
    assert questoes.historico(5894)[0]["resposta"].startswith("[ERRO] A API da Anthropic respondeu 401")


def test_radar_relatorias_clipping_e_rankings():
    import coleta_meu
    import meu
    from datetime import date
    rss = """<?xml version="1.0"?><rss><channel>
      <item><title>Senador apresenta PEC sobre fundo - Agência Senado</title><link>https://exemplo/a</link>
        <pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate><source url="https://www12.senado.leg.br">Agência Senado</source></item>
      <item><title>Outra notícia</title><link>https://exemplo/b</link><pubDate>xx</pubDate></item>
    </channel></rss>"""
    itens = coleta_meu.parse_rss(rss)
    assert itens[0]["titulo"] == "Senador apresenta PEC sobre fundo" and itens[0]["fonte"] == "Agência Senado"
    assert itens[1]["publicado"] is None
    with engine.begin() as c:
        assert coleta_meu.gravar_noticias(c, 5894, "Flávio Bolsonaro", itens) == 2
        assert coleta_meu.gravar_noticias(c, 5894, "Flávio Bolsonaro", itens) == 0      # sem duplicar
    adm = _cliente()
    tok = _tok(adm, "/meu/5894?secao=radar")
    adm.post("/meu/5894?secao=radar", data={"_csrf": tok, "acao": "add", "termo": "segurança pública"})
    assert ("segurança pública", "segurança pública") in coleta_meu.consultas_radar(5894)
    assert coleta_meu.consultas_radar(5894)[0] == ("Flávio Bolsonaro", '"Flávio Bolsonaro" senador')
    assert "news.google.com/rss/search" in coleta_meu.url_noticias("x")
    html = adm.get("/meu/5894?secao=radar").get_data(as_text=True)
    assert "Senador apresenta PEC sobre fundo" in html and "segurança pública" in html

    doc_rel = {"relatorias": [
        {"materia": {"identificacao": "PL 500/2025", "codigoMateria": 170001, "ementa": "Trata de Y."},
         "siglaColegiado": "CCJ", "dataDesignacao": "2025-03-01", "situacaoAtual": "APROVADA PELA COMISSÃO",
         "dataSituacaoAtual": date.today().isoformat()},
        {"IdentificacaoMateria": {"SiglaSubtipoMateria": "PLS", "NumeroMateria": "0012", "AnoMateria": "2019"},
         "IdentificacaoComissao": {"SiglaComissao": "CAE"}, "DataDesignacao": "2019-05-01"}]}
    rels = coleta_meu.parse_relatorias(doc_rel)
    assert [r["identificacao"] for r in rels] == ["PL 500/2025", "PLS 12/2019"]
    assert rels[0]["colegiado"] == "CCJ" and rels[1]["colegiado"] == "CAE"
    with engine.begin() as c:
        coleta_meu.gravar_relatorias(c, 5894, rels)
        coleta_meu.gravar_relatorias(c, 5894, rels)
    clip = meu.clipping(5894, date(2023, 1, 1), date.today())
    tipos = {(e["tipo"], e["identificacao"]) for d in clip["dias"] for e in d["itens"]}
    assert ("relatoria", "PL 500/2025") in tipos and ("autoria", "PL 2100/2023") in tipos   # virou norma
    assert ("autoria", "PEC 1/2025") not in tipos                                         # só tramitando
    assert meu.eh_aprovacao("APROVADA") and meu.eh_aprovacao("Remetida à Câmara dos Deputados")
    assert not meu.eh_aprovacao("REJEITADA") and not meu.eh_aprovacao("AGUARDANDO DESIGNAÇÃO DO RELATOR")

    rk = {r["slug"]: r for r in meu.rankings(5894)}
    assert rk["projetos"]["posicao"]["pos"] >= 1 and rk["projetos"]["sugestoes"]
    assert meu._passos_para(5, 10, 60) == 3 and meu._passos_para(9, 10, 60) == 0
    assert meu._mil(1234567.8) == "1.234.568"
    for r in rk.values():
        for x in r["sugestoes"]:
            assert ". passa " not in x, x          # vírgula da frase não vira ponto
    for secao in ("visao", "agenda", "notificacoes", "solicitacoes", "questoes", "radar", "rankings", "clipping"):
        r = adm.get(f"/meu/5894?secao={secao}")
        assert r.status_code == 200, secao
    assert "PL 500/2025" in adm.get("/meu/5894?secao=clipping").get_data(as_text=True)
    assert adm.get("/meu/").status_code == 200                               # admin escolhe o senador


RSS_PORTAL = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>G1</title>
<item><title>Flávio Bolsonaro cobra explicações do ministério sobre obras paradas</title><link>https://g1.exemplo/1</link>
  <description>&lt;p&gt;O senador quer saber por que 40 obras estão paralisadas.&lt;/p&gt;</description><pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Governo anuncia novo plano</title><link>https://g1.exemplo/2</link>
  <description>Plano prevê ações de segurança pública nas fronteiras.</description><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Campeonato de futebol começa</title><link>https://g1.exemplo/3</link><description>Esporte.</description></item>
<item><title>Flávio Bolsonarinho é personagem de novela</title><link>https://g1.exemplo/4</link><description>Ficção.</description></item>
</channel></rss>"""
ATOM_PORTAL = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Poder360</title>
<entry><title>Oposição: Flávio Bolsonaro apresenta PDL contra decreto</title><link href="https://poder.exemplo/9"/>
  <summary>O projeto susta o decreto.</summary><updated>2026-10-25T09:00:00Z</updated></entry></feed>"""
RSS_GOOGLE = """<?xml version="1.0"?><rss><channel>
<item><title>Senador Flávio Bolsonaro visita hospital em SP - Folha de S.Paulo</title><link>https://news.google/a</link>
  <description>&lt;a href="x"&gt;Senador Flávio Bolsonaro visita hospital em SP&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font&gt;Folha de S.Paulo&lt;/font&gt;</description>
  <pubDate>Tue, 27 Oct 2026 08:00:00 GMT</pubDate><source url="https://folha.uol.com.br">Folha de S.Paulo</source></item>
<item><title>Flávio Bolsonaro cobra explicações do ministério sobre obras paradas - G1</title><link>https://g1.exemplo/1</link>
  <pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate><source url="https://g1.globo.com">G1</source></item>
</channel></rss>"""


def _buscar_fake(url):
    if url == "https://g1.globo.com/rss/g1/politica/":
        return RSS_PORTAL.encode()
    if url == "https://www.poder360.com.br/feed/":
        return ATOM_PORTAL.encode()
    if "news.google.com" in url:
        return RSS_GOOGLE.encode() if "Fl" in url else b"<rss><channel></channel></rss>"
    return None


class _IAFake:
    """Responde como a IA do Monitor: relevante para cobranças, irrelevante para o resto."""
    def __init__(self):
        self.pedidos = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.pedidos.append(json)
        msg = json["messages"][0]["content"]
        if "obras paradas" in msg:
            txt = ('```json\n{"relevante": true, "categoria": "Infraestrutura e serviços públicos", "tipo_acao": '
                   '"Requerimento de informações", "acao_sugerida": "Apresentar RIC ao Ministério sobre as 40 obras.", '
                   '"justificativa": "Omissão do Executivo."}\n```')
        elif "hospital" in msg:
            txt = "resposta quebrada sem json"
        else:
            txt = '{"relevante": false, "categoria": null, "tipo_acao": null, "acao_sugerida": null, "justificativa": "Sem relação com fiscalização."}'

        class R:
            status_code = 200

            def json(self_):
                return {"content": [{"type": "text", "text": txt}]}
        return R()


def test_radar_no_modelo_do_monitor():
    from datetime import date, datetime, timedelta
    import app as painel
    import radar
    from sqlalchemy import text
    with engine.begin() as c:
        c.execute(text("DELETE FROM noticias"))
        c.execute(text("DELETE FROM temas_radar"))
    radar.garantir_fontes()
    fontes = radar.lista_fontes()
    assert len(fontes) == len(radar.FONTES_PADRAO) == 18
    assert {f["nome"] for f in fontes} >= {"G1 Política", "Poder360", "Folha Poder"}
    veja = next(f for f in fontes if f["nome"] == "Veja Brasil")
    adm = _cliente()
    tok = _tok(adm, "/meu/5894?secao=radar")
    adm.post("/meu/5894?secao=radar", data={"_csrf": tok, "acao": "fonte_del", "fonte": veja["id"]})
    radar.garantir_fontes()                                    # exclusão do admin é respeitada
    assert len(radar.lista_fontes()) == 17
    adm.post("/meu/5894?secao=radar", data={"_csrf": tok, "acao": "add", "termo": "segurança pública"})
    assert radar.casa_termo("Flávio Bolsonarinho na novela", radar._termos_norm(5894)) is None      # palavra inteira
    assert radar.casa_termo("A deputada FLAVIO BOLSONARO falou", radar._termos_norm(5894)) == "Flávio Bolsonaro"

    ia = _IAFake()
    os.environ["ANTHROPIC_API_KEY"] = "sk-teste"
    try:
        res = radar.ciclo([5894], buscar=_buscar_fake, sessao=ia)
        assert res == {"portais": 3, "google": 1, "analisadas": 4}
        assert radar.ciclo([5894], buscar=_buscar_fake, sessao=ia)["portais"] == 0      # já vistas
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    itens = {n["link"]: n for n in radar.lista(5894)}
    assert set(itens) == {"https://g1.exemplo/1", "https://g1.exemplo/2", "https://poder.exemplo/9", "https://news.google/a"}
    g1 = itens["https://g1.exemplo/1"]
    assert g1["origem"] == "portal" and g1["fonte"] == "G1 Política" and g1["termo"] == "Flávio Bolsonaro"
    assert g1["resumo"] == "O senador quer saber por que 40 obras estão paralisadas."
    assert g1["relevante"] is True and g1["tipo_acao"] == "Requerimento de informações" and g1["status"] == "Pendente"
    assert itens["https://g1.exemplo/2"]["termo"] == "segurança pública" and itens["https://g1.exemplo/2"]["relevante"] is False
    assert itens["https://poder.exemplo/9"]["fonte"] == "Poder360"
    gg = itens["https://news.google/a"]
    assert gg["origem"] == "google" and gg["fonte"] == "Folha de S.Paulo" and gg["titulo"] == "Senador Flávio Bolsonaro visita hospital em SP"
    assert gg["relevante"] is None and "não pôde ser lida" in gg["justificativa"]
    pedido = ia.pedidos[0]
    assert pedido["model"] == radar.modelo() and "Liderança da Oposição" in pedido["system"]
    assert "senador Flávio Bolsonaro (PL-RJ)" in pedido["messages"][0]["content"]
    assert [n["link"] for n in radar.lista(5894, filtro="relevantes")] == ["https://g1.exemplo/1"]
    assert len(radar.lista(5894, filtro="portal")) == 3 and len(radar.lista(5894, termo="segurança pública")) == 1

    # tela: filtro, análise e acompanhamento pela equipe
    html = adm.get("/meu/5894?secao=radar&filtro=relevantes").get_data(as_text=True)
    assert "Apresentar RIC ao Ministério sobre as 40 obras." in html and "Omissão do Executivo." in html
    assert "Campeonato" not in html and "Portais acompanhados" in html and "G1 Política" in html
    adm.post("/meu/5894?secao=radar", data={"_csrf": tok, "acao": "item", "id": g1["id"], "status": "Ação efetuada",
                                          "assessor": "Marcelo", "acao_efetuada": "RQS 1234/2026 protocolado"})
    html = adm.get("/meu/5894?secao=radar&filtro=Ação efetuada").get_data(as_text=True)
    assert "RQS 1234/2026 protocolado" in html and "Marcelo" in html
    assert radar.lista(5672) == []
    adm.post("/meu/5672?secao=radar", data={"_csrf": tok, "acao": "item", "id": g1["id"], "status": "Pendente"})
    assert radar.lista(5894, filtro="Ação efetuada")[0]["id"] == g1["id"]          # item de outro senador: intocado
    html = adm.get("/meu/5894?secao=visao").get_data(as_text=True)
    assert "Requerimento de informações" in html                               # selo da IA na visão geral

    # portais: admin acrescenta; quem não é admin não mexe
    adm.post("/meu/5894?secao=radar", data={"_csrf": tok, "acao": "fonte_add", "nome": "Agência Senado",
                                          "url": "https://www12.senado.leg.br/noticias/rss"})
    assert "Agência Senado" in {f["nome"] for f in radar.lista_fontes()}
    adm.post("/meu/5894?secao=radar", data={"_csrf": tok, "acao": "fonte_add", "nome": "Ruim", "url": "ftp://x"})
    assert "endereço do feed RSS" in adm.get("/meu/5894?secao=radar").get_data(as_text=True)
    tok_u = _tok(adm, "/usuarios")
    adm.post("/usuarios", data={"_csrf": tok_u, "acao": "criar", "login": "radar.leitor", "nome": "Leitor", "senha": "senha-segura-2"})
    uid = _uid("radar.leitor")
    adm.post("/usuarios", data={"_csrf": tok_u, "acao": "vincular", "id": uid, "senadores": ["5894"]})
    u = _cliente_usuario("radar.leitor", "senha-segura-2")
    tu = _tok(u, "/meu/5894?secao=radar")
    assert u.post("/meu/5894?secao=radar", data={"_csrf": tu, "acao": "fonte_del", "fonte": veja["id"]}).status_code == 403
    assert "Adicionar portal" not in u.get("/meu/5894?secao=radar").get_data(as_text=True)

    # limpeza: notícia antiga sem acompanhamento sai; a trabalhada fica
    with engine.begin() as c:
        c.execute(text("UPDATE noticias SET coletado_em = :d"), {"d": datetime.now() - timedelta(days=radar.DIAS_NOTICIAS + 5)})
        radar.gravar_noticias(c, 5894, "x", [])
    assert [n["link"] for n in radar.lista(5894)] == ["https://g1.exemplo/1"]

    # agendamento a cada 2 horas
    radar.ciclo(None, buscar=lambda u: None)
    assert not radar.precisa_ciclo() and radar.precisa_ciclo(datetime.now() + timedelta(hours=radar.HORAS_CICLO, minutes=1))
    os.environ["AUTO_RADAR"] = "0"
    try:
        assert painel.radar_se_preciso() is False
    finally:
        os.environ.pop("AUTO_RADAR")
    assert radar.extrair_json('lixo {"relevante": false} lixo') == {"relevante": False}


def test_banco_antigo_ganha_colunas_novas():
    """Quem já tinha a tabela de notícias antes do radar novo recebe as colunas no boot."""
    from sqlalchemy import inspect
    with engine.begin() as c:
        c.execute(text("ALTER TABLE noticias DROP COLUMN acao_efetuada"))
        c.execute(text("ALTER TABLE noticias DROP COLUMN relevante"))
    assert "relevante" not in {x["name"] for x in inspect(engine).get_columns("noticias")}
    init_db()
    cols = {x["name"] for x in inspect(engine).get_columns("noticias")}
    assert {"relevante", "acao_efetuada", "origem", "resumo"} <= cols


def _uid(login):
    from sqlalchemy import text as _t
    with engine.connect() as c:
        return c.execute(_t("SELECT id FROM usuarios WHERE login = :l"), {"l": login}).scalar()

