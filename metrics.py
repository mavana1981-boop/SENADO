"""Cálculos do painel: governismo, oposicionismo, participação e fidelidade partidária.

Regras (iguais às do painel da Câmara):
- Alinhamento com uma liderança (Governo/Oposição) = votos do senador iguais à orientação
  ÷ votos SIM/NÃO do senador nas votações em que essa liderança orientou SIM ou NÃO.
  Orientação LIVRE, abstenção, obstrução e voto secreto ficam fora da conta.
- "Orientações opostas": votações em que Governo e Oposição orientaram em sentidos
  contrários (um SIM, outro NÃO). É o recorte que mais diferencia governistas de opositores.
- Participação = votações em que o senador registrou voto (inclui secretas)
  ÷ votações nominais realizadas entre o primeiro e o último voto dele no período.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import DateTime, bindparam, select
from sqlalchemy import text as _text

from db import engine, meta, senadores


@dataclass
class Filtros:
    inicio: date | None = None
    fim: date | None = None
    partido: str = ""
    uf: str = ""
    sigla: str = ""          # tipo de matéria: PL, PEC, MPV...
    opostas: bool = True     # só votações com orientações opostas Governo x Oposição

    @classmethod
    def from_args(cls, a):
        def _d(s):
            try:
                return date.fromisoformat(s) if s else None
            except ValueError:
                return None
        return cls(inicio=_d(a.get("inicio")), fim=_d(a.get("fim")),
                   partido=(a.get("partido") or "").upper(), uf=(a.get("uf") or "").upper(),
                   sigla=(a.get("sigla") or "").upper(),
                   opostas=cls._opostas(a))

    @staticmethod
    def _opostas(a):
        # o formulário manda um campo oculto "0" e, se marcado, também "1"
        vals = a.getlist("opostas") if hasattr(a, "getlist") else [a.get("opostas")]
        vals = [v for v in vals if v is not None]
        return ("1" in vals) if vals else True

    def where_votacoes(self, alias="vt"):
        cond, p = ["1=1"], {}
        if self.inicio:
            cond.append(f"{alias}.data >= :inicio")
            p["inicio"] = datetime.combine(self.inicio, datetime.min.time())
        if self.fim:
            cond.append(f"{alias}.data < :fim")
            p["fim"] = datetime.combine(self.fim + timedelta(days=1), datetime.min.time())
        if self.sigla:
            cond.append(f"{alias}.sigla_materia = :sigla")
            p["sigla"] = self.sigla
        if self.opostas:
            cond.append(f"""EXISTS (SELECT 1 FROM orientacoes g JOIN orientacoes o
                              ON o.votacao_id = g.votacao_id
                            WHERE g.votacao_id = {alias}.id AND g.lideranca = 'Governo'
                              AND o.lideranca = 'Oposição'
                              AND g.voto IN ('SIM','NÃO') AND o.voto IN ('SIM','NÃO')
                              AND g.voto <> o.voto)""")
        return " AND ".join(cond), p


def text(sql):
    """text() com tipos de data explícitos (funciona igual em SQLite e PostgreSQL)."""
    q = _text(sql)
    tipos = [bindparam(k, type_=DateTime) for k in ("inicio", "fim") if f":{k}" in sql]
    return q.bindparams(*tipos) if tipos else q


def _cadastro():
    with engine.connect() as c:
        rows = c.execute(select(senadores)).mappings().all()
    return {r["codigo"]: dict(r) for r in rows}


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def _enriquecer(linhas, cad, f: Filtros):
    """Junta cadastro (foto, partido atual, UF) e aplica filtros de partido/UF."""
    saida = []
    for r in linhas:
        s = cad.get(r["codigo"]) if r.get("codigo") else None
        item = dict(r)
        item["nome"] = (s or {}).get("nome") or r["nome"]
        item["partido"] = (s or {}).get("partido") or r.get("partido_voto") or ""
        item["uf"] = (s or {}).get("uf") or r.get("uf") or ""
        item["foto"] = (s or {}).get("foto")
        item["em_exercicio"] = bool(s.get("em_exercicio")) if s else None  # None = fora do cadastro
        if f.partido and item["partido"] != f.partido:
            continue
        if f.uf and item["uf"] != f.uf:
            continue
        saida.append(item)
    return saida


def alinhamento(lideranca: str, f: Filtros, minimo: int = 1):
    where, p = f.where_votacoes()
    p["lid"] = lideranca
    sql = text(f"""
        SELECT v.nome_norm, MAX(v.nome) AS nome, MAX(v.codigo_senador) AS codigo,
               MAX(v.uf) AS uf, MAX(v.partido) AS partido_voto,
               SUM(CASE WHEN v.voto = o.voto THEN 1 ELSE 0 END) AS seguiu,
               SUM(CASE WHEN v.voto <> o.voto THEN 1 ELSE 0 END) AS contra
        FROM votos v
        JOIN orientacoes o ON o.votacao_id = v.votacao_id AND o.lideranca = :lid
        JOIN votacoes vt ON vt.id = v.votacao_id
        WHERE o.voto IN ('SIM','NÃO') AND v.voto IN ('SIM','NÃO') AND {where}
        GROUP BY v.nome_norm""")
    with engine.connect() as c:
        linhas = [dict(r) for r in c.execute(sql, p).mappings()]
    for r in linhas:
        r["total"] = r["seguiu"] + r["contra"]
        r["pct"] = _pct(r["seguiu"], r["total"])
    linhas = [r for r in linhas if r["total"] >= minimo]
    out = _enriquecer(linhas, _cadastro(), f)
    out.sort(key=lambda r: (-(r["pct"] or 0), -r["total"], r["nome"]))
    return out


def alinhamento_agrupado(campo: str, f: Filtros):
    """% de votos seguindo Governo e Oposição, agregado por 'partido' (na data do voto) ou 'uf'."""
    assert campo in ("partido", "uf")
    where, p = f.where_votacoes()
    sql = text(f"""
        SELECT v.{campo} AS chave, o.lideranca AS lid,
               SUM(CASE WHEN v.voto = o.voto THEN 1 ELSE 0 END) AS seguiu,
               COUNT(*) AS total
        FROM votos v
        JOIN orientacoes o ON o.votacao_id = v.votacao_id
             AND o.lideranca IN ('Governo','Oposição')
        JOIN votacoes vt ON vt.id = v.votacao_id
        WHERE o.voto IN ('SIM','NÃO') AND v.voto IN ('SIM','NÃO') AND {where}
        GROUP BY v.{campo}, o.lideranca""")
    agg = {}
    with engine.connect() as c:
        for r in c.execute(sql, p).mappings():
            if not r["chave"]:
                continue
            d = agg.setdefault(r["chave"], {"chave": r["chave"]})
            k = "gov" if r["lid"] == "Governo" else "opo"
            d[k] = _pct(r["seguiu"], r["total"])
            d[k + "_n"] = r["total"]
    out = list(agg.values())
    out.sort(key=lambda d: (-(d.get("opo") or 0), d["chave"]))
    return out


def resumo(f: Filtros):
    base = Filtros(f.inicio, f.fim, "", "", f.sigla, opostas=False)
    where, p = base.where_votacoes()
    sql = text(f"""
        SELECT COUNT(*) AS total,
          SUM(CASE WHEN g.voto IN ('SIM','NÃO') AND o.voto IN ('SIM','NÃO') AND g.voto <> o.voto
                   THEN 1 ELSE 0 END) AS opostas,
          SUM(CASE WHEN g.voto IN ('SIM','NÃO') AND o.voto IN ('SIM','NÃO') AND g.voto = o.voto
                   THEN 1 ELSE 0 END) AS iguais
        FROM votacoes vt
        JOIN orientacoes g ON g.votacao_id = vt.id AND g.lideranca = 'Governo'
        JOIN orientacoes o ON o.votacao_id = vt.id AND o.lideranca = 'Oposição'
        WHERE {where}""")
    sql_tot = text(f"SELECT COUNT(*) AS n, SUM(CASE WHEN vt.secreta THEN 1 ELSE 0 END) AS sec "
                   f"FROM votacoes vt WHERE {where}")
    with engine.connect() as c:
        r = c.execute(sql, p).mappings().first()
        t = c.execute(sql_tot, p).mappings().first()
    return {"votacoes": t["n"] or 0, "secretas": t["sec"] or 0,
            "com_ambas": r["total"] or 0, "opostas": r["opostas"] or 0,
            "nao_opostas": r["iguais"] or 0}


def votacoes_orientadas(f: Filtros, limite: int = 200):
    where, p = f.where_votacoes()
    p["lim"] = limite
    sql = text(f"""
        SELECT vt.id, vt.data, vt.sigla_materia, vt.numero_materia, vt.ano_materia,
               vt.descricao_materia, vt.dispositivo, vt.qtd_sim, vt.qtd_nao,
               g.voto AS gov, o.voto AS opo
        FROM votacoes vt
        LEFT JOIN orientacoes g ON g.votacao_id = vt.id AND g.lideranca = 'Governo'
        LEFT JOIN orientacoes o ON o.votacao_id = vt.id AND o.lideranca = 'Oposição'
        WHERE (g.voto IS NOT NULL OR o.voto IS NOT NULL) AND {where}
        ORDER BY vt.data DESC LIMIT :lim""")
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql, p).mappings()]


def participacao(f: Filtros):
    """Ignora o filtro de orientações opostas: participação vale para todas as votações."""
    base = Filtros(f.inicio, f.fim, f.partido, f.uf, f.sigla, opostas=False)
    where, p = base.where_votacoes()
    sql = text(f"""
        SELECT v.nome_norm, MAX(v.nome) AS nome, MAX(v.codigo_senador) AS codigo,
               MAX(v.uf) AS uf, MAX(v.partido) AS partido_voto,
               MIN(vt.data) AS primeiro, MAX(vt.data) AS ultimo,
               SUM(CASE WHEN v.voto IN ('SIM','NÃO','ABSTENÇÃO','OBSTRUÇÃO','SECRETO')
                        THEN 1 ELSE 0 END) AS votou
        FROM votos v JOIN votacoes vt ON vt.id = v.votacao_id
        WHERE {where}
        GROUP BY v.nome_norm""")
    sql_datas = text(f"SELECT vt.data FROM votacoes vt WHERE {where} ORDER BY vt.data")
    with engine.connect() as c:
        linhas = [dict(r) for r in c.execute(sql, p).mappings()]
        datas = [r[0] for r in c.execute(sql_datas, p)]
    import bisect
    for r in linhas:
        i = bisect.bisect_left(datas, r["primeiro"])
        j = bisect.bisect_right(datas, r["ultimo"])
        r["realizadas"] = max(j - i, r["votou"])
        r["pct"] = _pct(r["votou"], r["realizadas"])
    out = _enriquecer(linhas, _cadastro(), f)
    out.sort(key=lambda r: (-(r["pct"] or 0), -r["votou"], r["nome"]))
    return out


def fidelidade(f: Filtros):
    """% de votos iguais à orientação do próprio partido (na data do voto)."""
    where, p = f.where_votacoes()
    sql = text(f"""
        SELECT v.nome_norm, MAX(v.nome) AS nome, MAX(v.codigo_senador) AS codigo,
               MAX(v.uf) AS uf, MAX(v.partido) AS partido_voto,
               SUM(CASE WHEN v.voto = o.voto THEN 1 ELSE 0 END) AS seguiu,
               COUNT(*) AS total
        FROM votos v
        JOIN orientacoes o ON o.votacao_id = v.votacao_id AND o.lideranca = v.partido
        JOIN votacoes vt ON vt.id = v.votacao_id
        WHERE o.voto IN ('SIM','NÃO') AND v.voto IN ('SIM','NÃO') AND {where}
        GROUP BY v.nome_norm""")
    with engine.connect() as c:
        linhas = [dict(r) for r in c.execute(sql, p).mappings()]
    for r in linhas:
        r["pct"] = _pct(r["seguiu"], r["total"])
    out = _enriquecer(linhas, _cadastro(), f)
    out.sort(key=lambda r: (-(r["pct"] or 0), -r["total"], r["nome"]))
    return out


def opcoes_filtro():
    with engine.connect() as c:
        partidos = [r[0] for r in c.execute(text(
            "SELECT DISTINCT partido FROM senadores WHERE partido IS NOT NULL ORDER BY partido"))]
        ufs = [r[0] for r in c.execute(text(
            "SELECT DISTINCT uf FROM senadores WHERE uf IS NOT NULL ORDER BY uf"))]
        siglas = [r[0] for r in c.execute(text(
            "SELECT sigla_materia, COUNT(*) n FROM votacoes WHERE sigla_materia IS NOT NULL "
            "GROUP BY sigla_materia ORDER BY n DESC"))]
        ult = c.execute(select(meta.c.valor).where(meta.c.chave == "ultima_coleta")).scalar()
    return {"partidos": partidos, "ufs": ufs, "siglas": siglas, "ultima_coleta": ult}
