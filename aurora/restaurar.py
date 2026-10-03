"""Restaura reservas e visitantes ao estado de `dados/*.json`.

Também apaga as sessões (conversas e eventos) e as confirmações pendentes,
para que a próxima subida da API comece limpa. Rode com a API parada.
"""

from . import banco, config


def main() -> None:
    for sufixo in ("", "-wal", "-shm"):
        arquivo = config.BANCO_SESSOES.with_name(config.BANCO_SESSOES.name + sufixo)
        arquivo.unlink(missing_ok=True)
    banco.inicializar()
    banco.restaurar_dados_iniciais()
    print("Dados restaurados a partir de dados/*.json; sessões apagadas.")


if __name__ == "__main__":
    main()
