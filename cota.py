"""Teto mensal da CEAPS por UF.

Valores a partir de 01/02/2023 (após reajuste de 6%), publicados no Boletim Administrativo do
Senado de 10/02/2023 e reproduzidos pela imprensa (CNN Brasil). O mesmo ato escalonou:
+6% a partir de 01/02/2024 e +6,13% a partir de 01/02/2025.
De 2026 em diante, mantém o valor de 2025 até que um novo ato seja publicado — se houver
reajuste, acrescente-o em REAJUSTES abaixo (ou via variável de ambiente CEAPS_REAJUSTES,
ex.: "2026-02-01:0.05").
"""
import os
from datetime import date

BASE_2023 = {
    "AC": 41185.72, "AL": 37159.57, "AP": 45426.51, "AM": 46933.20, "BA": 37541.17,
    "CE": 40477.80, "DF": 22307.91, "ES": 35167.20, "GO": 22307.91, "MA": 39640.40,
    "MT": 37030.52, "MS": 34879.51, "MG": 30205.97, "PA": 42851.77, "PB": 37688.51,
    "PR": 34541.80, "PE": 38442.60, "PI": 41164.52, "RJ": 33725.17, "RN": 38134.77,
    "RS": 38039.80, "RO": 36692.11, "RR": 43167.92, "SC": 34843.60, "SP": 32039.77,
    "SE": 44355.12, "TO": 26728.11,
}
INICIO_BASE = date(2023, 2, 1)

REAJUSTES = [(date(2024, 2, 1), 0.06), (date(2025, 2, 1), 0.0613)]
for item in filter(None, os.environ.get("CEAPS_REAJUSTES", "").split(",")):
    d, _, pct = item.partition(":")
    try:
        REAJUSTES.append((date.fromisoformat(d.strip()), float(pct)))
    except ValueError:
        pass
REAJUSTES.sort()


def teto_mensal(uf: str, ano: int, mes: int) -> float | None:
    """Valor da cota de um mês. Antes de fev/2023 usa o valor de 2023 sem o reajuste de 6%."""
    base = BASE_2023.get((uf or "").upper())
    if base is None:
        return None
    ref = date(ano, mes, 1)
    if ref < INICIO_BASE:
        return round(base / 1.06, 2)
    v = base
    for quando, pct in REAJUSTES:
        if ref >= quando:
            v *= 1 + pct
    return round(v, 2)


def teto_periodo(uf: str, ano: int, meses) -> float:
    return round(sum(teto_mensal(uf, ano, m) or 0 for m in meses), 2)
