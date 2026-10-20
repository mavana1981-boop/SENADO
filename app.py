"""Painel do Senado — Liderança da Oposição.  Flask + PostgreSQL (Railway)."""
import logging
import os
import threading
from datetime import date

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

import auth
import metrics
import paineis
from db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)
init_db()
auth.configurar(app)

ABAS = [("perfil", "Perfil"), ("proposicoes", "Proposições"), ("cota", "Cota"),
        ("votacoes", "Votações"), ("orientacoes", "Orientações"), ("rankings", "Rankings")]
ABAS_SENADOR = {"perfil", "proposicoes", "cota"}   # abas com seletor de senador


@app.context_processor
def _ctx():
    return {"abas": ABAS, "abas_senador": ABAS_SENADOR}


def _contexto_filtros():
    f = metrics.Filtros.from_args(request.args)
    return f, metrics.opcoes_filtro()


# ---------------- filtros de template ----------------

@app.template_filter("pct")
def _fmt_pct(v):
    return "—" if v is None else f"{v:.1f}%".replace(".", ",")


@app.template_filter("data")
def _fmt_data(v):
    if not v:
        return ""
    s = str(v)
    return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"


@app.template_filter("brl")
def _fmt_brl(v, casas=2):
    if v is None:
        return "—"
    s = f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


@app.template_filter("mil")
def _fmt_mil(v):
    return "—" if v is None else f"{v:,.0f}".replace(",", ".")


# ---------------- abas de votação ----------------

@app.route("/")
def index():
    return redirect(url_for("orientacoes"))


@app.route("/orientacoes")
def orientacoes():
    f, op = _contexto_filtros()
    return render_template(
        "orientacoes.html", aba="orientacoes", f=f, op=op,
        resumo=metrics.resumo(f),
        governismo=metrics.alinhamento("Governo", f),
        oposicionismo=metrics.alinhamento("Oposição", f),
        por_partido=metrics.alinhamento_agrupado("partido", f),
        por_uf=metrics.alinhamento_agrupado("uf", f),
        votacoes=metrics.votacoes_orientadas(f, 150))


@app.route("/rankings")
def rankings():
    f, op = _contexto_filtros()
    ano_ini = f.inicio.year if f.inicio else None
    ano_fim = f.fim.year if f.fim else None

    def _filtra(lista):
        return [r for r in lista if (not f.partido or r.get("partido") == f.partido)
                and (not f.uf or r.get("uf") == f.uf)]

    return render_template(
        "rankings.html", aba="rankings", f=f, op=op,
        participacao=metrics.participacao(f),
        oposicionismo=metrics.alinhamento("Oposição", f, minimo=10),
        governismo=metrics.alinhamento("Governo", f, minimo=10),
        fidelidade=[r for r in metrics.fidelidade(f) if r["total"] >= 10],
        projetos=_filtra(paineis.ranking_proposicoes(f.inicio, f.fim)),
        economia=_filtra(paineis.ranking_economia(ano_ini, ano_fim)))


@app.route("/votacoes")
def votacoes():
    f, op = _contexto_filtros()
    busca = (request.args.get("busca") or "").strip()
    lista = metrics.lista_votacoes(f, busca)
    vid = request.args.get("id", type=int)
    if vid is None and lista:
        vid = next((v["id"] for v in lista if not v["secreta"]), lista[0]["id"])
    det = metrics.detalhe_votacao(vid) if vid is not None else None
    return render_template("votacoes.html", aba="votacoes", f=f, op=op, busca=busca,
                           lista=lista, det=det, vid=vid)


# ---------------- abas por senador ----------------

def _senador_da_requisicao():
    lista = paineis.lista_senadores()
    pedido = request.args.get("senador", type=int) or session.get("senador")
    cod = paineis.escolher_senador(pedido, lista)
    if cod:
        session["senador"] = cod
    return cod, lista


