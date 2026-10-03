# Residencial Aurora: assistente virtual com Google ADK

API em Python (FastAPI + Google ADK 2.11.0 + Gemini) para o assistente dos moradores do Residencial Aurora. Pelo chat o morador reserva e cancela áreas comuns, autoriza visitantes e tira dúvidas sobre o regulamento.

O princípio do projeto: **o modelo decide o caminho, o código decide o que é permitido.** Cada regra crítica está em código Python ou no banco SQLite. Nenhuma depende do prompt.

```
aurora/
├── agentes.py        # agente principal + 3 especialistas
├── runner.py         # Runner com roteamento explícito (mensagem -> principal; confirmação -> quem pediu)
├── api.py            # FastAPI: contrato HTTP, sessões persistidas, rota de confirmações
├── banco.py          # SQLite do condomínio: reservas, visitantes, confirmações, sessões
├── restaurar.py      # comando de restauração dos dados iniciais
├── agent.py          # expõe root_agent para o `adk web`
└── tools/
    ├── comum.py      # apartamento da sessão, normalização de área e data
    ├── reservas.py   # listar/consultar/reservar/cancelar
    ├── visitantes.py # listar/autorizar
    └── regulamento.py# leitura de um capítulo por vez
scripts/fluxo_avaliador.py  # reproduz o fluxo do avaliador contra a API
dados/                      # estado inicial (intocado)
estado/                     # bancos SQLite gerados em tempo de execução (fora do Git)
```

## Arquitetura

```
                 POST /sessoes/{id}/mensagens            POST /sessoes/{id}/confirmacoes
                            │                                         │
                            ▼                                         ▼
                 RunnerAurora ── mensagem nova ──► assistente_aurora   │
                      │                               │  transfer_to_agent
                      │                               ├──► especialista_reservas   ◄──┐
                      │                               ├──► especialista_visitantes ◄──┤ resposta da
                      │                               └──► especialista_regulamento   │ confirmação vai
                      │                                    (AgentTool)                │ direto ao autor
                      └───────────────────────────────────────────────────────────────┘
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` (principal) | Entende o pedido e encaminha. Não tem tools de dados nem o regulamento nas instruções. | É o agente raiz. Recebe **toda** mensagem nova do morador (`RunnerAurora`). | Um ponto único de entrada torna o roteamento previsível: um especialista nunca "segura" a conversa e responde algo fora do seu assunto. |
| `especialista_reservas` | Lista áreas, consulta disponibilidade, lista, cria e cancela reservas do apartamento da sessão. | Sub-agente, acionado por **transferência** (`transfer_to_agent`). Tem `disallow_transfer_to_parent/peers=True`: responde e encerra o turno. | Precisa rodar na sessão persistida do morador, porque o pedido de confirmação de cobrança fica gravado nela e é retomado depois, inclusive após reiniciar a API. |
| `especialista_visitantes` | Lista e autoriza visitantes do apartamento da sessão. | Sub-agente, por **transferência**, com as mesmas travas. | Mesmo motivo: a autorização de visitante sempre pede confirmação, que precisa sobreviver na sessão. |
| `especialista_regulamento` | Responde dúvidas lendo **um capítulo** do regulamento por vez. | **AgentTool** chamado pelo principal. | O AgentTool roda numa sessão própria, em memória e descartável: os capítulos lidos ficam lá e só a resposta curta volta para a sessão do morador (Garantia 4). |

Cada agente usa um modelo Gemini configurável por variável de ambiente (`aurora/config.py`). Os padrões são modelos *flash-lite*, um modelo diferente por agente onde possível, porque no plano gratuito do Google AI Studio o limite de requisições é **por modelo**. Assim a carga se divide e o fluxo inteiro cabe na cota. As chamadas repetem automaticamente em 429/5xx (`_modelo` em `aurora/agentes.py`).

**Fluxo de uma confirmação (o ponto que vai além das aulas):**

1. A tool (`reservar_area` com taxa > 0, ou `autorizar_visitante`) chama `tool_context.request_confirmation(...)`. O ADK grava na sessão uma chamada `adk_request_confirmation` e pausa.
2. A API transforma cada `adk_request_confirmation` em uma linha `pendente` na tabela `confirmacoes` (`_registrar_confirmacoes` em `aurora/api.py`) e a devolve em `confirmacoes_pendentes`, com `detalhes` vindos do `payload` da tool (`area`/`data` ou `nome`/`data`).
3. Em `POST /confirmacoes`, a API muda o status de `pendente` para `aprovada`/`negada` com um UPDATE atômico (senão responde `409`). Depois envia ao Runner um `FunctionResponse(name="adk_request_confirmation", id=..., response={"confirmed": ...})`.
4. O `RunnerAurora` entrega essa resposta **ao agente autor da chamada** (`autor_da_confirmacao`). O processador de confirmação do ADK só reexecuta a tool quando a resposta chega a esse agente. Se chegasse ao principal, seria ignorada em silêncio. Foi o que aconteceu nos testes com a retomada (`ResumabilityConfig`) ligada e com a escolha automática de agente do ADK 2.11.
5. A tool roda de novo com `tool_context.tool_confirmation` preenchido. Antes de gravar, ela "consome" a aprovação no banco (`aprovada` vira `executada`, atômico).

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

