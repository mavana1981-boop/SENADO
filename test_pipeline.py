"""Testes com dados reais do Senado (sessão de 12/12/2023, PL 3.626/2023) no formato exato da API.

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
        collector.marcar_coleta(c)


_carregar()


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
    import app as painel
    cli = painel.app.test_client()
    for url in ["/orientacoes", "/orientacoes?opostas=0", "/orientacoes?partido=PL&opostas=0&opostas=1",
                "/rankings", "/rankings?uf=RJ", "/perfil", "/saude"]:
        r = cli.get(url)
        assert r.status_code == 200, url
    html = cli.get("/orientacoes").get_data(as_text=True)
    assert "Flávio Bolsonaro" in html and "Emenda nº 147" in html
    assert cli.post("/api/coletar").status_code == 403


if __name__ == "__main__":
    for nome, fn in list(globals().items()):
        if nome.startswith("test_"):
            fn()
            print("ok", nome)
