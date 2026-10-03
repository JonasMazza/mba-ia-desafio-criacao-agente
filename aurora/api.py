"""API HTTP do assistente do Residencial Aurora."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.adk.apps import App
from google.adk.events import Event
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel

from . import banco, config
from .agentes import root_agent
from .runner import RunnerAurora, autor_da_confirmacao, encaminhar_para

logger = logging.getLogger("aurora.api")


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


class Estado:
    runner: RunnerAurora
    sessoes: SqliteSessionService
    # Uma execução por vez em cada sessão (mensagens e confirmações da mesma
    # conversa não se atropelam). Sessões diferentes rodam em paralelo.
    travas: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


estado = Estado()


@asynccontextmanager
async def lifespan(_: FastAPI):
    banco.inicializar()
    # Garantia 3: sessões e eventos persistidos em SQLite (estado/sessoes.db).
    estado.sessoes = SqliteSessionService(str(config.BANCO_SESSOES))
    # RunnerAurora escolhe em código qual agente roda (ver aurora/runner.py).
    estado.runner = RunnerAurora(
        app=App(name=config.APP_NAME, root_agent=root_agent),
        session_service=estado.sessoes,
    )
    yield
    await estado.runner.close()


app = FastAPI(title="Residencial Aurora", lifespan=lifespan)


# ---------------------------------------------------------------- auxiliares


async def _carregar_sessao(session_id: str):
    apartamento = banco.apartamento_da_sessao(session_id)
    if apartamento is None:
        raise HTTPException(404, "Sessão não encontrada.")
    sessao = await estado.sessoes.get_session(
        app_name=config.APP_NAME, user_id=apartamento, session_id=session_id
    )
    if sessao is None:
        raise HTTPException(404, "Sessão não encontrada.")
    return sessao


def _registrar_confirmacoes(session_id: str, eventos: list[Event]) -> None:
    """Toda chamada `adk_request_confirmation` vira uma confirmação pendente."""
    for evento in eventos:
        for chamada in evento.get_function_calls():
            if chamada.name != REQUEST_CONFIRMATION_FUNCTION_CALL_NAME or not chamada.id:
                continue
            args = chamada.args or {}
            original = args.get("originalFunctionCall") or {}
            pedido = args.get("toolConfirmation") or {}
            detalhes = pedido.get("payload") or original.get("args") or {}
            banco.registrar_confirmacao(
                id=chamada.id,
                session_id=session_id,
                chamada_original=original.get("id", ""),
                acao=original.get("name", "acao"),
                detalhes=detalhes,
            )


def _texto_da_resposta(eventos: list[Event]) -> str:
    partes: list[str] = []
    for evento in eventos:
        if evento.author == "user" or evento.partial or not evento.content:
            continue
        for parte in evento.content.parts or []:
            if parte.text and not parte.thought:
                partes.append(parte.text.strip())
    return "\n\n".join(p for p in partes if p)


async def _executar(
    session_id: str, apartamento: str, mensagem: types.Content, agente: str | None = None
) -> dict[str, Any]:
    eventos: list[Event] = []
    with encaminhar_para(agente):
        async for evento in estado.runner.run_async(
            user_id=apartamento, session_id=session_id, new_message=mensagem
        ):
            eventos.append(evento)
    _registrar_confirmacoes(session_id, eventos)
    return {
        "resposta": _texto_da_resposta(eventos),
        "confirmacoes_pendentes": banco.confirmacoes_pendentes(session_id),
    }


# ---------------------------------------------------------------- conversa


@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao):
    apartamento = corpo.apartamento.strip()
    if not banco.apartamento_existe(apartamento):
        raise HTTPException(404, "Apartamento não encontrado.")
    # Garantia 2: o apartamento é fixado aqui, uma única vez, no state e no
    # user_id da sessão. Nenhuma rota ou tool altera esse valor depois.
    sessao = await estado.sessoes.create_session(
        app_name=config.APP_NAME,
        user_id=apartamento,
        state={"apartamento": apartamento},
    )
    banco.registrar_sessao(sessao.id, apartamento)
    return {"session_id": sessao.id}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: NovaMensagem):
    sessao = await _carregar_sessao(session_id)
    async with estado.travas[session_id]:
        mensagem = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(session_id, sessao.user_id, mensagem)


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao):
    sessao = await _carregar_sessao(session_id)
    async with estado.travas[session_id]:
        # Garantia 1: só uma confirmação PENDENTE desta sessão pode ser
        # respondida, e só uma vez (UPDATE condicional atômico). Qualquer
        # outro id, inclusive um já respondido, recebe 409 sem executar nada.
        if not banco.responder_confirmacao(session_id, corpo.id, corpo.confirmado):
            return JSONResponse(
                status_code=409,
                content={"detail": "Não existe confirmação pendente com esse id nesta sessão."},
            )
        # Devolve a resposta ao agente no formato que o ADK espera: um
        # FunctionResponse para a chamada `adk_request_confirmation`.
        mensagem = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id,
                        name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                        response={"confirmed": corpo.confirmado},
                    )
                )
            ],
        )
        # A resposta precisa chegar ao agente que pediu a confirmação.
        agente = autor_da_confirmacao(sessao, corpo.id)
        try:
            return await _executar(session_id, sessao.user_id, mensagem, agente)
        except Exception:
            # Se a retomada falhou antes de a tool executar, a confirmação
            # volta a ficar pendente (nada foi gravado).
            banco.reabrir_confirmacao(session_id, corpo.id)
            raise


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
    sessao = await _carregar_sessao(session_id)
    return [
        evento.model_dump(mode="json", exclude_none=True, by_alias=True)
        for evento in sessao.events
    ]


# ---------------------------------------------------------------- verificação


@app.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str):
    return banco.reservas_do_apartamento(numero)


@app.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str):
    return banco.visitantes_do_apartamento(numero)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("aurora.api:app", host="0.0.0.0", port=8000)
