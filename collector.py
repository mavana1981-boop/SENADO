"""Coletor de dados do Senado Federal (API de Dados Abertos Legislativos).

Uso:
    python collector.py                    # incremental: últimos 45 dias + senadores
    python collector.py --desde 2023-02-01 # carga completa a partir da data
    python collector.py --senadores        # só atualiza o cadastro de senadores

Fontes:
    /dadosabertos/plenario/votacao/orientacaoBancada/{ini}/{fim}.json
        -> votações nominais do Plenário, votos de cada senador e orientações
           (partidos, Governo, Oposição, Maioria, Minoria)
    /dadosabertos/senador/lista/legislatura/{n}.json e /senador/lista/atual.json
        -> cadastro, foto, partido e UF
"""
import argparse
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests
from sqlalchemy import delete, insert, select, update

from db import (engine, init_db, meta, norm_nome, norm_partido, norm_voto,
                orientacoes, senadores, votacoes, votos)

BASE = os.environ.get("SENADO_API", "https://legis.senado.leg.br/dadosabertos")
LEGISLATURA = int(os.environ.get("LEGISLATURA", "57"))
INICIO_LEGISLATURA = os.environ.get("INICIO_LEGISLATURA", "2023-02-01")
PAUSA = 0.25  # a API aceita até 10 req/s; ficamos bem abaixo

log = logging.getLogger("coletor")
sess = requests.Session()
sess.headers.update({"Accept": "application/json",
                     "User-Agent": "painel-senado-oposicao/1.0"})


# ---------------- HTTP ----------------

def get_json(caminho: str, tentativas: int = 5):
    url = f"{BASE}{caminho}"
    for i in range(tentativas):
        try:
            r = sess.get(url, timeout=60)
        except requests.RequestException as e:
            log.warning("erro de rede em %s: %s", url, e)
            time.sleep(2 * (i + 1))
            continue
        if r.status_code == 200:
            time.sleep(PAUSA)
            if not r.content.strip():
                return None
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code in (429, 503, 502, 504):
            espera = 5 * (i + 1)
            log.warning("HTTP %s em %s — aguardando %ss", r.status_code, url, espera)
            time.sleep(espera)
            continue
        r.raise_for_status()
    raise RuntimeError(f"Falha ao obter {url} após {tentativas} tentativas")


