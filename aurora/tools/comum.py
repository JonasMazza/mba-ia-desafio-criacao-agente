"""Utilitários compartilhados pelas tools."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from google.adk.tools import ToolContext

from .. import banco


class AcessoNegado(Exception):
    """A sessão não tem um apartamento válido."""


def apartamento_da_sessao(tool_context: ToolContext) -> str:
    """Garantia 2: o apartamento SEMPRE vem da sessão, nunca de um argumento.

    O valor é gravado pela API em `state["apartamento"]` (e como `user_id`) no
    momento da criação da sessão. Nenhuma tool recebe apartamento como
    parâmetro, então o modelo não tem como escolher outro.
    """
    apartamento = tool_context.state.get("apartamento")
    if not apartamento or apartamento != tool_context.user_id:
        raise AcessoNegado("Sessão sem apartamento autenticado.")
    return str(apartamento)


def _slug(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")


def resolver_area(area: str) -> dict | None:
    """Aceita o id ('salao-de-festas') ou o nome ('Salão de festas')."""
    alvo = _slug(area or "")
    if not alvo:
        return None
    areas = banco.listar_areas()
    for a in areas:
        if alvo in (a["id"], _slug(a["nome"])):
            return a
    # tolera variações como "salão", "quadra poliesportiva", "churrasco"
    for a in areas:
        if alvo in _slug(a["nome"]) or a["id"] in alvo or alvo.split("-")[0] in a["id"]:
            return a
    return None


def validar_data(data: str) -> str | None:
    """Devolve a data normalizada (AAAA-MM-DD) ou None se inválida."""
    try:
        return date.fromisoformat((data or "").strip()).isoformat()
    except ValueError:
        return None


def nomes_das_areas() -> str:
    return ", ".join(f"{a['id']} ({a['nome']})" for a in banco.listar_areas())
