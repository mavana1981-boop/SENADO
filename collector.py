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
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests
from sqlalchemy import delete, insert, select, update

from db import (autorias, despesas, engine, init_db, meta, notificacoes, norm_nome, norm_partido,
                norm_voto, perfis, processos,
                orientacoes, senadores, votacoes, votos)

BASE = os.environ.get("SENADO_API", "https://legis.senado.leg.br/dadosabertos")
ADM = os.environ.get("SENADO_ADM_API", "https://adm.senado.gov.br/adm-dadosabertos/api/v1")
CEAPS_CSV = "https://www.senado.leg.br/transparencia/LAI/verba/despesa_ceaps_{ano}.csv"
LEGISLATURA = int(os.environ.get("LEGISLATURA", "57"))
INICIO_LEGISLATURA = os.environ.get("INICIO_LEGISLATURA", "2023-02-01")
PAUSA = 0.25  # a API aceita até 10 req/s; ficamos bem abaixo

log = logging.getLogger("coletor")
sess = requests.Session()
sess.headers.update({"Accept": "application/json",
                     "User-Agent": "painel-senado-oposicao/1.0"})


# ---------------- HTTP ----------------

def get_json(caminho: str, tentativas: int = 7):
    url = caminho if caminho.startswith("http") else f"{BASE}{caminho}"
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
            espera = min(60, 5 * 2 ** i)  # 5, 10, 20, 40, 60, 60 s
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
        # exercicio=S: só quem efetivamente assumiu (sem isso vêm todos os suplentes)
        doc_leg = get_json(f"/senador/lista/legislatura/{LEGISLATURA}.json?exercicio=S")
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


def _meses_pendentes(conn=None):
    def _ler(c):
        v = c.execute(select(meta.c.valor).where(meta.c.chave == "votacoes_meses_pendentes")).scalar()
        return set(filter(None, (v or "").split(",")))
    if conn is not None:
        return _ler(conn)
    with engine.connect() as c:
        return _ler(c)


def _salvar_pendentes(conn, pendentes):
    conn.execute(delete(meta).where(meta.c.chave == "votacoes_meses_pendentes"))
    if pendentes:
        conn.execute(insert(meta).values(chave="votacoes_meses_pendentes", valor=",".join(sorted(pendentes))))


def coletar_votacoes(desde: date, ate: date) -> int:
    """Baixa mês a mês. Um mês que falhar não interrompe os seguintes: fica anotado como
    pendente e é refeito automaticamente na próxima coleta."""
    pendentes = _meses_pendentes()
    meses = {(ini.replace(day=1)): (ini, fim) for ini, fim in _meses(desde, ate)}
    for p in pendentes:  # inclui meses que falharam em coletas anteriores
        ini = date.fromisoformat(p)
        fim = min((ini + timedelta(days=32)).replace(day=1) - timedelta(days=1), ate)
        meses.setdefault(ini, (ini, fim))
    total, falhas = 0, set()
    for chave in sorted(meses):
        ini, fim = meses[chave]
        try:
            doc = get_json(f"/plenario/votacao/orientacaoBancada/{ini:%Y%m%d}/{fim:%Y%m%d}.json")
        except Exception as e:
            log.error("votações de %s a %s não baixadas (%s) — ficam para a próxima coleta", ini, fim, e)
            falhas.add(chave.isoformat())
            continue
        with engine.begin() as c:
            n = gravar_votacoes(doc, c)
        log.info("%s a %s: %d votações", ini, fim, n)
        total += n
    with engine.begin() as c:
        resolvidos = {k.isoformat() for k in meses} - falhas
        _salvar_pendentes(c, (pendentes - resolvidos) | falhas)
    return total


def inicio_atualizacao_votacoes(hoje=None) -> date:
    """Coleta diária: desde a última votação no banco (com folga de 30 dias), nunca menos de 45 dias.
    Assim qualquer buraco deixado por uma coleta interrompida é preenchido sozinho."""
    from sqlalchemy import func
    hoje = hoje or date.today()
    with engine.connect() as c:
        ultima = c.execute(select(func.max(votacoes.c.data))).scalar()
    padrao = hoje - timedelta(days=45)
    if not ultima:
        return date.fromisoformat(INICIO_LEGISLATURA)
    if isinstance(ultima, str):
        ultima = datetime.fromisoformat(ultima)
    return min(padrao, ultima.date() - timedelta(days=30))


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