def _lista(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


# ---------------- Senadores ----------------

def _foto(codigo):
    return f"https://www.senado.leg.br/senadores/img/fotos-oficiais/senador{codigo}.jpg"


def parse_parlamentar(p: dict) -> dict:
    ident = p.get("IdentificacaoParlamentar", {}) or {}
    codigo = int(ident.get("CodigoParlamentar"))
    uf = ident.get("UfParlamentar")
    if not uf:
        # Na lista por legislatura a UF vem só no mandato
        mand = p.get("Mandato") or p.get("Mandatos", {}).get("Mandato")
        for m in _lista(mand):
            if m and m.get("UfParlamentar"):
                uf = m["UfParlamentar"]
                break
    bloco = (ident.get("Bloco") or {}).get("NomeApelido")
    nome = (ident.get("NomeParlamentar") or "").strip()
    return {
        "codigo": codigo,
        "nome": nome,
        "nome_norm": norm_nome(nome),
        "nome_completo": ident.get("NomeCompletoParlamentar"),
        "partido": norm_partido(ident.get("SiglaPartidoParlamentar")) or None,
        "uf": uf,
        "bloco": bloco,
        "foto": _foto(codigo),
    }


def _parlamentares(doc: dict):
    if not doc:
        return []
    raiz = next(iter(doc.values()))  # ListaParlamentarEmExercicio / ListaParlamentarLegislatura
    return _lista((raiz.get("Parlamentares") or {}).get("Parlamentar"))


def coletar_senadores(conn=None, docs=None):
    """docs: (doc_legislatura, doc_atual) — usado nos testes; em produção busca na API."""
    if docs is None:
        doc_leg = get_json(f"/senador/lista/legislatura/{LEGISLATURA}.json")
        doc_atual = get_json("/senador/lista/atual.json")
    else:
        doc_leg, doc_atual = docs

    registros = {}
    for p in _parlamentares(doc_leg):
        r = parse_parlamentar(p)
        r["em_exercicio"] = False
        registros[r["codigo"]] = r
    for p in _parlamentares(doc_atual):
        r = parse_parlamentar(p)
        r["em_exercicio"] = True
        anterior = registros.get(r["codigo"], {})
        # dados atuais têm prioridade, mas não apagam UF/partido que só a outra lista traz
        registros[r["codigo"]] = {**anterior, **{k: v for k, v in r.items() if v is not None}}

    def _gravar(c):
        for r in registros.values():
            existe = c.execute(select(senadores.c.codigo).where(
                senadores.c.codigo == r["codigo"])).first()
            if existe:
                c.execute(update(senadores).where(senadores.c.codigo == r["codigo"]).values(**r))
            else:
                c.execute(insert(senadores).values(**r))
        if docs is None or registros:
            # quem saiu de exercício precisa ser desmarcado
            atuais = [k for k, v in registros.items() if v.get("em_exercicio")]
            if atuais:
                c.execute(update(senadores).where(senadores.c.codigo.notin_(atuais))
                          .values(em_exercicio=False))

    if conn is None:
        with engine.begin() as c:
            _gravar(c)
    else:
        _gravar(conn)
    log.info("senadores gravados: %d", len(registros))
    return len(registros)


# ---------------- Votações + orientações ----------------

def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_votacao(v: dict):
    vid = int(v["codigoVotacaoSve"])
    lista_votos = _lista(v.get("votosParlamentar"))
    secreta = bool(lista_votos) and all(norm_voto(x.get("voto")) == "SECRETO" for x in lista_votos)
    linha = {
        "id": vid,
        "sequencial": v.get("sequencialVotacao"),
        "data": _dt(v.get("dataInicioVotacao")),
        "sigla_materia": v.get("siglaTipoMateria"),
        "numero_materia": v.get("numeroMateria"),
        "ano_materia": v.get("anoMateria"),
        "descricao_materia": v.get("descricaoMateria"),
        "descricao_votacao": v.get("descricaoVotacao"),
        "dispositivo": v.get("dispositivo"),
        "sessao": v.get("descricaoSessao"),
        "presidente": v.get("presidenteSessao"),
        "qtd_sim": v.get("qtdVotosSim"),
        "qtd_nao": v.get("qtdVotosNao"),
        "qtd_abstencao": v.get("qtdVotosAbstencao"),
        "secreta": secreta,
    }
    vts = {}
    for x in lista_votos:
        nome = (x.get("nomeParlamentar") or "").strip()
        nn = norm_nome(nome)
        if not nn:
            continue
        vts[nn] = {"votacao_id": vid, "nome_norm": nn, "nome": nome,
                   "partido": norm_partido(x.get("partido")), "uf": x.get("uf"),
                   "voto": norm_voto(x.get("voto"))}
    oris = {}
    # quando há mais de um registro da mesma liderança, vale o mais recente
    for o in sorted(_lista(v.get("orientacoesLideranca")), key=lambda o: o.get("dataHora") or ""):
        lid = (o.get("partido") or "").strip()
        if not lid:
            continue
        nome_lid = lid if lid in ("Governo", "Oposição", "Maioria", "Minoria") else norm_partido(lid)
        oris[nome_lid] = {"votacao_id": vid, "lideranca": nome_lid, "voto": norm_voto(o.get("voto"))}
    return linha, list(vts.values()), list(oris.values())


def gravar_votacoes(doc: dict, conn) -> int:
    n = 0
    for v in _lista((doc or {}).get("votacoes")):
        linha, vts, oris = parse_votacao(v)
        vid = linha["id"]
        conn.execute(delete(votos).where(votos.c.votacao_id == vid))
        conn.execute(delete(orientacoes).where(orientacoes.c.votacao_id == vid))
        conn.execute(delete(votacoes).where(votacoes.c.id == vid))
        conn.execute(insert(votacoes).values(**linha))
        if vts:
            conn.execute(insert(votos), vts)
        if oris:
            conn.execute(insert(orientacoes), oris)
        n += 1
    return n


def _meses(inicio: date, fim: date):
    atual = inicio
    while atual <= fim:
        prox = (atual.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield atual, min(prox - timedelta(days=1), fim)
        atual = prox


def coletar_votacoes(desde: date, ate: date) -> int:
    total = 0
    for ini, fim in _meses(desde, ate):
        doc = get_json(f"/plenario/votacao/orientacaoBancada/"
                       f"{ini:%Y%m%d}/{fim:%Y%m%d}.json")
        with engine.begin() as c:
            n = gravar_votacoes(doc, c)
        log.info("%s a %s: %d votações", ini, fim, n)
        total += n
    return total


# ---------------- Cruzamento nome do voto -> código do senador ----------------

def vincular_senadores(conn):
    """Liga cada voto ao código do senador.
    1º por nome normalizado; 2º por mesma UF + mesmo último sobrenome
    (resolve 'Astr. Marcos Pontes' x 'Astronauta Marcos Pontes', 'Prof. Dorinha'...)."""
    cad = conn.execute(select(senadores.c.codigo, senadores.c.nome_norm, senadores.c.uf)).all()
    por_nome = {r.nome_norm: r.codigo for r in cad}
    por_uf_sobrenome = {}
    for r in cad:
        if r.nome_norm and r.uf:
            chave = (r.uf, r.nome_norm.split()[-1])
            por_uf_sobrenome.setdefault(chave, []).append(r.codigo)

    pares = conn.execute(select(votos.c.nome_norm, votos.c.uf).distinct()
                         .where(votos.c.codigo_senador.is_(None))).all()
    ligados = 0
    for nn, uf in pares:
        cod = por_nome.get(nn)
        if cod is None and nn and uf:
            cands = por_uf_sobrenome.get((uf, nn.split()[-1]), [])
            if len(cands) == 1:
                cod = cands[0]
        if cod is not None:
            conn.execute(update(votos).where(votos.c.nome_norm == nn, votos.c.uf == uf)
                         .values(codigo_senador=cod))
            ligados += 1
    log.info("nomes vinculados a senadores: %d de %d", ligados, len(pares))
    return ligados


def marcar_coleta(conn):
    agora = datetime.now().isoformat(timespec="seconds")
    conn.execute(delete(meta).where(meta.c.chave == "ultima_coleta"))
    conn.execute(insert(meta).values(chave="ultima_coleta", valor=agora))


def executar(desde: date | None = None, so_senadores=False):
    init_db()
    coletar_senadores()
    if not so_senadores:
        if desde is None:
            desde = date.today() - timedelta(days=45)
        coletar_votacoes(desde, date.today())
    with engine.begin() as c:
        vincular_senadores(c)
        marcar_coleta(c)


def carga_inicial_se_vazio():
    """Usado no primeiro deploy: se o banco estiver vazio, coleta a legislatura inteira."""
    init_db()
    with engine.connect() as c:
        tem = c.execute(select(votacoes.c.id).limit(1)).first()
    if not tem:
        log.info("banco vazio — carga completa desde %s", INICIO_LEGISLATURA)
        executar(date.fromisoformat(INICIO_LEGISLATURA))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", help="AAAA-MM-DD (carga completa a partir desta data)")
    ap.add_argument("--senadores", action="store_true", help="só o cadastro de senadores")
    ap.add_argument("--se-vazio", action="store_true", help="carga completa só se o banco estiver vazio")
    a = ap.parse_args()
    if a.se_vazio:
        carga_inicial_se_vazio()
    else:
        executar(date.fromisoformat(a.desde) if a.desde else None, a.senadores)