| Onde | Trecho |
|---|---|
| `aurora/tools/reservas.py`, `reservar_area` (linhas 97-125) | `if area_info["taxa"] > 0:` → sem `tool_context.tool_confirmation`, chama `tool_context.request_confirmation(...)` e devolve `aguardando_confirmacao` sem gravar nada. A taxa vem do banco, não do modelo. Área com taxa 0 grava direto (sem confirmação). |
| `aurora/tools/visitantes.py`, `autorizar_visitante` (linhas 47-65) | Sempre pede confirmação. Não existe caminho no código que grave visitante sem passar por ela. |
| `aurora/api.py`, `responder_confirmacao` (linhas 157-190) | `banco.responder_confirmacao(...)` falso → `409` sem executar nada. Só depois monta o `FunctionResponse` e retoma o agente autor (`autor_da_confirmacao`). |
| `aurora/banco.py`, `responder_confirmacao` (linha 349) | `UPDATE confirmacoes SET status=... WHERE id=? AND session_id=? AND status='pendente'`: só uma confirmação **pendente desta sessão** muda de estado, uma única vez. Id inexistente, de outra sessão ou já respondido → `rowcount == 0` → `409`. |
| `aurora/banco.py`, `consumir_aprovacao` (linha 370), chamado nas tools | `aprovada` → `executada` atômico, antes de gravar: a ação roda no máximo uma vez por aprovação e só se a **rota de confirmações** registrou a aprovação. |

**Por que não depende do modelo:** a decisão de pedir confirmação é um `if` sobre a taxa da área (ou incondicional para visitantes), e a aprovação só existe quando a API grava `aprovada` na tabela. Texto do morador como "já estou confirmando aqui" chega ao modelo como mensagem, nunca como `FunctionResponse`, então não preenche `tool_confirmation`. Mesmo que o modelo chamasse a tool de novo, ela abriria outra confirmação pendente.

### Garantia 2: cada sessão pertence a um apartamento

| Onde | Trecho |
|---|---|
| `aurora/api.py`, `criar_sessao` (linhas 133-145) | O apartamento é gravado uma vez, na criação: `user_id=apartamento` e `state={"apartamento": apartamento}`. Nenhuma rota ou tool altera esse valor depois. |
| `aurora/tools/comum.py`, `apartamento_da_sessao` (linha 18) | Toda tool de dados obtém o apartamento daqui: lê `state["apartamento"]` e confere com o `user_id` da sessão. |
| `aurora/tools/reservas.py` e `aurora/tools/visitantes.py` | **Nenhuma tool tem parâmetro de apartamento.** O modelo não tem como escolher outro. |
| `aurora/banco.py`, `cancelar_reserva` (filtro `apartamento = ?` no SELECT e no UPDATE), `reservas_do_apartamento`, `visitantes_do_apartamento` | O filtro por apartamento está no SQL. Reserva de outro apartamento simplesmente "não existe" para a sessão. |
| `aurora/banco.py`, `data_livre` e `aurora/tools/reservas.py`, `consultar_disponibilidade` | A checagem de agenda devolve só `disponivel: true/false`. Quando a data está ocupada, `reservar_area` responde `indisponivel` sem código nem apartamento do dono. |

**Por que não depende do modelo:** o dado de outro apartamento nunca chega ao contexto do modelo, porque nenhuma tool o devolve. "Sou do 302" não muda nada: o 302 não é parâmetro de nenhuma tool, e o apartamento usado vem da sessão.

### Garantia 3: nada se perde no reinício

| Onde | Trecho |
|---|---|
| `aurora/api.py`, `lifespan` (linhas 52-62) | `SqliteSessionService(str(config.BANCO_SESSOES))`: sessões e eventos do ADK em `estado/sessoes.db`. |
| `aurora/banco.py` | Reservas, visitantes, confirmações e o mapa sessão→apartamento em `estado/condominio.db` (SQLite com WAL). |
| `aurora/runner.py`, `RunnerAurora` | O roteamento não guarda nada em memória. A escolha do agente sai dos eventos persistidos (`autor_da_confirmacao`), então uma confirmação pedida antes do reinício é aprovada normalmente depois dele (testado em `scripts/fluxo_avaliador.py`, "Extra"). |
| `aurora/banco.py`, `_novo_codigo` (linha 226) e tabela `codigos_emitidos` (linha 53) | Regra 5: todo código emitido fica registrado para sempre. Cancelar é *soft delete* (`status='cancelada'`) e nem a restauração apaga `codigos_emitidos`, então um código nunca se repete. |

### Garantia 4: o regulamento é consultado, não carregado