def limpar_senadores_sem_atividade(conn):
    """Remove do cadastro quem não está em exercício e não tem voto nem despesa registrados
    (ex.: suplentes que nunca assumiram)."""
    from sqlalchemy import exists
    sem_voto = ~exists().where(votos.c.codigo_senador == senadores.c.codigo)
    sem_desp = ~exists().where(despesas.c.codigo_senador == senadores.c.codigo)
    alvo = [r[0] for r in conn.execute(select(senadores.c.codigo).where(
        senadores.c.em_exercicio.is_not(True), sem_voto, sem_desp))]
    if alvo:
        conn.execute(delete(autorias).where(autorias.c.codigo_senador.in_(alvo)))
        conn.execute(delete(perfis).where(perfis.c.codigo.in_(alvo)))
        conn.execute(delete(senadores).where(senadores.c.codigo.in_(alvo)))
        # proposições que ficaram sem nenhum autor no cadastro
        conn.execute(delete(processos).where(~exists().where(autorias.c.processo_id == processos.c.id)))
    log.info("senadores sem atividade removidos do cadastro: %d", len(alvo))
    return len(alvo)


def marcar_coleta(conn):
    agora = datetime.now().isoformat(timespec="seconds")
    conn.execute(delete(meta).where(meta.c.chave == "ultima_coleta"))
    conn.execute(insert(meta).values(chave="ultima_coleta", valor=agora))


# ---------------- CEAPS (cota parlamentar) ----------------

