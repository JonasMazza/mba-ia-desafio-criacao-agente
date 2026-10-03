"""Tools de consulta ao regulamento interno (`dados/regulamento.md`).

O texto é dividido por capítulo e entregue um capítulo por vez, sob demanda.
Essas tools só são usadas pelo especialista em regulamento, que roda como
AgentTool numa sessão própria e efêmera: os capítulos lidos nunca entram nos
eventos da sessão do morador (Garantia 4).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from .. import config

_CABECALHO = re.compile(r"^## (Capítulo ([IVXLC]+)): (.+)$", re.MULTILINE)


@lru_cache(maxsize=1)
def _capitulos() -> list[dict[str, str]]:
    texto = (config.DADOS / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_CABECALHO.finditer(texto))
    capitulos = []
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        capitulos.append(
            {
                "numero": m.group(2),
                "titulo": m.group(3).strip(),
                "texto": texto[m.start():fim].strip(),
            }
        )
    return capitulos


def sumario() -> str:
    """Sumário em texto (só títulos), usado nas instruções do especialista."""
    return "\n".join(f"- {c['numero']}: {c['titulo']}" for c in _capitulos())


def ler_capitulo(numero: str) -> dict[str, Any]:
    """Lê o texto integral de UM capítulo do regulamento.

    Args:
        numero: número romano do capítulo (ex.: "IV").

    Returns:
        O título e o texto do capítulo pedido.
    """
    alvo = (numero or "").strip().upper().removeprefix("CAPÍTULO ").strip()
    for c in _capitulos():
        if c["numero"] == alvo:
            return {"numero": c["numero"], "titulo": c["titulo"], "texto": c["texto"]}
    return {"erro": "Capítulo não encontrado. Use o número romano do sumário."}
