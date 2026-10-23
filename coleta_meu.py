"""Coleta do "Meu Parlamentar": agenda pública do Senado, relatorias e radar de notícias.

Roda na atualização diária (depois das etapas principais) e também sob demanda pelo botão
"Atualizar agora". Relatorias e notícias só são buscadas para senadores que têm pelo menos um
usuário vinculado, para não sobrecarregar a API com os 81 senadores.

Os serviços de agenda e relatoria do Senado mudaram de formato mais de uma vez; por isso os
leitores abaixo percorrem o JSON procurando os campos conhecidos (em qualquer grafia) em vez de
depender de um caminho fixo.
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from sqlalchemy import delete, insert, select, update

from db import agenda, engine, noticias, relatorias, senadores, temas_radar, usuario_senadores

log = logging.getLogger("meu_parlamentar")

DIAS_AGENDA_PASSADO, DIAS_AGENDA_FUTURO = 7, 45
DIAS_NOTICIAS = 60
MAX_RELATORIAS_DETALHE = 150

# ---------------- utilitários de leitura ----------------


def _chaves(d, *nomes):
    """Primeiro valor não vazio entre as chaves (sem diferenciar maiúsculas)."""
    if not isinstance(d, dict):
        return None
    baixa = {k.lower(): v for k, v in d.items()}
    for n in nomes:
        v = baixa.get(n.lower())
        if v not in (None, "", [], {}):
            return v
    return None


def _profundo(d, *nomes, nivel=3):
    """Como _chaves, mas também procura em dicionários aninhados (até `nivel`)."""
    v = _chaves(d, *nomes)
    if v is not None or nivel == 0 or not isinstance(d, dict):
        return v
    for filho in d.values():
        if isinstance(filho, dict):
            v = _profundo(filho, *nomes, nivel=nivel - 1)
            if v is not None:
                return v
    return None


def _texto(v):
    if isinstance(v, dict):
        v = _chaves(v, "descricao", "nome", "sigla", "valor")
    return str(v).strip() if v not in (None, "") else None


def _data_hora(v):
    """Aceita '2026-10-23', '2026-10-23T10:00:00', '23/10/2026', '23/10/2026 10:00'."""
    if not v:
        return None, None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", s)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}", (f"{m[4]}:{m[5]}" if m[4] else None)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2})[:h](\d{2}))?", s)
    if m:
        return f"{m[3]}-{m[2]}-{m[1]}", (f"{m[4]}:{m[5]}" if m[4] else None)
    m = re.match(r"(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}", None
    return None, None


def _hora(v):
    if not v:
        return None
    m = re.search(r"(\d{1,2})[:h](\d{2})", str(v))
    return f"{int(m[1]):02d}:{m[2]}" if m else None


def _caminhar(no, aceitar):
    """Percorre o JSON; quando `aceitar(dict)` devolve algo, guarda e não desce mais naquele ramo."""
    if isinstance(no, list):
        for x in no:
            yield from _caminhar(x, aceitar)
    elif isinstance(no, dict):
        r = aceitar(no)
        if r is not None:
            yield r
            return
        for v in no.values():
            yield from _caminhar(v, aceitar)


_PAUTA = re.compile(r"(ementa|identifica|relator|materia|matéria|titulo|título|assunto)", re.I)


def _resumo_pauta(no, limite=1800):
    """Junta os textos de pauta (matérias, ementas, relatores) de um evento."""
    partes = []

    def _rec(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, (dict, list)):
                    _rec(v)
                elif v and _PAUTA.search(k) and isinstance(v, str) and len(v) > 3:
                    partes.append(v.strip())
        elif isinstance(x, list):
            for y in x:
                _rec(y)

    _rec(no)
    vistos, out = set(), []
    for p in partes:
        if p not in vistos:
            vistos.add(p)
            out.append(p)
    return " · ".join(out)[:limite] or None


# ---------------- agenda ----------------

_DATA_EVENTO = ("DataReuniao", "dataReuniao", "DataSessao", "dataSessao", "dataInicio", "DataInicio",
                "dataHoraInicio", "DataHoraInicio", "DataEvento", "dataEvento")


def _evento(d, plenario=False):
    bruto = _chaves(d, *_DATA_EVENTO)
    if bruto is None:
        return None
    dia, hora = _data_hora(bruto)
    if not dia:
        return None
    hora = hora or _hora(_chaves(d, "HoraReuniao", "horaReuniao", "HoraInicioSessao", "horaInicio",
                                 "Hora", "hora"))
    sigla = _texto(_profundo(d, "SiglaComissao", "siglaComissao", "siglaColegiado", "SiglaColegiado",
                             "sigla", nivel=2))
    nome_col = _texto(_profundo(d, "NomeComissao", "nomeComissao", "nomeColegiado", nivel=2))
    tipo = _texto(_profundo(d, "DescricaoTipoSessao", "DescricaoTipoReuniao", "descricaoTipo", "tipo",
                            nivel=2))
    titulo = _texto(_chaves(d, "TituloReuniao", "tituloReuniao", "titulo", "Titulo", "descricao"))
    if plenario and not sigla:
        sigla = "PLEN"
    if not (titulo or sigla or tipo):
        return None
    if not titulo:
        titulo = " · ".join(x for x in (nome_col or ("Sessão do Plenário" if sigla == "PLEN" else sigla), tipo) if x)
    codigo = _chaves(d, "CodigoReuniao", "codigoReuniao", "CodigoSessao", "codigoSessao", "codigo", "id")
    return {
        "origem": "senado",
        "chave": f"{'S' if sigla == 'PLEN' else 'R'}{codigo or ''}-{dia}-{sigla or ''}"[:80],
        "data": dia, "hora": hora, "titulo": titulo[:300],
        "colegiado": (sigla or "")[:30] or None,
        "local": _texto(_chaves(d, "LocalReuniao", "localReuniao", "local", "Local")),
        "situacao": _texto(_profundo(d, "DescricaoSituacaoReuniao", "DescricaoSituacaoSessao", "situacao",
                                     "Situacao", nivel=2)),
        "descricao": _resumo_pauta(d),
    }


def parse_agenda(doc, plenario=False):
    vistos, out = set(), []
    for ev in _caminhar(doc, lambda d: _evento(d, plenario)):
        if ev["chave"] not in vistos:
            vistos.add(ev["chave"])
            out.append(ev)
    return out


def gravar_agenda_senado(conn, eventos, inicio: date, fim: date):
    """Substitui a agenda pública do período pela versão mais recente (reuniões são remarcadas)."""
    conn.execute(delete(agenda).where(agenda.c.origem == "senado", agenda.c.data >= inicio.isoformat(),
                                      agenda.c.data <= fim.isoformat()))
    agora = datetime.now()
    for ev in eventos:
        if inicio.isoformat() <= ev["data"] <= fim.isoformat():
            conn.execute(insert(agenda).values(**ev, codigo_senador=None, criado_em=agora))
    return len(eventos)


def coletar_agenda(hoje=None):
    from collector import get_json
    hoje = hoje or date.today()
    ini, fim = hoje - timedelta(days=DIAS_AGENDA_PASSADO), hoje + timedelta(days=DIAS_AGENDA_FUTURO)
    eventos = []
    try:
        doc = get_json(f"/comissao/agenda/{ini:%Y%m%d}/{fim:%Y%m%d}.json")
        eventos += parse_agenda(doc)
    except Exception as e:
        log.warning("agenda de comissões indisponível: %s", e)
    meses, d = [], ini.replace(day=1)
    while d <= fim:
        meses.append(d)
        d = (d + timedelta(days=32)).replace(day=1)
    plen = []
    for m in meses:
        try:
            plen += parse_agenda(get_json(f"/plenario/agenda/mes/{m:%Y%m%d}.json"), plenario=True)
        except Exception as e:
            log.warning("agenda do Plenário %s indisponível: %s", m, e)
    if not plen:  # serviço alternativo: agenda do dia
        for i in range((fim - hoje).days + 1):
            dia = hoje + timedelta(days=i)
            try:
                plen += [e for e in parse_agenda(get_json(f"/agenda/{dia:%Y%m%d}.json"), plenario=True)]
            except Exception:
                break
    eventos += plen
    if not eventos:
        log.warning("agenda: nenhum evento lido — formato da API pode ter mudado")
        return 0
    with engine.begin() as c:
        n = gravar_agenda_senado(c, eventos, ini, fim)
    log.info("agenda do Senado: %d eventos", n)
    return n


def colegiados_do_senador(codigo):
    """Siglas das comissões em que o senador tem participação atual (do perfil coletado)."""
    import paineis
    pf = paineis.perfil(codigo)
    return {c["sigla"] for c in pf["comissoes"] + pf["cargos"] if c.get("sigla")}


# ---------------- relatorias ----------------

def _relatoria(d):
    desig = _chaves(d, "DataDesignacao", "dataDesignacao", "DataDesignacaoRelator", "dataDesignacaoRelator")
    if desig is None:
        return None
    ident = _texto(_profundo(d, "DescricaoIdentificacaoMateria", "identificacaoMateria", "identificacao",
                             "descricaoIdentificacao", nivel=3))
    if not ident:
        sig = _texto(_profundo(d, "SiglaSubtipoMateria", "siglaMateria", "sigla", nivel=3))
        num = _texto(_profundo(d, "NumeroMateria", "numeroMateria", "numero", nivel=3))
        ano = _texto(_profundo(d, "AnoMateria", "anoMateria", "ano", nivel=3))
        if sig and num and ano:
            ident = f"{sig} {int(num) if str(num).isdigit() else num}/{ano}"
    if not ident:
        return None
    col = _texto(_profundo(d, "SiglaComissao", "siglaComissao", "siglaColegiado", "SiglaColegiado", nivel=3))
    dd, _ = _data_hora(desig)
    dfim, _ = _data_hora(_chaves(d, "DataDestituicao", "dataDestituicao", "DataFimRelatoria", "dataFim"))
    cm = _profundo(d, "CodigoMateria", "codigoMateria", nivel=3)
    situ = _texto(_profundo(d, "DescricaoSituacao", "situacaoAtual", "SituacaoAtual", nivel=3))
    dsit, _ = _data_hora(_profundo(d, "DataSituacao", "dataSituacaoAtual", "dataSituacao", nivel=3))
    return {
        "chave": f"{ident}|{col or ''}|{dd or ''}"[:120], "identificacao": ident[:60],
        "codigo_materia": int(cm) if str(cm or "").isdigit() else None,
        "ementa": _texto(_profundo(d, "EmentaMateria", "ementa", "Ementa", nivel=3)),
        "colegiado": (col or "")[:30] or None, "data_designacao": dd, "data_destituicao": dfim,
        "situacao": situ, "data_situacao": dsit,
    }


def parse_relatorias(doc):
    vistos, out = set(), []
    for r in _caminhar(doc, _relatoria):
        if r["chave"] not in vistos:
            vistos.add(r["chave"])
            out.append(r)
    return out


def gravar_relatorias(conn, codigo, itens):
    agora = datetime.now()
    for r in itens:
        existe = conn.execute(select(relatorias.c.chave).where(
            relatorias.c.codigo_senador == codigo, relatorias.c.chave == r["chave"])).first()
        valores = {k: v for k, v in r.items() if v is not None or k in ("data_destituicao",)}
        if existe:
            conn.execute(update(relatorias).where(relatorias.c.codigo_senador == codigo,
                                                  relatorias.c.chave == r["chave"])
                         .values(**valores, atualizado=agora))
        else:
            conn.execute(insert(relatorias).values(codigo_senador=codigo, **r, atualizado=agora))


def coletar_relatorias(codigo, inicio_legislatura="2023-02-01"):
    from collector import get_json
    itens = []
    for caminho in (f"/processo/relatoria?codigoParlamentar={codigo}&v=1",
                    f"/senador/{codigo}/relatorias.json"):
        try:
            itens = parse_relatorias(get_json(caminho))
        except Exception as e:
            log.warning("relatorias %s (%s): %s", codigo, caminho, e)
            itens = []
        if itens:
            break
    itens = [r for r in itens if (r["data_designacao"] or "9999") >= inicio_legislatura]
    # situação atual da matéria, quando o serviço de relatorias não traz
    for r in [x for x in itens if not x["situacao"] and x["codigo_materia"]][:MAX_RELATORIAS_DETALHE]:
        try:
            proc = get_json(f"/processo?codigoMateria={r['codigo_materia']}&v=1")
        except Exception:
            continue
        p = (proc or [None])[0] if isinstance(proc, list) else proc
        if isinstance(p, dict):
            r["situacao"] = p.get("situacaoAtual")
            r["data_situacao"] = (p.get("dataSituacaoAtual") or "")[:10] or None
            r["ementa"] = r["ementa"] or p.get("ementa")
            r["url"] = p.get("urlDocumento")
    with engine.begin() as c:
        gravar_relatorias(c, codigo, itens)
    log.info("relatorias senador %s: %d", codigo, len(itens))
    return len(itens)


# ---------------- radar de notícias ----------------

def url_noticias(consulta):
    return ("https://news.google.com/rss/search?q=" + quote(f"{consulta} when:7d")
            + "&hl=pt-BR&gl=BR&ceid=BR:pt-419")


def parse_rss(xml_texto):
    out = []
    try:
        raiz = ET.fromstring(xml_texto)
    except ET.ParseError:
        return out
    for it in raiz.iter("item"):
        titulo = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not titulo or not link:
            continue
        fonte = (it.findtext("source") or "").strip() or None
        if fonte and titulo.endswith(f" - {fonte}"):
            titulo = titulo[: -len(fonte) - 3].strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).replace(tzinfo=None)
        except (TypeError, ValueError):
            pub = None
        out.append({"titulo": titulo[:500], "link": link[:1000], "fonte": fonte, "publicado": pub})
    return out


def consultas_radar(codigo):
    with engine.connect() as c:
        s = c.execute(select(senadores.c.nome).where(senadores.c.codigo == codigo)).first()
        temas = [r[0] for r in c.execute(select(temas_radar.c.termo).where(
            temas_radar.c.codigo_senador == codigo).order_by(temas_radar.c.termo))]
    out = []
    if s:
        out.append((s[0], f'"{s[0]}" senador'))
    out += [(t, t) for t in temas]
    return out


def gravar_noticias(conn, codigo, termo, itens, hoje=None):
    agora = datetime.now()
    conhecidos = {r[0] for r in conn.execute(select(noticias.c.link).where(noticias.c.codigo_senador == codigo))}
    n = 0
    for it in itens:
        if it["link"] in conhecidos:
            continue
        conhecidos.add(it["link"])
        conn.execute(insert(noticias).values(codigo_senador=codigo, termo=termo, coletado_em=agora, **it))
        n += 1
    corte = datetime.combine((hoje or date.today()) - timedelta(days=DIAS_NOTICIAS), datetime.min.time())
    conn.execute(delete(noticias).where(noticias.c.codigo_senador == codigo, noticias.c.coletado_em < corte))
    return n


def coletar_noticias(codigo):
    import requests
    total = 0
    for termo, consulta in consultas_radar(codigo):
        try:
            r = requests.get(url_noticias(consulta), timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 (painel-senado)"})
            if r.status_code != 200:
                log.warning("radar %s/%s: HTTP %s", codigo, termo, r.status_code)
                continue
            itens = parse_rss(r.text)
        except Exception as e:
            log.warning("radar %s/%s: %s", codigo, termo, e)
            continue
        with engine.begin() as c:
            total += gravar_noticias(c, codigo, termo, itens[:40])
    log.info("radar senador %s: %d notícias novas", codigo, total)
    return total


# ---------------- orquestração ----------------

def senadores_vinculados():
    with engine.connect() as c:
        return sorted({r[0] for r in c.execute(select(usuario_senadores.c.codigo_senador))})


def executar(codigos=None, agenda_tambem=True):
    """Etapa diária do Meu Parlamentar (também usada pelo botão 'Atualizar agora')."""
    if agenda_tambem:
        try:
            coletar_agenda()
        except Exception:
            log.exception("agenda")
    for cod in (codigos if codigos is not None else senadores_vinculados()):
        for fn in (coletar_relatorias, coletar_noticias):
            try:
                fn(cod)
            except Exception:
                log.exception("%s do senador %s", fn.__name__, cod)