def _num_br(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_ceaps_adm(itens):
    """Formato da API administrativa (adm.senado.gov.br): já traz codSenador."""
    out = []
    for d in itens or []:
        if d.get("id") is None:
            continue
        out.append({
            "id": int(d["id"]), "ano": d.get("ano"), "mes": d.get("mes"),
            "data": (d.get("data") or "")[:10] or None,
            "codigo_senador": d.get("codSenador"),
            "nome": (d.get("nomeSenador") or "").strip(),
            "tipo": (d.get("tipoDespesa") or "").strip(),
            "tipo_documento": d.get("tipoDocumento"),
            "fornecedor": (d.get("fornecedor") or "").strip(),
            "cnpj_cpf": d.get("cpfCnpj"), "documento": d.get("documento"),
            "detalhamento": d.get("detalhamento"),
            "valor": _num_br(d.get("valorReembolsado")),
        })
    return out


def parse_ceaps_csv(texto: str, ano: int, nomes_para_codigo: dict):
    """Plano B: CSV da Transparência (latin-1, ';', 1ª linha é metadado, sem código do senador)."""
    import csv
    import io
    linhas = texto.splitlines()[1:]
    out = []
    for i, r in enumerate(csv.DictReader(io.StringIO("\n".join(linhas)), delimiter=";")):
        nome = (r.get("SENADOR") or "").strip()
        data = (r.get("DATA") or "").strip()
        if "/" in data:  # dd/mm/aaaa
            dd, mm, aa = (data.split("/") + ["", "", ""])[:3]
            data = f"{aa}-{mm.zfill(2)}-{dd.zfill(2)}" if aa else None
        cod_doc = r.get("COD_DOCUMENTO") or ""
        out.append({
            "id": int(cod_doc) if cod_doc.strip().isdigit() else ano * 10_000_000 + i,
            "ano": int(r.get("ANO") or ano), "mes": int(r.get("MES") or 0) or None,
            "data": data or None,
            "codigo_senador": nomes_para_codigo.get(norm_nome(nome)),
            "nome": nome, "tipo": (r.get("TIPO_DESPESA") or "").strip(),
            "tipo_documento": None, "fornecedor": (r.get("FORNECEDOR") or "").strip(),
            "cnpj_cpf": r.get("CNPJ_CPF"), "documento": r.get("DOCUMENTO"),
            "detalhamento": r.get("DETALHAMENTO"),
            "valor": _num_br(r.get("VALOR_REEMBOLSADO")),
        })
    return out


def gravar_ceaps(conn, ano: int, linhas):
    """Substitui o ano inteiro (o Senado revisa lançamentos retroativamente)."""
    vistos, unicos = set(), []
    for r in linhas:
        if r["id"] in vistos:
            continue
        vistos.add(r["id"])
        unicos.append(r)
    conn.execute(delete(despesas).where(despesas.c.ano == ano))
    for i in range(0, len(unicos), 2000):
        conn.execute(insert(despesas), unicos[i:i + 2000])
    return len(unicos)


def coletar_ceaps(anos):
    for ano in anos:
        linhas = None
        try:
            itens = get_json(f"{ADM}/senadores/despesas_ceaps/{ano}")
            if isinstance(itens, dict):  # alguns serviços vêm envelopados {data: [...]}
                itens = itens.get("data") or itens.get("dados") or []
            linhas = parse_ceaps_adm(itens)
        except Exception as e:
            log.warning("API administrativa falhou para CEAPS %s (%s); tentando CSV", ano, e)
        if not linhas:
            try:
                r = sess.get(CEAPS_CSV.format(ano=ano), timeout=120,
                             headers={"Accept": "text/csv,*/*"})
                if r.status_code == 200:
                    with engine.connect() as c:
                        mapa = {n: cod for cod, n in c.execute(
                            select(senadores.c.codigo, senadores.c.nome_norm))}
                    linhas = parse_ceaps_csv(r.content.decode("latin-1"), ano, mapa)
            except requests.RequestException as e:
                log.warning("CSV da CEAPS %s indisponível: %s", ano, e)
        if linhas:
            with engine.begin() as c:
                n = gravar_ceaps(c, ano, linhas)
            log.info("CEAPS %s: %d lançamentos", ano, n)


# ---------------- Proposições (processos) ----------------

def _primeiro_autor(autoria: str) -> str:
    primeiro = (autoria or "").split(",")[0]
    primeiro = primeiro.split("(")[0]
    for prefixo in ("Senadora ", "Senador ", "Deputada ", "Deputado "):
        if primeiro.strip().startswith(prefixo):
            primeiro = primeiro.strip()[len(prefixo):]
    return norm_nome(primeiro)


def _virou_norma(p: dict) -> bool:
    sit = norm_nome(p.get("situacaoAtual") or "")
    return ("norma juridica" in sit) or bool(p.get("normaGerada")) or ("transformad" in sit)


def parse_processo(p: dict) -> dict:
    ident = (p.get("identificacao") or "").strip()      # "PL 1234/2025"
    sigla, numero, ano = "", "", None
    if " " in ident:
        sigla, resto = ident.split(" ", 1)
        numero, _, a = resto.partition("/")
        ano = int(a) if a.strip().isdigit() else None
    tram = p.get("tramitando")
    return {
        "id": int(p["id"]), "identificacao": ident, "sigla": sigla, "numero": numero.strip(),
        "ano": ano, "codigo_materia": p.get("codigoMateria"), "ementa": p.get("ementa"),
        "autoria": p.get("autoria"), "tipo_documento": p.get("tipoDocumento"),
        "tipo_conteudo": p.get("tipoConteudo"), "situacao": p.get("situacaoAtual"),
        "data_apresentacao": (p.get("dataApresentacao") or "")[:10] or None,
        "data_situacao": (p.get("dataSituacaoAtual") or "")[:10] or None,
        "tramitando": (str(tram).lower() in ("sim", "s", "true")) if tram is not None else None,
        "virou_norma": _virou_norma(p), "url": p.get("urlDocumento"),
    }


def gravar_processos(conn, codigo_senador: int, nome_norm: str, itens, notificar=False):
    """Grava as proposições do senador. Com notificar=True (coletas depois da carga inicial),
    registra em `notificacoes` cada proposição que mudou de situação ou que apareceu."""
    n = 0
    agora = datetime.now()
    ja_tinha = notificar and conn.execute(select(autorias.c.processo_id).where(
        autorias.c.codigo_senador == codigo_senador).limit(1)).first() is not None
    for p in itens or []:
        if p.get("id") is None:
            continue
        linha = parse_processo(p)
        antes = conn.execute(select(processos.c.situacao, processos.c.data_situacao)
                             .where(processos.c.id == linha["id"])).first()
        if antes:
            conn.execute(update(processos).where(processos.c.id == linha["id"]).values(**linha))
        else:
            conn.execute(insert(processos).values(**linha))
        vinculo = conn.execute(select(autorias.c.processo_id).where(
            autorias.c.processo_id == linha["id"],
            autorias.c.codigo_senador == codigo_senador)).first()
        if notificar:
            if antes and linha["situacao"] and (
                    (antes.situacao or "") != (linha["situacao"] or "")
                    or (antes.data_situacao or "") != (linha["data_situacao"] or "")):
                # avisa todos os autores já conhecidos (a proposição só muda uma vez no banco)
                autores = {r[0] for r in conn.execute(select(autorias.c.codigo_senador).where(
                    autorias.c.processo_id == linha["id"]))} | {codigo_senador}
                for cod in autores:
                    conn.execute(insert(notificacoes).values(
                        codigo_senador=cod, processo_id=linha["id"], identificacao=linha["identificacao"],
                        tipo="situacao", situacao_anterior=antes.situacao, situacao_nova=linha["situacao"],
                        data_situacao=linha["data_situacao"], detectado_em=agora))
            elif not vinculo and ja_tinha:
                conn.execute(insert(notificacoes).values(
                    codigo_senador=codigo_senador, processo_id=linha["id"],
                    identificacao=linha["identificacao"], tipo="nova", situacao_anterior=None,
                    situacao_nova=linha["situacao"], data_situacao=linha["data_apresentacao"],
                    detectado_em=agora))
        principal = bool(nome_norm) and _primeiro_autor(linha["autoria"]) == nome_norm
        conn.execute(delete(autorias).where(autorias.c.processo_id == linha["id"],
                                            autorias.c.codigo_senador == codigo_senador))
        conn.execute(insert(autorias).values(processo_id=linha["id"],
                                             codigo_senador=codigo_senador, principal=principal))
        n += 1
    return n


DIAS_NOTIFICACOES = 180   # notificações mais antigas são apagadas


def limpar_notificacoes(conn, hoje=None):
    limite = datetime.combine((hoje or date.today()) - timedelta(days=DIAS_NOTIFICACOES), datetime.min.time())
    conn.execute(delete(notificacoes).where(notificacoes.c.detectado_em < limite))


def coletar_processos(codigos=None):
    with engine.connect() as c:
        q = select(senadores.c.codigo, senadores.c.nome_norm)
        if codigos:
            q = q.where(senadores.c.codigo.in_(codigos))
        alvo = c.execute(q).all()
    # na carga inicial tudo é "novo": só notifica depois que as proposições já foram coletadas uma vez
    notificar = _etapa_concluida("proposições")
    for cod, nn in alvo:
        try:
            itens = get_json(f"/processo?codigoParlamentarAutor={cod}&v=1")
        except Exception as e:
            log.warning("processos do senador %s: %s", cod, e)
            continue
        with engine.begin() as c:
            n = gravar_processos(c, cod, nn, itens if isinstance(itens, list) else [], notificar)
        log.info("processos senador %s: %d", cod, n)
    with engine.begin() as c:
        limpar_notificacoes(c)


# ---------------- Perfil (detalhe, comissões, cargos) ----------------

def gravar_perfil(conn, codigo: int, detalhe, comissoes, cargos):
    conn.execute(delete(perfis).where(perfis.c.codigo == codigo))
    conn.execute(insert(perfis).values(
        codigo=codigo, detalhe=json.dumps(detalhe or {}, ensure_ascii=False),
        comissoes=json.dumps(comissoes or {}, ensure_ascii=False),
        cargos=json.dumps(cargos or {}, ensure_ascii=False), atualizado=datetime.now()))


def coletar_perfis(codigos=None):
    with engine.connect() as c:
        q = select(senadores.c.codigo)
        if codigos:
            q = q.where(senadores.c.codigo.in_(codigos))
        alvo = [r[0] for r in c.execute(q)]
    for cod in alvo:
        try:
            det = get_json(f"/senador/{cod}.json")
            com = get_json(f"/senador/{cod}/comissoes.json")
            car = get_json(f"/senador/{cod}/cargos.json")
        except Exception as e:
            log.warning("perfil %s: %s", cod, e)
            continue
        with engine.begin() as c:
            gravar_perfil(c, cod, det, com, car)
    log.info("perfis atualizados: %d", len(alvo))


def _passo(nome, fn, *a):
    """Uma etapa que falha não derruba as outras. Registra a conclusão em `meta`."""
    try:
        fn(*a)
    except Exception:
        log.exception("etapa %s falhou", nome)
        return False
    with engine.begin() as c:
        chave = f"etapa_ok:{nome}"
        c.execute(delete(meta).where(meta.c.chave == chave))
        c.execute(insert(meta).values(chave=chave, valor=datetime.now().isoformat(timespec="seconds")))
    return True


def _etapa_concluida(nome):
    with engine.connect() as c:
        return c.execute(select(meta.c.valor).where(meta.c.chave == f"etapa_ok:{nome}")).first() is not None


def executar(desde: date | None = None, so_senadores=False):
    init_db()
    coletar_senadores()
    if not so_senadores:
        hoje = date.today()
        completa = desde is not None
        if desde is None:
            desde = inicio_atualizacao_votacoes(hoje)
        _passo("votações", coletar_votacoes, desde, hoje)
        with engine.begin() as c:
            vincular_senadores(c)
        # CEAPS: na carga completa, todos os anos; no dia a dia, o ano corrente
        # (e o anterior até março, quando ainda chegam lançamentos atrasados)
        inicio_ceaps = int(INICIO_LEGISLATURA[:4]) if completa else (
            hoje.year - 1 if hoje.month <= 3 else hoje.year)
        _passo("CEAPS", coletar_ceaps, range(inicio_ceaps, hoje.year + 1))
        with engine.begin() as c:
            limpar_senadores_sem_atividade(c)
        _passo("proposições", coletar_processos)
        _passo("perfis", coletar_perfis)
        import coleta_meu
        _passo("meu parlamentar", coleta_meu.executar)
    with engine.begin() as c:
        vincular_senadores(c)
        if not so_senadores:
            limpar_senadores_sem_atividade(c)
        marcar_coleta(c)


def carga_inicial_se_vazio():
    """Usado no primeiro deploy: se o banco estiver vazio, coleta a legislatura inteira."""
    init_db()
    with engine.connect() as c:
        tem_votacoes = c.execute(select(votacoes.c.id).limit(1)).first() is not None
    # uma etapa só conta como feita se terminou (deploy no meio da carga não deixa dados pela metade)
    tem = {"despesas": _etapa_concluida("CEAPS"), "processos": _etapa_concluida("proposições"),
           "perfis": _etapa_concluida("perfis")}
    if not tem_votacoes:
        log.info("banco vazio — carga completa desde %s", INICIO_LEGISLATURA)
        executar(date.fromisoformat(INICIO_LEGISLATURA))
        return
    # banco de uma versão anterior: completa só o que falta
    coletar_senadores()
    _passo("votações", coletar_votacoes, inicio_atualizacao_votacoes(), date.today())
    with engine.begin() as c:
        vincular_senadores(c)
    if not tem["despesas"]:
        _passo("CEAPS", coletar_ceaps, range(int(INICIO_LEGISLATURA[:4]), date.today().year + 1))
    with engine.begin() as c:
        limpar_senadores_sem_atividade(c)
    if not tem["processos"]:
        _passo("proposições", coletar_processos)
    if not tem["perfis"]:
        _passo("perfis", coletar_perfis)
    with engine.begin() as c:
        vincular_senadores(c)
        limpar_senadores_sem_atividade(c)
        marcar_coleta(c)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", help="AAAA-MM-DD (carga completa a partir desta data)")
    ap.add_argument("--senadores", action="store_true", help="só o cadastro de senadores")
    ap.add_argument("--se-vazio", action="store_true", help="carga completa só se o banco estiver vazio")
    ap.add_argument("--limpar", action="store_true", help="só remove senadores sem atividade (suplentes que não assumiram)")
    a = ap.parse_args()
    if a.limpar:
        init_db()
        with engine.begin() as c:
            vincular_senadores(c)
            limpar_senadores_sem_atividade(c)
    elif a.se_vazio:
        carga_inicial_se_vazio()
    else:
        executar(date.fromisoformat(a.desde) if a.desde else None, a.senadores)
