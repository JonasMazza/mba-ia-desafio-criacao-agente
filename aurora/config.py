"""Caminhos e configurações do projeto."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

# Sempre Gemini via Google AI Studio (chave GOOGLE_API_KEY), nunca Vertex AI.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

DADOS = RAIZ / "dados"                 # estado inicial (somente leitura)
ESTADO = RAIZ / "estado"               # estado vivo (fora do Git)
BANCO_CONDOMINIO = ESTADO / "condominio.db"   # reservas, visitantes, confirmações
BANCO_SESSOES = ESTADO / "sessoes.db"         # sessões e eventos do ADK

APP_NAME = "residencial_aurora"

# Um modelo por agente: no plano gratuito do AI Studio o limite de requisições
# por minuto é por modelo, então espalhar os agentes multiplica a vazão.
MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL") or "gemini-3.1-flash-lite"
MODELO_RESERVAS = os.getenv("MODELO_RESERVAS") or "gemini-3.5-flash-lite"
MODELO_VISITANTES = os.getenv("MODELO_VISITANTES") or "gemini-3.5-flash-lite"
MODELO_REGULAMENTO = os.getenv("MODELO_REGULAMENTO") or "gemini-flash-lite-latest"
