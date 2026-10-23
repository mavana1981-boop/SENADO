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
