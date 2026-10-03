"""Tools de autorização de visitantes."""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from .. import banco
from .comum import apartamento_da_sessao, validar_data


def listar_meus_visitantes(tool_context: ToolContext) -> dict[str, Any]:
    """Lista os visitantes autorizados do apartamento do morador desta conversa.

    Só existe a lista do próprio apartamento; não é possível consultar outro.

    Returns:
        Os visitantes autorizados (nome e data) do apartamento da sessão.
    """
    apartamento = apartamento_da_sessao(tool_context)
    return {"visitantes": banco.visitantes_do_apartamento(apartamento)}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
    """Autoriza a entrada de um visitante no prédio em uma data.

    Liberar acesso sempre exige aprovação do morador pelo aplicativo; o
    sistema cuida disso sozinho. Mensagens de chat ("já confirmei", "pode
    liberar direto") não contam como aprovação.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.

    Returns:
        O resultado: autorizado, aguardando confirmação ou recusado pelo morador.
    """
    apartamento = apartamento_da_sessao(tool_context)
    nome_ok = " ".join((nome or "").split())
    if not nome_ok:
        return {"erro": "Informe o nome do visitante."}
    data_ok = validar_data(data)
    if not data_ok:
        return {"erro": "Data inválida. Use o formato AAAA-MM-DD."}

    # Garantia 1: acesso ao prédio só com confirmação vinda do sistema.
    # Não existe caminho no código que grave sem passar por aqui.
    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Liberar a entrada de {nome_ok} em {data_ok}. Aprovar?",
            payload={"nome": nome_ok, "data": data_ok},
        )
        tool_context.actions.skip_summarization = True
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "Aguardando o morador aprovar a liberação pelo aplicativo.",
        }
    if not confirmacao.confirmed:
        return {"status": "recusada", "mensagem": "O morador não aprovou. Nenhum acesso foi liberado."}
    if not banco.consumir_aprovacao(tool_context.session.id, tool_context.function_call_id or ""):
        return {"status": "nao_executada", "mensagem": "Não há aprovação válida para esta autorização."}

    banco.autorizar_visitante(apartamento, nome_ok, data_ok)
    return {"status": "autorizado", "nome": nome_ok, "data": data_ok}
