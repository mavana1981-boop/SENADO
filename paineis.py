"""Consultas das abas Perfil, Proposições e Cota (e dos rankings que dependem delas)."""
import json
from collections import defaultdict
from datetime import date

from sqlalchemy import select, text

import cota
import metrics
from db import engine, votacoes, votos

SIGLAS_PROJETO = ("PL", "PLP", "PEC", "PDL", "PRS", "PLS", "PDS")

# Cache em memória das consultas pesadas; invalida sozinho quando há coleta nova.
_cache = {}


def _versao_dados():
    with engine.connect() as c:
        return c.execute(text("SELECT valor FROM meta WHERE chave = 'ultima_coleta'")).scalar()


def limpar_cache():
    _cache.clear()


def _memo(fn):
    def _w(*a):
        chave = (fn.__name__, a, _versao_dados())
        if chave not in _cache:
            if len(_cache) > 500:
                _cache.clear()
            _cache[chave] = fn(*a)
        return _cache[chave]
    _w.__name__ = fn.__name__
    return _w


# ---------------- senadores ----------------

def lista_senadores():
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT codigo, nome, partido, uf, foto, em_exercicio FROM senadores ORDER BY nome")).mappings()
        return [dict(r) for r in rows]


def senador(codigo):
    with engine.connect() as c:
        r = c.execute(text("SELECT * FROM senadores WHERE codigo = :c"), {"c": codigo}).mappings().first()
    return dict(r) if r else None


def escolher_senador(codigo_pedido, lista):
    """Senador pedido; senão o primeiro em exercício em ordem alfabética."""
    cods = {s["codigo"] for s in lista}
    if codigo_pedido in cods:
        return codigo_pedido
    em = [s for s in lista if s["em_exercicio"]]
    return (em or lista or [{"codigo": None}])[0]["codigo"]


# ---------------- perfil ----------------

