"""Painel do Senado — Liderança da Oposição.  Flask + PostgreSQL (Railway)."""
import logging
import os
import threading

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import metrics
from db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)
init_db()

ABAS = [("perfil", "Perfil"), ("proposicoes", "Proposições"), ("cota", "Cota"),
        ("votacoes", "Votações"), ("orientacoes", "Orientações"), ("rankings", "Rankings")]
PRONTAS = {"orientacoes", "rankings"}


@app.context_processor
def _ctx():
    return {"abas": ABAS, "prontas": PRONTAS}


def _contexto_filtros():
    f = metrics.Filtros.from_args(request.args)
    return f, metrics.opcoes_filtro()


@app.template_filter("pct")
def _fmt_pct(v):
    return "—" if v is None else f"{v:.1f}%".replace(".", ",")


@app.template_filter("data")
def _fmt_data(v):
    if not v:
        return ""
    s = str(v)
    return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"


@app.route("/")
def index():
    return redirect(url_for("orientacoes"))


@app.route("/orientacoes")
def orientacoes():
    f, op = _contexto_filtros()
    gov = metrics.alinhamento("Governo", f)
    opo = metrics.alinhamento("Oposição", f)
    return render_template(
        "orientacoes.html", aba="orientacoes", f=f, op=op,
        resumo=metrics.resumo(f),
        governismo=gov, oposicionismo=opo,
        por_partido=metrics.alinhamento_agrupado("partido", f),
        por_uf=metrics.alinhamento_agrupado("uf", f),
        votacoes=metrics.votacoes_orientadas(f, 150))


@app.route("/rankings")
def rankings():
    f, op = _contexto_filtros()
    return render_template(
        "rankings.html", aba="rankings", f=f, op=op,
        participacao=metrics.participacao(f),
        oposicionismo=metrics.alinhamento("Oposição", f, minimo=10),
        governismo=metrics.alinhamento("Governo", f, minimo=10),
        fidelidade=metrics.fidelidade(f))


@app.route("/<aba>")
def em_breve(aba):
    if aba not in dict(ABAS):
        abort(404)
    _, op = _contexto_filtros()
    return render_template("em_breve.html", aba=aba, titulo=dict(ABAS)[aba], op=op)


# --------- coleta disparada pela web (protegida por token) ---------
_coleta_lock = threading.Lock()


@app.post("/api/coletar")
def api_coletar():
    token = os.environ.get("COLETA_TOKEN")
    if not token or request.args.get("token") != token:
        abort(403)
    if not _coleta_lock.acquire(blocking=False):
        return jsonify(status="já existe uma coleta em andamento"), 409
    desde = request.args.get("desde")

    def _rodar():
        try:
            import collector
            from datetime import date
            collector.executar(date.fromisoformat(desde) if desde else None)
        finally:
            _coleta_lock.release()

    threading.Thread(target=_rodar, daemon=True).start()
    return jsonify(status="coleta iniciada", desde=desde or "últimos 45 dias"), 202


def _carga_inicial_em_segundo_plano():
    """No primeiro deploy o banco está vazio: carrega a legislatura inteira sem travar o site."""
    if os.environ.get("AUTO_CARGA", "1") != "1":
        return
    if not _coleta_lock.acquire(blocking=False):
        return

    def _rodar():
        try:
            import collector
            collector.carga_inicial_se_vazio()
        except Exception:
            logging.exception("falha na carga inicial")
        finally:
            _coleta_lock.release()

    threading.Thread(target=_rodar, daemon=True).start()


# Só roda no Railway (ou com AUTO_CARGA=1 local). Desligue com AUTO_CARGA=0.
if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("AUTO_CARGA") == "1":
    _carga_inicial_em_segundo_plano()


@app.get("/saude")
def saude():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
