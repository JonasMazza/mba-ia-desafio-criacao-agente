"""Tools de reservas das áreas comuns.

Nenhuma tool aceita apartamento como parâmetro: ele vem sempre da sessão
(`apartamento_da_sessao`). Por isso o modelo não consegue ler, criar ou
cancelar reservas de outro apartamento, nem que o morador peça.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from .. import banco
from .comum import apartamento_da_sessao, nomes_das_areas, resolver_area, validar_data


def listar_areas() -> dict[str, Any]:
    """Lista as áreas comuns que podem ser reservadas e a taxa de cada uma.

    Returns:
        As áreas com id, nome, taxa em reais e se a reserva gera cobrança.
    """
    return {
        "areas": [
            {**a, "gera_cobranca": a["taxa"] > 0} for a in banco.listar_areas()
        ]
    }


def listar_minhas_reservas(tool_context: ToolContext) -> dict[str, Any]:
    """Lista as reservas ativas do apartamento do morador desta conversa.

    Só existe a lista do próprio apartamento; não é possível consultar outro.

    Returns:
        As reservas ativas (código, área e data) do apartamento da sessão.
    """
    apartamento = apartamento_da_sessao(tool_context)
    return {"reservas": banco.reservas_do_apartamento(apartamento)}


def consultar_disponibilidade(area: str, data: str) -> dict[str, Any]:
    """Informa se uma área comum está livre ou ocupada em uma data.

    Não informa de quem é a reserva, apenas se a data está livre.

    Args:
        area: id ou nome da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.

    Returns:
        A área, a data e se ela está disponível.
    """
    area_info = resolver_area(area)
    if not area_info:
        return {"erro": f"Área desconhecida. Áreas válidas: {nomes_das_areas()}."}
    data_ok = validar_data(data)
    if not data_ok:
        return {"erro": "Data inválida. Use o formato AAAA-MM-DD."}
    livre = banco.data_livre(area_info["id"], data_ok)
    return {"area": area_info["id"], "data": data_ok, "disponivel": livre}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
    """Reserva uma área comum para o apartamento do morador desta conversa.

    Áreas com taxa geram cobrança e por isso ficam pendentes até o morador
    aprovar pelo aplicativo; o sistema cuida disso sozinho. Áreas sem taxa
    são reservadas na hora.

    Args:
        area: id ou nome da área (salao-de-festas, churrasqueira ou quadra).
        data: data da reserva no formato AAAA-MM-DD.

    Returns:
        O resultado da reserva: reservada (com o código), aguardando
        confirmação, recusada pelo morador ou data indisponível.
    """
    apartamento = apartamento_da_sessao(tool_context)
    area_info = resolver_area(area)
    if not area_info:
        return {"erro": f"Área desconhecida. Áreas válidas: {nomes_das_areas()}."}
    data_ok = validar_data(data)
    if not data_ok:
        return {"erro": "Data inválida. Use o formato AAAA-MM-DD."}

    indisponivel = {
        "status": "indisponivel",
        "area": area_info["id"],
        "data": data_ok,
        "mensagem": "A área já está reservada nesta data. Sugira outra data ao morador.",
    }

    # Garantia 1: cobrança só com confirmação vinda do sistema.
    # A decisão de pedir confirmação é do código (taxa > 0), não do modelo.
    if area_info["taxa"] > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            # Conferência prévia só para não pedir aprovação de algo impossível.
            # A exclusividade de verdade é garantida na gravação (banco.criar_reserva).
            if not banco.data_livre(area_info["id"], data_ok):
                return indisponivel
            tool_context.request_confirmation(
                hint=(
                    f"Reservar {area_info['nome']} em {data_ok} gera cobrança de "
                    f"R$ {area_info['taxa']:.2f}. Aprovar?"
                ),
                payload={
                    "area": area_info["id"],
                    "data": data_ok,
                    "taxa": area_info["taxa"],
                },
            )
            tool_context.actions.skip_summarization = True
            return {
                "status": "aguardando_confirmacao",
                "mensagem": "Aguardando o morador aprovar a cobrança pelo aplicativo.",
            }
        if not confirmacao.confirmed:
            return {"status": "recusada", "mensagem": "O morador não aprovou a cobrança. Nada foi reservado."}
        # A aprovação precisa estar registrada pela rota de confirmações e só
        # pode ser usada uma vez (aprovada -> executada, atômico).
        if not banco.consumir_aprovacao(tool_context.session.id, tool_context.function_call_id or ""):
            return {"status": "nao_executada", "mensagem": "Não há aprovação válida para esta reserva."}

    try:
        codigo = banco.criar_reserva(apartamento, area_info["id"], data_ok)
    except banco.DataOcupada:
        # Garantia 5: outra reserva entrou antes (ex.: aprovação simultânea).
        return indisponivel
    return {
        "status": "reservada",
        "codigo": codigo,
        "area": area_info["id"],
        "data": data_ok,
        "cobranca": area_info["taxa"],
        "mensagem": (
            f"Reserva concluída. Cobrança de R$ {area_info['taxa']:.2f} já aprovada pelo morador."
            if area_info["taxa"] > 0
            else "Reserva concluída. Esta área não tem cobrança."
        ),
    }


def cancelar_reserva(
    tool_context: ToolContext,
    codigo: str = "",
    area: str = "",
    data: str = "",
) -> dict[str, Any]:
    """Cancela uma reserva do apartamento do morador desta conversa.

    Informe o código da reserva, ou a área e a data. Só reservas do próprio
    apartamento podem ser canceladas. Não precisa de confirmação.

    Args:
        codigo: código da reserva (opcional se área e data forem informadas).
        area: id ou nome da área (opcional se o código for informado).
        data: data da reserva no formato AAAA-MM-DD (opcional se o código for informado).

    Returns:
        A reserva cancelada, ou a informação de que não há reserva do
        apartamento com esses dados.
    """
    apartamento = apartamento_da_sessao(tool_context)
    area_id = None
    if area:
        area_info = resolver_area(area)
        if not area_info:
            return {"erro": f"Área desconhecida. Áreas válidas: {nomes_das_areas()}."}
        area_id = area_info["id"]
    data_ok = None
    if data:
        data_ok = validar_data(data)
        if not data_ok:
            return {"erro": "Data inválida. Use o formato AAAA-MM-DD."}
    if not codigo and not (area_id and data_ok):
        return {"erro": "Informe o código da reserva ou a área e a data."}

    cancelada = banco.cancelar_reserva(
        apartamento, codigo=codigo or None, area_id=area_id, data=data_ok
    )
    if not cancelada:
        # Resposta neutra: não revela se existe reserva de outro apartamento.
        return {
            "status": "nao_encontrada",
            "mensagem": "Não existe reserva ativa do seu apartamento com esses dados.",
        }
    return {"status": "cancelada", **cancelada}