def _lista(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _raiz(doc):
    if not isinstance(doc, dict) or not doc:
        return {}
    v = next(iter(doc.values()))
    return v if isinstance(v, dict) else {}


def perfil(codigo):
    with engine.connect() as c:
        r = c.execute(text("SELECT detalhe, comissoes, cargos FROM perfis WHERE codigo = :c"),
                      {"c": codigo}).mappings().first()
    out = {"basicos": {}, "comissoes": [], "cargos": [], "telefones": []}
    if not r:
        return out
    det = _raiz(json.loads(r["detalhe"] or "{}")).get("Parlamentar", {})
    basicos = det.get("DadosBasicosParlamentar", {}) or {}
    ident = det.get("IdentificacaoParlamentar", {}) or {}
    nasc = basicos.get("DataNascimento")
    idade = None
    if nasc:
        try:
            dn = date.fromisoformat(nasc[:10])
            hoje = date.today()
            idade = hoje.year - dn.year - ((hoje.month, hoje.day) < (dn.month, dn.day))
        except ValueError:
            pass
    out["basicos"] = {
        "nome_completo": ident.get("NomeCompletoParlamentar"),
        "email": ident.get("EmailParlamentar"),
        "pagina": ident.get("UrlPaginaParlamentar"),
        "nascimento": nasc, "idade": idade,
        "naturalidade": basicos.get("Naturalidade"), "uf_naturalidade": basicos.get("UfNaturalidade"),
        "endereco": (basicos.get("EnderecoParlamentar") or "").strip() or None,
    }
    out["telefones"] = [t.get("NumeroTelefone") for t in
                        _lista((det.get("Telefones") or {}).get("Telefone")) if t.get("NumeroTelefone")]

    com = _raiz(json.loads(r["comissoes"] or "{}")).get("Parlamentar", {}) or {}
    for x in _lista((com.get("MembroComissoes") or {}).get("Comissao")):
        if x.get("DataFim"):
            continue  # só participações atuais
        ic = x.get("IdentificacaoComissao") or {}
        out["comissoes"].append({"sigla": ic.get("SiglaComissao"), "nome": ic.get("NomeComissao"),
                                 "casa": ic.get("SiglaCasaComissao"),
                                 "participacao": x.get("DescricaoParticipacao"),
                                 "desde": x.get("DataInicio")})
    car = _raiz(json.loads(r["cargos"] or "{}")).get("Parlamentar", {}) or {}
    for x in _lista((car.get("Cargos") or {}).get("Cargo")):
        if x.get("DataFim"):
            continue
        ic = x.get("IdentificacaoComissao") or {}
        out["cargos"].append({"cargo": (x.get("DescricaoCargo") or "").capitalize(),
                              "sigla": ic.get("SiglaComissao"), "nome": ic.get("NomeComissao"),
                              "desde": x.get("DataInicio")})
    # comissões permanentes do Senado primeiro; frentes e grupos parlamentares depois
    def _peso(n):
        n = (n or "").lower()
        return 2 if ("frente" in n or "grupo parlamentar" in n) else (1 if "mista" in n else 0)
    out["comissoes"].sort(key=lambda x: (_peso(x["nome"]), x["participacao"] != "Titular", x["sigla"] or ""))
    out["cargos"].sort(key=lambda x: (_peso(x["nome"]), x["sigla"] or ""))
    todos = {x["sigla"]: x["nome"] for x in out["comissoes"] + out["cargos"]}
    out["frentes"] = sum(1 for n in todos.values() if "frente" in (n or "").lower())
    out["grupos"] = sum(1 for n in todos.values() if "grupo parlamentar" in (n or "").lower())
    return out


@_memo
def _indicadores_todos():
    f = metrics.Filtros(opostas=False)
    return {
        "gov": {r["codigo"]: r for r in metrics.alinhamento("Governo", metrics.Filtros(opostas=True)) if r.get("codigo")},
        "opo": {r["codigo"]: r for r in metrics.alinhamento("Oposição", metrics.Filtros(opostas=True)) if r.get("codigo")},
        "part": {r["codigo"]: r for r in metrics.participacao(f) if r.get("codigo")},
        "fid": {r["codigo"]: r for r in metrics.fidelidade(f) if r.get("codigo")},
    }


def indicadores_senador(codigo):
    """Números de votação do senador na legislatura (governismo, oposicionismo, participação)."""
    t = _indicadores_todos()
    res = {}
    for chave in ("gov", "opo"):
        linha = t[chave].get(codigo)
        res[chave] = linha["pct"] if linha else None
        res[chave + "_n"] = linha["total"] if linha else 0
    part = t["part"].get(codigo)
    res["participacao"] = part["pct"] if part else None
    res["votou"] = part["votou"] if part else 0
    res["realizadas"] = part["realizadas"] if part else 0
    fid = t["fid"].get(codigo)
    res["fidelidade"] = fid["pct"] if fid else None
    return res


def votos_oposicao(codigo, f=None):
    """Votações em que o senador seguiu ou contrariou a orientação da Oposição.

    Mesma regra do % de oposicionismo: entram só votos SIM/NÃO do senador em votações
    em que a Oposição orientou SIM ou NÃO. Respeita os filtros da aba Orientações
    (período, tipo de matéria e "só orientações opostas Governo × Oposição").
    """
    f = f or metrics.Filtros()
    where, p = f.where_votacoes()
    p["c"] = codigo
    sql = text(f"""
        SELECT vt.id, vt.data, vt.sigla_materia, vt.numero_materia, vt.ano_materia,
               vt.dispositivo, vt.descricao_materia, vt.descricao_votacao,
               v.voto AS voto, o.voto AS opo, g.voto AS gov
        FROM votos v
        JOIN votacoes vt ON vt.id = v.votacao_id
        JOIN orientacoes o ON o.votacao_id = v.votacao_id AND o.lideranca = 'Oposição'
        LEFT JOIN orientacoes g ON g.votacao_id = v.votacao_id AND g.lideranca = 'Governo'
        WHERE v.codigo_senador = :c AND o.voto IN ('SIM','NÃO') AND v.voto IN ('SIM','NÃO')
          AND {where}
        ORDER BY vt.data DESC, vt.id DESC""")
    with engine.connect() as c:
        linhas = [dict(r) for r in c.execute(sql, p).mappings()]
    seguiu = [r for r in linhas if r["voto"] == r["opo"]]
    contra = [r for r in linhas if r["voto"] != r["opo"]]
    return {"seguiu": seguiu, "contra": contra, "total": len(linhas), "opostas": f.opostas}


# ---------------- proposições ----------------

def proposicoes(codigo, papel="", sigla="", situacao="", inicio=None, fim=None, busca="", limite=400):
    cond, p = ["a.codigo_senador = :c"], {"c": codigo}
    if papel == "autor":
        cond.append("a.principal = :t")
        p["t"] = True
    elif papel == "coautor":
        cond.append("a.principal = :f")
        p["f"] = False
    if sigla:
        cond.append("pr.sigla = :sigla")
        p["sigla"] = sigla
    if situacao == "norma":
        cond.append("pr.virou_norma = :vn")
        p["vn"] = True
    elif situacao == "tramitando":
        cond.append("pr.tramitando = :tr")
        p["tr"] = True
    elif situacao == "encerrada":
        cond.append("pr.tramitando = :tr")
        p["tr"] = False
    if inicio:
        cond.append("pr.data_apresentacao >= :ini")
        p["ini"] = inicio.isoformat()
    if fim:
        cond.append("pr.data_apresentacao <= :fim")
        p["fim"] = fim.isoformat()
    if busca:
        cond.append("(LOWER(COALESCE(pr.ementa,'')) LIKE :b OR LOWER(pr.identificacao) LIKE :b)")
        p["b"] = f"%{busca.lower()}%"
    where = " AND ".join(cond)
    base = f"FROM processos pr JOIN autorias a ON a.processo_id = pr.id WHERE {where}"
    with engine.connect() as c:
        tot = c.execute(text(f"""SELECT COUNT(*) n,
                 SUM(CASE WHEN pr.virou_norma THEN 1 ELSE 0 END) normas,
                 SUM(CASE WHEN pr.tramitando THEN 1 ELSE 0 END) tram,
                 SUM(CASE WHEN a.principal THEN 1 ELSE 0 END) principal,
                 SUM(CASE WHEN pr.sigla IN {SIGLAS_PROJETO} THEN 1 ELSE 0 END) projetos
                 {base}"""), p).mappings().first()
        por_tipo = [dict(r) for r in c.execute(text(
            f"SELECT pr.sigla, MAX(pr.tipo_documento) tipo, COUNT(*) n {base} "
            f"GROUP BY pr.sigla ORDER BY n DESC"), p).mappings()]
        por_situacao = [dict(r) for r in c.execute(text(
            f"SELECT COALESCE(pr.situacao,'(sem situação)') situacao, COUNT(*) n {base} "
            f"GROUP BY COALESCE(pr.situacao,'(sem situação)') ORDER BY n DESC"), p).mappings()]
        por_ano = [dict(r) for r in c.execute(text(
            f"SELECT SUBSTR(pr.data_apresentacao,1,4) ano, COUNT(*) n {base} "
            f"GROUP BY SUBSTR(pr.data_apresentacao,1,4) ORDER BY ano DESC"), p).mappings()]
        por_conteudo = [dict(r) for r in c.execute(text(
            f"SELECT COALESCE(pr.tipo_conteudo,'—') conteudo, COUNT(*) n {base} "
            f"GROUP BY COALESCE(pr.tipo_conteudo,'—') ORDER BY n DESC"), p).mappings()]
        p["lim"] = limite
        itens = [dict(r) for r in c.execute(text(
            f"SELECT pr.*, a.principal {base} ORDER BY pr.data_apresentacao DESC, pr.id DESC LIMIT :lim"),
            p).mappings()]
    siglas = sorted({x["sigla"] for x in por_tipo if x["sigla"]})
    return {"total": tot["n"] or 0, "normas": tot["normas"] or 0, "tramitando": tot["tram"] or 0,
            "principal": tot["principal"] or 0, "projetos": tot["projetos"] or 0,
            "por_tipo": por_tipo, "por_situacao": por_situacao, "por_ano": por_ano,
            "por_conteudo": por_conteudo, "itens": itens, "siglas": siglas}


def ranking_proposicoes(inicio=None, fim=None, so_projetos=True, so_autor=True):
    cond, p = ["1=1"], {}
    if so_projetos:
        cond.append(f"pr.sigla IN {SIGLAS_PROJETO}")
    if so_autor:
        cond.append("a.principal = :t")
        p["t"] = True
    if inicio:
        cond.append("pr.data_apresentacao >= :ini")
        p["ini"] = inicio.isoformat()
    if fim:
        cond.append("pr.data_apresentacao <= :fim")
        p["fim"] = fim.isoformat()
    sql = text(f"""SELECT s.codigo, s.nome, s.partido, s.uf, s.foto, s.em_exercicio, COUNT(*) n,
                   SUM(CASE WHEN pr.virou_norma THEN 1 ELSE 0 END) normas
                   FROM autorias a JOIN processos pr ON pr.id = a.processo_id
                   JOIN senadores s ON s.codigo = a.codigo_senador
                   WHERE {' AND '.join(cond)}
                   GROUP BY s.codigo, s.nome, s.partido, s.uf, s.foto, s.em_exercicio
                   ORDER BY n DESC, s.nome""")
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql, p).mappings()]


