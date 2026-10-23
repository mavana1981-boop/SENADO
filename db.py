"""Banco de dados: PostgreSQL no Railway (DATABASE_URL) ou SQLite local."""
import os
import re
import unicodedata

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, Integer, LargeBinary, MetaData,
                        String, Table, Text, create_engine)


def _db_url():
    url = os.environ.get("DATABASE_URL", "sqlite:///senado.db")
    # Railway entrega "postgres://"; o SQLAlchemy 2 + psycopg 3 exige o dialeto explícito
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(_db_url(), pool_pre_ping=True, future=True)
metadata = MetaData()

senadores = Table(
    "senadores", metadata,
    Column("codigo", Integer, primary_key=True),
    Column("nome", String(120), nullable=False),
    Column("nome_norm", String(120), index=True),
    Column("nome_completo", String(200)),
    Column("partido", String(40)),
    Column("uf", String(2)),
    Column("bloco", String(120)),
    Column("foto", String(255)),
    Column("em_exercicio", Boolean, default=False),
)

votacoes = Table(
    "votacoes", metadata,
    Column("id", Integer, primary_key=True),          # codigoVotacaoSve
    Column("sequencial", Integer),
    Column("data", DateTime, index=True),
    Column("sigla_materia", String(20), index=True),
    Column("numero_materia", Integer),
    Column("ano_materia", Integer),
    Column("descricao_materia", Text),
    Column("descricao_votacao", Text),
    Column("dispositivo", Text),
    Column("sessao", String(200)),
    Column("presidente", String(120)),
    Column("qtd_sim", Integer),
    Column("qtd_nao", Integer),
    Column("qtd_abstencao", Integer),
    Column("secreta", Boolean, default=False),
)

votos = Table(
    "votos", metadata,
    Column("votacao_id", Integer, primary_key=True),
    Column("nome_norm", String(120), primary_key=True),
    Column("nome", String(120)),
    Column("codigo_senador", Integer, index=True),    # preenchido pelo cruzamento de nomes
    Column("partido", String(40)),                    # partido na data do voto
    Column("uf", String(2)),
    Column("voto", String(30)),
)

orientacoes = Table(
    "orientacoes", metadata,
    Column("votacao_id", Integer, primary_key=True),
    Column("lideranca", String(60), primary_key=True),  # partido, "Governo", "Oposição"...
    Column("voto", String(30)),
)

despesas = Table(  # CEAPS
    "despesas", metadata,
    Column("id", Integer, primary_key=True),
    Column("ano", Integer, index=True),
    Column("mes", Integer),
    Column("data", String(10)),
    Column("codigo_senador", Integer, index=True),
    Column("nome", String(120)),
    Column("tipo", String(300)),
    Column("tipo_documento", String(80)),
    Column("fornecedor", String(300)),
    Column("cnpj_cpf", String(30)),
    Column("documento", String(80)),
    Column("detalhamento", Text),
    Column("valor", Float),
)

processos = Table(
    "processos", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("identificacao", String(60)),
    Column("sigla", String(20), index=True),
    Column("numero", String(20)),
    Column("ano", Integer, index=True),
    Column("codigo_materia", BigInteger),
    Column("ementa", Text),
    Column("autoria", Text),
    Column("tipo_documento", String(120)),
    Column("tipo_conteudo", String(120)),
    Column("situacao", String(200)),
    Column("data_apresentacao", String(10), index=True),
    Column("data_situacao", String(10)),
    Column("tramitando", Boolean),
    Column("virou_norma", Boolean, default=False),
    Column("url", String(300)),
)

autorias = Table(
    "autorias", metadata,
    Column("processo_id", BigInteger, primary_key=True),
    Column("codigo_senador", Integer, primary_key=True, index=True),
    Column("principal", Boolean, default=False),
)

notificacoes = Table(  # mudanças de situação das proposições, detectadas na coleta diária
    "notificacoes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("codigo_senador", Integer, index=True),
    Column("processo_id", BigInteger, index=True),
    Column("identificacao", String(60)),
    Column("tipo", String(20)),                 # "situacao" (mudou) ou "nova" (proposição nova)
    Column("situacao_anterior", String(200)),
    Column("situacao_nova", String(200)),
    Column("data_situacao", String(10)),        # data da situação nova, segundo o Senado
    Column("detectado_em", DateTime, index=True),
)

perfis = Table(  # detalhe, comissões e cargos de cada senador (JSON da API)
    "perfis", metadata,
    Column("codigo", Integer, primary_key=True),
    Column("detalhe", Text),
    Column("comissoes", Text),
    Column("cargos", Text),
    Column("atualizado", DateTime),
)

usuarios = Table(
    "usuarios", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("login", String(60), unique=True, nullable=False),
    Column("nome", String(120)),
    Column("senha_hash", String(300), nullable=False),
    Column("admin", Boolean, default=False),
    Column("ativo", Boolean, default=True),
    Column("criado", DateTime),
    Column("ultimo_acesso", DateTime),
)

# ---------------- Meu Parlamentar ----------------

usuario_senadores = Table(  # quem pode abrir o "Meu Parlamentar" de qual senador (admin vê todos)
    "usuario_senadores", metadata,
    Column("usuario_id", Integer, primary_key=True),
    Column("codigo_senador", Integer, primary_key=True, index=True),
)