def _data(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


@app.route("/perfil")
def perfil():
    cod, lista = _senador_da_requisicao()
    _, op = _contexto_filtros()
    if not cod:
        return render_template("vazio.html", aba="perfil", op=op, lista=lista)
    s = paineis.senador(cod)
    return render_template(
        "perfil.html", aba="perfil", op=op, lista=lista, s=s, cod=cod,
        perfil=paineis.perfil(cod), ind=paineis.indicadores_senador(cod),
        props=paineis.proposicoes(cod, limite=0), cota=paineis.cota_senador(cod))


@app.route("/proposicoes")
def proposicoes():
    cod, lista = _senador_da_requisicao()
    _, op = _contexto_filtros()
    if not cod:
        return render_template("vazio.html", aba="proposicoes", op=op, lista=lista)
    a = request.args
    filtros = {"papel": a.get("papel", ""), "sigla": a.get("sigla", ""),
               "situacao": a.get("situacao", ""), "inicio": _data(a.get("inicio")),
               "fim": _data(a.get("fim")), "busca": (a.get("busca") or "").strip()}
    return render_template(
        "proposicoes.html", aba="proposicoes", op=op, lista=lista, cod=cod,
        s=paineis.senador(cod), fp=filtros, props=paineis.proposicoes(cod, **filtros),
        todas_siglas=paineis.proposicoes(cod, limite=0)["siglas"])


@app.route("/cota")
def cota():
    cod, lista = _senador_da_requisicao()
    _, op = _contexto_filtros()
    if not cod:
        return render_template("vazio.html", aba="cota", op=op, lista=lista)
    ano = request.args.get("ano", type=int)
    dados = paineis.cota_senador(cod, ano, ano) if ano else paineis.cota_senador(cod)
    todos_anos = paineis.cota_senador(cod)["anos"]
    return render_template("cota.html", aba="cota", op=op, lista=lista, cod=cod,
                           c=dados, ano=ano, todos_anos=todos_anos)


# ---------------- coleta ----------------
_coleta_lock = threading.Lock()


def _rodar_coleta(fn, *a):
    try:
        fn(*a)
    except Exception:
        logging.exception("falha na coleta")
    finally:
        paineis.limpar_cache()
        _coleta_lock.release()


@app.post("/api/coletar")
def api_coletar():
    token = os.environ.get("COLETA_TOKEN")
    if not token or request.args.get("token") != token:
        abort(403)
    if not _coleta_lock.acquire(blocking=False):
        return jsonify(status="já existe uma coleta em andamento"), 409
    desde = request.args.get("desde")
    import collector
    threading.Thread(target=_rodar_coleta, daemon=True,
                     args=(collector.executar, _data(desde))).start()
    return jsonify(status="coleta iniciada", desde=desde or "rotina diária"), 202


def _carga_inicial_em_segundo_plano():
    """No primeiro deploy o banco está vazio: carrega a legislatura inteira sem travar o site."""
    if not _coleta_lock.acquire(blocking=False):
        return
    import collector
    threading.Thread(target=_rodar_coleta, daemon=True,
                     args=(collector.carga_inicial_se_vazio,)).start()


# Só roda no Railway (ou com AUTO_CARGA=1 local). Desligue com AUTO_CARGA=0.
if os.environ.get("AUTO_CARGA") != "0" and (os.environ.get("RAILWAY_ENVIRONMENT")
                                             or os.environ.get("AUTO_CARGA") == "1"):
    _carga_inicial_em_segundo_plano()


@app.get("/saude")
def saude():
    return jsonify(ok=True)


@app.errorhandler(403)
def _proibido(e):
    return render_template("erro.html", aba=None, codigo=403,
                           msg="Você não tem permissão para acessar esta página."), 403


@app.errorhandler(404)
def _nao_encontrado(e):
    return render_template("erro.html", aba=None, codigo=404, msg="Página não encontrada."), 404


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