# ---------------- cota (CEAPS) ----------------

def _meses_ativos(codigo, ano, em_exercicio):
    """Meses do ano em que o senador estava no cargo (aproximação pela atividade registrada)."""
    hoje = date.today()
    ultimo_mes_ano = 12 if ano < hoje.year else hoje.month
    with engine.connect() as c:
        m_desp = [r[0] for r in c.execute(text(
            "SELECT DISTINCT mes FROM despesas WHERE codigo_senador = :c AND ano = :a AND mes IS NOT NULL"),
            {"c": codigo, "a": ano})]
        datas = [r[0] for r in c.execute(
            select(votacoes.c.data).select_from(votos.join(votacoes, votacoes.c.id == votos.c.votacao_id))
            .where(votos.c.codigo_senador == codigo))]
    m_vot = [d.month for d in datas if d and d.year == ano]
    meses = m_desp + m_vot
    if not meses:
        return []
    ini = min(meses)
    if ini <= 3:
        ini = 1  # recesso de janeiro não deixa rastro de votação
    fim = ultimo_mes_ano if em_exercicio else max(meses)
    return list(range(ini, min(fim, ultimo_mes_ano) + 1))


@_memo
def cota_senador(codigo, ano_ini=None, ano_fim=None):
    s = senador(codigo)
    if not s:
        return None
    hoje = date.today()
    ano_ini = ano_ini or 2023
    ano_fim = ano_fim or hoje.year
    with engine.connect() as c:
        p = {"c": codigo, "a1": ano_ini, "a2": ano_fim}
        por_ano_mes = c.execute(text(
            "SELECT ano, mes, SUM(valor) v FROM despesas WHERE codigo_senador = :c "
            "AND ano BETWEEN :a1 AND :a2 GROUP BY ano, mes"), p).all()
        cat = c.execute(text(
            "SELECT ano, tipo, SUM(valor) v FROM despesas WHERE codigo_senador = :c "
            "AND ano BETWEEN :a1 AND :a2 GROUP BY ano, tipo"), p).all()
        forn = [dict(r) for r in c.execute(text(
            "SELECT fornecedor, MAX(cnpj_cpf) cnpj, SUM(valor) total, COUNT(*) n FROM despesas "
            "WHERE codigo_senador = :c AND ano BETWEEN :a1 AND :a2 "
            "GROUP BY fornecedor ORDER BY total DESC LIMIT 25"), p).mappings()]
        recentes = [dict(r) for r in c.execute(text(
            "SELECT data, tipo, fornecedor, cnpj_cpf, documento, valor FROM despesas "
            "WHERE codigo_senador = :c AND ano BETWEEN :a1 AND :a2 "
            "ORDER BY data DESC, id DESC LIMIT 60"), p).mappings()]

    anos = sorted({a for a, _, _ in por_ano_mes}, reverse=True)
    mensal = defaultdict(lambda: [0.0] * 12)
    for a, m, v in por_ano_mes:
        if m:
            mensal[a][m - 1] += v or 0
    resumo_anos = []
    for a in sorted(anos):
        meses = _meses_ativos(codigo, a, bool(s["em_exercicio"]))
        disponivel = cota.teto_periodo(s["uf"], a, meses) if meses else 0
        gasto = round(sum(mensal[a]), 2)
        resumo_anos.append({"ano": a, "meses": len(meses), "disponivel": disponivel, "gasto": gasto,
                            "economia": round(disponivel - gasto, 2) if disponivel else None,
                            "teto_mes": cota.teto_mensal(s["uf"], a, max(meses) if meses else 1),
                            "pct_uso": round(100 * gasto / disponivel, 1) if disponivel else None})
    total = {k: round(sum((r[k] or 0) for r in resumo_anos), 2) for k in ("disponivel", "gasto", "economia")}
    total["pct_uso"] = round(100 * total["gasto"] / total["disponivel"], 1) if total["disponivel"] else None

    categorias = defaultdict(lambda: {"total": 0.0})
    for a, t, v in cat:
        d = categorias[t or "(sem tipo)"]
        d[a] = d.get(a, 0) + (v or 0)
        d["total"] += v or 0
    cats = sorted(({"tipo": k, **v} for k, v in categorias.items()), key=lambda x: -x["total"])
    maior_mes = max((max(v) for v in mensal.values()), default=0)
    return {"senador": s, "anos": anos, "resumo": resumo_anos, "total": total,
            "mensal": {a: mensal[a] for a in anos}, "maior_mes": maior_mes,
            "categorias": cats, "fornecedores": forn, "recentes": recentes,
            "teto_atual": cota.teto_mensal(s["uf"], hoje.year, hoje.month)}


@_memo
def ranking_economia(ano_ini=None, ano_fim=None):
    out = []
    for s in lista_senadores():
        c = cota_senador(s["codigo"], ano_ini, ano_fim)
        if not c or not c["total"]["disponivel"]:
            continue
        t = c["total"]
        out.append({**s, "gasto": t["gasto"], "disponivel": t["disponivel"], "economia": t["economia"],
                    "pct": round(100 * t["economia"] / t["disponivel"], 1)})
    out.sort(key=lambda r: (-r["pct"], r["nome"]))
    return out


def gastos_por_categoria_geral(ano):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT tipo, SUM(valor) total FROM despesas WHERE ano = :a GROUP BY tipo ORDER BY total DESC"),
            {"a": ano}).mappings()]
