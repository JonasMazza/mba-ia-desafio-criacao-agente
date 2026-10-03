"""Armazenamento do condomínio em SQLite.

É a única porta de leitura e escrita de reservas, visitantes e confirmações.
As regras que não podem depender do modelo moram aqui, no próprio banco:

* exclusividade de reserva: índice único parcial em (area, data) para reservas
  ativas, verificado pelo SQLite no instante do INSERT (Garantia 5);
* código de reserva nunca repetido: tabela `codigos_emitidos`, que nunca é
  apagada, nem por cancelamento nem pela restauração (regra de negócio 5);
* confirmações: só mudam de estado com UPDATE condicional atômico, então uma
  confirmação é respondida uma vez e executada uma vez (Garantia 1).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

ESQUEMA = """
CREATE TABLE IF NOT EXISTS areas (
    id   TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS apartamentos (
    numero  TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservas (
    codigo       TEXT PRIMARY KEY,
    apartamento  TEXT NOT NULL,
    area         TEXT NOT NULL REFERENCES areas(id),
    data         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada')),
    criada_em    TEXT NOT NULL,
    cancelada_em TEXT
);

-- Garantia 5: no máximo UMA reserva ativa por área e data, garantido pelo
-- próprio SQLite no momento da gravação (não por uma consulta anterior).
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa_area_data
    ON reservas (area, data) WHERE status = 'ativa';

-- Regra 5: todo código já emitido fica registrado para sempre.
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL,
    criado_em   TEXT NOT NULL
);

-- Sessões da API: cada sessão pertence a um apartamento, fixado na criação.
CREATE TABLE IF NOT EXISTS sessoes (
    session_id  TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    criada_em   TEXT NOT NULL
);

-- Garantia 1: confirmações pedidas pelas tools. Só a API muda o status.
--   pendente -> aprovada -> executada
--   pendente -> negada
CREATE TABLE IF NOT EXISTS confirmacoes (
    id               TEXT PRIMARY KEY,   -- id da chamada adk_request_confirmation
    session_id       TEXT NOT NULL,
    chamada_original TEXT NOT NULL,      -- id da chamada da tool que pediu
    acao             TEXT NOT NULL,
    detalhes         TEXT NOT NULL,      -- JSON
    status           TEXT NOT NULL CHECK (status IN ('pendente', 'aprovada', 'negada', 'executada')),
    criada_em        TEXT NOT NULL,
    respondida_em    TEXT
);
CREATE INDEX IF NOT EXISTS ix_confirmacoes_sessao ON confirmacoes (session_id, status);
"""


class DataOcupada(Exception):
    """A área já tem uma reserva ativa nessa data."""


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    """Abre uma conexão curta; commit ao sair, rollback em caso de erro."""
    config.ESTADO.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        config.BANCO_CONDOMINIO, timeout=30, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transacao() -> Iterator[sqlite3.Connection]:
    """Transação de escrita que já nasce com o lock de escrita (BEGIN IMMEDIATE)."""
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def inicializar() -> None:
    """Cria o esquema e, se o banco estiver vazio, carrega os dados iniciais."""
    with conectar() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(ESQUEMA)
    with conectar() as conn:
        vazio = conn.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 0
    if vazio:
        restaurar_dados_iniciais()


def _ler_json(nome: str) -> list[dict[str, Any]]:
    return json.loads((config.DADOS / nome).read_text(encoding="utf-8"))


def restaurar_dados_iniciais() -> None:
    """Volta reservas, visitantes e cadastros ao estado de `dados/*.json`.

    Também apaga as sessões e confirmações da API. A tabela `codigos_emitidos`
    é preservada para que nenhum código já usado volte a ser gerado.
    """
    with conectar() as conn:
        conn.executescript(ESQUEMA)
    agora = _agora()
    with transacao() as conn:
        for tabela in ("reservas", "visitantes", "confirmacoes", "sessoes", "areas", "apartamentos"):
            conn.execute(f"DELETE FROM {tabela}")
        conn.executemany(
            "INSERT INTO areas (id, nome, taxa) VALUES (:id, :nome, :taxa)",
            _ler_json("areas.json"),
        )
        conn.executemany(
            "INSERT INTO apartamentos (numero, morador) VALUES (:numero, :morador)",
            _ler_json("apartamentos.json"),
        )
        for r in _ler_json("reservas.json"):
            conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, status, criada_em)"
                " VALUES (?, ?, ?, ?, 'ativa', ?)",
                (r["codigo"], r["apartamento"], r["area"], r["data"], agora),
            )
            conn.execute(
                "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)",
                (r["codigo"],),
            )
        conn.executemany(
            "INSERT INTO visitantes (apartamento, nome, data, criado_em)"
            " VALUES (:apartamento, :nome, :data, :criado_em)",
            [{**v, "criado_em": agora} for v in _ler_json("visitantes.json")],
        )


# ---------------------------------------------------------------- consultas


def listar_areas() -> list[dict[str, Any]]:
    with conectar() as conn:
        linhas = conn.execute("SELECT id, nome, taxa FROM areas ORDER BY nome").fetchall()
    return [dict(l) for l in linhas]


def apartamento_existe(numero: str) -> bool:
    with conectar() as conn:
        return conn.execute(
            "SELECT 1 FROM apartamentos WHERE numero = ?", (numero,)
        ).fetchone() is not None


def reservas_do_apartamento(apartamento: str) -> list[dict[str, Any]]:
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
            (apartamento,),
        ).fetchall()
    return [dict(l) for l in linhas]


def visitantes_do_apartamento(apartamento: str) -> list[dict[str, Any]]:
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(l) for l in linhas]


def data_livre(area_id: str, data: str) -> bool:
    """Responde só livre/ocupada; nunca diz de quem é a reserva."""
    with conectar() as conn:
        return conn.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area_id, data),
        ).fetchone() is None


# ---------------------------------------------------------------- gravações


def _novo_codigo(conn: sqlite3.Connection) -> str:
    """Gera um código inédito e o registra em `codigos_emitidos` na mesma transação."""
    while True:
        codigo = f"RSV-{secrets.token_hex(3).upper()}"
        try:
            conn.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            return codigo
        except sqlite3.IntegrityError:
            continue  # colisão: tenta outro código


def criar_reserva(apartamento: str, area_id: str, data: str) -> str:
    """Grava a reserva. A exclusividade é decidida aqui, pelo índice único.

    Se outra reserva ativa para a mesma área e data já tiver sido gravada,
    inclusive por uma requisição concorrente, o INSERT falha e levantamos
    `DataOcupada`. Não existe janela entre "conferir" e "gravar".
    """
    try:
        with transacao() as conn:
            codigo = _novo_codigo(conn)
            conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, status, criada_em)"
                " VALUES (?, ?, ?, ?, 'ativa', ?)",
                (codigo, apartamento, area_id, data, _agora()),
            )
    except sqlite3.IntegrityError as erro:
        if "reservas.area" in str(erro) or "ux_reserva_ativa" in str(erro):
            raise DataOcupada from erro
        raise
    return codigo


def cancelar_reserva(apartamento: str, *, codigo: str | None, area_id: str | None, data: str | None) -> dict[str, Any] | None:
    """Cancela uma reserva ativa DO APARTAMENTO informado. Devolve a reserva ou None.

    O filtro por apartamento está no próprio UPDATE: reserva de outro
    apartamento simplesmente não é encontrada.
    """
    filtros = ["apartamento = ?", "status = 'ativa'"]
    params: list[Any] = [apartamento]
    if codigo:
        filtros.append("codigo = ?")
        params.append(codigo.strip().upper())
    if area_id:
        filtros.append("area = ?")
        params.append(area_id)
    if data:
        filtros.append("data = ?")
        params.append(data)
    if len(params) == 1:
        return None
    onde = " AND ".join(filtros)
    with transacao() as conn:
        linhas = conn.execute(
            f"SELECT codigo, area, data FROM reservas WHERE {onde}", params
        ).fetchall()
        if len(linhas) != 1:
            return None
        alvo = dict(linhas[0])
        conn.execute(
            "UPDATE reservas SET status = 'cancelada', cancelada_em = ?"
            " WHERE codigo = ? AND apartamento = ? AND status = 'ativa'",
            (_agora(), alvo["codigo"], apartamento),
        )
    return alvo


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with transacao() as conn:
        conn.execute(
            "INSERT INTO visitantes (apartamento, nome, data, criado_em) VALUES (?, ?, ?, ?)",
            (apartamento, nome, data, _agora()),
        )


# ---------------------------------------------------------------- sessões


def registrar_sessao(session_id: str, apartamento: str) -> None:
    with transacao() as conn:
        conn.execute(
            "INSERT INTO sessoes (session_id, apartamento, criada_em) VALUES (?, ?, ?)",
            (session_id, apartamento, _agora()),
        )


def apartamento_da_sessao(session_id: str) -> str | None:
    with conectar() as conn:
        linha = conn.execute(
            "SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)
        ).fetchone()
    return linha["apartamento"] if linha else None


# ---------------------------------------------------------------- confirmações


def registrar_confirmacao(
    *, id: str, session_id: str, chamada_original: str, acao: str, detalhes: dict[str, Any]
) -> None:
    with transacao() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO confirmacoes"
            " (id, session_id, chamada_original, acao, detalhes, status, criada_em)"
            " VALUES (?, ?, ?, ?, ?, 'pendente', ?)",
            (id, session_id, chamada_original, acao, json.dumps(detalhes, ensure_ascii=False), _agora()),
        )


def confirmacoes_pendentes(session_id: str) -> list[dict[str, Any]]:
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT id, acao, detalhes FROM confirmacoes"
            " WHERE session_id = ? AND status = 'pendente' ORDER BY criada_em",
            (session_id,),
        ).fetchall()
    return [
        {"id": l["id"], "acao": l["acao"], "detalhes": json.loads(l["detalhes"])}
        for l in linhas
    ]


def responder_confirmacao(session_id: str, confirmacao_id: str, confirmado: bool) -> bool:
    """pendente -> aprovada/negada, de forma atômica. False se não estava pendente."""
    with transacao() as conn:
        cursor = conn.execute(
            "UPDATE confirmacoes SET status = ?, respondida_em = ?"
            " WHERE id = ? AND session_id = ? AND status = 'pendente'",
            ("aprovada" if confirmado else "negada", _agora(), confirmacao_id, session_id),
        )
        return cursor.rowcount == 1


def reabrir_confirmacao(session_id: str, confirmacao_id: str) -> None:
    """Desfaz a resposta quando a retomada do agente falhou antes de executar."""
    with transacao() as conn:
        conn.execute(
            "UPDATE confirmacoes SET status = 'pendente', respondida_em = NULL"
            " WHERE id = ? AND session_id = ? AND status IN ('aprovada', 'negada')",
            (confirmacao_id, session_id),
        )


def consumir_aprovacao(session_id: str, chamada_original: str) -> bool:
    """aprovada -> executada, de forma atômica. Só uma execução por aprovação.

    É chamada pela tool antes de gravar: mesmo que o ADK reentregue a
    confirmação, a ação só roda se a API registrou a aprovação do morador.
    """
    with transacao() as conn:
        cursor = conn.execute(
            "UPDATE confirmacoes SET status = 'executada'"
            " WHERE session_id = ? AND chamada_original = ? AND status = 'aprovada'",
            (session_id, chamada_original),
        )
        return cursor.rowcount == 1