| Onde | Trecho |
|---|---|
| `aurora/agentes.py`, `assistente_aurora` | As instruções do agente principal não têm nenhum texto do regulamento. Ele só tem a tool `AgentTool(agent=especialista_regulamento)` (linha 156). |
| `aurora/agentes.py`, `especialista_regulamento` (linha 121) | As instruções têm só o **sumário** (títulos dos capítulos, `regulamento.sumario()`). O texto vem sob demanda. |
| `aurora/tools/regulamento.py`, `ler_capitulo` (linha 42) | Devolve **um** capítulo, recortado pelo cabeçalho `## Capítulo ...`. |
| `google.adk.tools.agent_tool.AgentTool.run_async` (ADK) | Roda o especialista com `InMemorySessionService()` próprio e devolve à sessão do morador só o texto final. A chamada de `ler_capitulo` e o capítulo lido nunca viram eventos da sessão do morador. |

**Por que não depende do modelo:** mesmo que o especialista leia o capítulo errado ou vários capítulos, isso acontece numa sessão descartável. Na sessão do morador entram só a pergunta (argumento do AgentTool) e a resposta curta. Nos testes, os eventos de S1 não continham nenhum trecho de 40 caracteres de nenhum capítulo.

### Garantia 5: dois moradores, uma reserva

| Onde | Trecho |
|---|---|
| `aurora/banco.py` (linhas 49-50) | `CREATE UNIQUE INDEX ux_reserva_ativa_area_data ON reservas (area, data) WHERE status = 'ativa'`: índice único **parcial**. Duas reservas ativas para a mesma área e data são impossíveis no banco, e uma reserva cancelada libera a data. |
| `aurora/banco.py`, `criar_reserva` (linha 237) | O `INSERT` roda numa transação `BEGIN IMMEDIATE`. Se outra reserva ativa já foi gravada (mesmo por uma requisição simultânea), o SQLite rejeita com `IntegrityError`, que vira `DataOcupada`. |
| `aurora/tools/reservas.py`, `reservar_area` (linha 129) | `except banco.DataOcupada:` → a tool devolve `indisponivel` e o agente responde normalmente ("data ocupada"). A API responde `200`, sem erro de servidor. |

**Por que não depende do modelo:** a conferência de agenda feita antes de pedir a confirmação é só conveniência. A exclusividade é decidida pelo índice único no instante do `INSERT`. Testado com 20 gravações simultâneas (1 vencedora) e com duas aprovações HTTP simultâneas (passo 14: as duas respostas `200`, uma reserva).

## Como rodar

**Pré-requisitos:** Python 3.12+, [uv](https://docs.astral.sh/uv/) e uma chave do [Google AI Studio](https://aistudio.google.com/apikey). Não há serviço externo: o armazenamento é SQLite em arquivo (`estado/`).

**Variáveis do `.env`** (copie de `.env.example`):

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio. |
| `MODELO_PRINCIPAL` | não | Modelo do `assistente_aurora` (padrão `gemini-3.1-flash-lite`). |
| `MODELO_RESERVAS` | não | Modelo do `especialista_reservas` (padrão `gemini-3.5-flash-lite`). |
| `MODELO_VISITANTES` | não | Modelo do `especialista_visitantes` (padrão `gemini-3.5-flash-lite`). |
| `MODELO_REGULAMENTO` | não | Modelo do `especialista_regulamento` (padrão `gemini-flash-lite-latest`). |

```bash
cp .env.example .env        # e preencha GOOGLE_API_KEY
uv sync
```

**Restaurar os dados iniciais** (com a API parada). Volta reservas e visitantes ao estado de `dados/*.json` e apaga sessões e confirmações:

```bash
uv run aurora-restaurar
```

**Subir a API** em `http://localhost:8000`:

```bash
uv run aurora-api
```

Na primeira subida, se `estado/` não existir, o banco é criado e carregado com os dados iniciais automaticamente.

**Conferir o fluxo do avaliador** (opcional, com a API no ar e os dados recém-restaurados):

```bash
uv run python scripts/fluxo_avaliador.py parte1
```

Depois pare a API (Ctrl+C), suba de novo sem restaurar e rode:

```bash
uv run python scripts/fluxo_avaliador.py parte2
```

**Inspecionar no `adk web`** (opcional, durante o desenvolvimento): `uv run adk web .` e escolha o app `aurora`.

### Contrato da API

| Rota | Resposta |
|---|---|
| `POST /sessoes` `{"apartamento": "101"}` | `201 {"session_id": "..."}` |
| `POST /sessoes/{id}/mensagens` `{"texto": "..."}` | `200 {"resposta": "...", "confirmacoes_pendentes": [{"id", "acao", "detalhes"}]}` |
| `POST /sessoes/{id}/confirmacoes` `{"id": "...", "confirmado": true}` | `200` (mesmo formato) ou `409` se não há confirmação pendente com esse id na sessão |
| `GET /sessoes/{id}/eventos` | `200` lista completa dos eventos da sessão, em ordem |
| `GET /apartamentos/{numero}/reservas` | `200 [{"codigo", "area", "data"}]` (reservas ativas) |
| `GET /apartamentos/{numero}/visitantes` | `200 [{"nome", "data"}]` |

Rotas com `{id}` respondem `404` para sessão inexistente.
