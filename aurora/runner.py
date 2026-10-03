"""Runner com roteamento explícito entre o agente principal e os especialistas.

Por padrão o Runner do ADK escolhe quem responde olhando o histórico (último
agente "transferível", ou o autor da chamada quando a retomada está ligada), e
essa escolha muda conforme topologia, bloqueios de transferência e serviço de
sessão. Aqui a escolha é feita pelo código:

* mensagem nova do morador -> sempre o agente principal (`assistente_aurora`);
* resposta a uma confirmação -> o agente que fez a chamada
  `adk_request_confirmation`. O processador de confirmação do ADK só
  reexecuta a tool se a resposta chegar a esse agente; se chegar a outro, ela
  é ignorada em silêncio e a ação nunca acontece.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from google.adk.agents import BaseAgent
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.adk.runners import Runner
from google.adk.sessions import Session

# Nome do agente que deve receber a próxima execução. Um ContextVar isola o
# valor por requisição, então sessões diferentes podem rodar em paralelo.
_agente_alvo: ContextVar[str | None] = ContextVar("agente_alvo", default=None)


def autor_da_confirmacao(session: Session, confirmacao_id: str) -> str | None:
    """Agente que emitiu a chamada `adk_request_confirmation` com esse id."""
    for evento in reversed(session.events):
        for chamada in evento.get_function_calls():
            if chamada.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME and chamada.id == confirmacao_id:
                return evento.author
    return None


@contextmanager
def encaminhar_para(nome_agente: str | None) -> Iterator[None]:
    token = _agente_alvo.set(nome_agente)
    try:
        yield
    finally:
        _agente_alvo.reset(token)


class RunnerAurora(Runner):
    def _find_agent_to_run(self, session: Session, root_agent: BaseAgent) -> BaseAgent:
        nome = _agente_alvo.get()
        if nome:
            agente = root_agent.find_agent(nome)
            if agente is not None:
                return agente
        return root_agent