agenda = Table(  # agenda pública do Senado (codigo_senador nulo) e compromissos lançados pela equipe
    "agenda", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("origem", String(10)),                # "senado" ou "equipe"
    Column("chave", String(80), index=True),     # identificador da API (evita duplicar)
    Column("codigo_senador", Integer, index=True),
    Column("data", String(10), index=True),      # AAAA-MM-DD
    Column("hora", String(5)),
    Column("titulo", String(300)),
    Column("colegiado", String(30)),             # sigla da comissão, "PLEN" para o Plenário
    Column("local", String(200)),
    Column("situacao", String(80)),
    Column("descricao", Text),                   # pauta (texto) ou observação da equipe
    Column("criado_por", String(60)),
    Column("criado_em", DateTime),
)

solicitacoes = Table(
    "solicitacoes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("codigo_senador", Integer, index=True),
    Column("titulo", String(200)),
    Column("descricao", Text),
    Column("solicitante", String(120)),          # quem pediu (o senador, chefe de gabinete...)
    Column("status", String(20)),                # aberta, andamento, concluida, arquivada
    Column("prazo", String(10)),
    Column("criado_por", String(60)),
    Column("criado_em", DateTime),
    Column("atualizado_em", DateTime),
)

anexos = Table(
    "anexos", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("solicitacao_id", Integer, index=True),
    Column("nome", String(200)),
    Column("mime", String(100)),
    Column("tamanho", Integer),
    Column("conteudo", LargeBinary),
    Column("enviado_por", String(60)),
    Column("enviado_em", DateTime),
)

questoes_ordem = Table(
    "questoes_ordem", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("codigo_senador", Integer, index=True),
    Column("autor", String(60)),
    Column("situacao", Text),
    Column("resposta", Text),
    Column("modelo", String(60)),
    Column("criado_em", DateTime),
)

documentos = Table(  # textos de referência (ex.: Regimento Interno), enviados pelo admin
    "documentos", metadata,
    Column("chave", String(40), primary_key=True),
    Column("nome", String(200)),
    Column("texto", Text),
    Column("atualizado", DateTime),
)

temas_radar = Table(
    "temas_radar", metadata,
    Column("codigo_senador", Integer, primary_key=True),
    Column("termo", String(120), primary_key=True),
)

noticias = Table(
    "noticias", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("codigo_senador", Integer, index=True),
    Column("termo", String(120)),
    Column("titulo", String(500)),
    Column("link", String(1000)),
    Column("fonte", String(200)),
    Column("publicado", DateTime, index=True),
    Column("coletado_em", DateTime),
)

relatorias = Table(
    "relatorias", metadata,
    Column("codigo_senador", Integer, primary_key=True),
    Column("chave", String(120), primary_key=True),   # matéria + colegiado + data de designação
    Column("identificacao", String(60)),
    Column("codigo_materia", BigInteger),
    Column("ementa", Text),
    Column("colegiado", String(30)),
    Column("data_designacao", String(10)),
    Column("data_destituicao", String(10)),
    Column("situacao", String(200)),
    Column("data_situacao", String(10)),
    Column("url", String(300)),
    Column("atualizado", DateTime),
)

meta = Table(
    "meta", metadata,
    Column("chave", String(60), primary_key=True),
    Column("valor", Text),
)


def init_db():
    metadata.create_all(engine)


# ---------- Normalização ----------

def norm_nome(nome: str) -> str:
    """'Ivete da Silveira ' -> 'ivete da silveira'; remove acentos e pontuação."""
    if not nome:
        return ""
    s = unicodedata.normalize("NFKD", nome)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z ]", " ", s).lower()
    return re.sub(r"\s+", " ", s).strip()


# A API usa grafias diferentes para o mesmo partido em votos/orientações e no cadastro
_PARTIDOS = {
    "REPUBLICA": "REPUBLICANOS", "REPUBLICANOS": "REPUBLICANOS",
    "PODEMOS": "PODE", "PODE": "PODE",
    "UNIAO": "UNIÃO", "UNIÃO": "UNIÃO",
    "CIDADANIA": "CIDADANIA", "S/PARTIDO": "S/PARTIDO",
}


def norm_partido(p: str) -> str:
    if not p:
        return ""
    chave = p.strip().upper()
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", chave)
                         if not unicodedata.combining(c))
    return _PARTIDOS.get(chave) or _PARTIDOS.get(sem_acento) or chave


def norm_voto(v: str) -> str:
    """Padroniza: SIM, NÃO, ABSTENÇÃO, OBSTRUÇÃO, LIVRE, SECRETO; demais códigos ficam como vêm."""
    if not v:
        return ""
    s = v.strip().upper()
    s_sem = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    mapa = {"SIM": "SIM", "NAO": "NÃO", "ABSTENCAO": "ABSTENÇÃO",
            "OBSTRUCAO": "OBSTRUÇÃO", "LIVRE": "LIVRE", "LIBERADO": "LIVRE",
            "SECRETO": "SECRETO"}
    return mapa.get(s_sem, s)


# Votos que contam como participação (o senador registrou posição)
VOTOS_PARTICIPACAO = ("SIM", "NÃO", "ABSTENÇÃO", "OBSTRUÇÃO", "SECRETO")
# Votos e orientações que permitem medir alinhamento
POSICOES = ("SIM", "NÃO")
