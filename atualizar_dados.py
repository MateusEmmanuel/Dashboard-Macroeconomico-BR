"""Baixa as séries do BCB (SGS) e do IBGE (SIDRA) e salva em dados/*.json.

Executado uma vez por dia pelo GitHub Actions (.github/workflows/atualizar-dados.yml)
e também pode ser rodado localmente:

    python atualizar_dados.py

O painel (app.py) lê os arquivos gerados aqui, em vez de consultar as APIs a cada acesso.
Se uma das fontes falhar, o arquivo anterior dela é mantido e o script termina com erro.
"""

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

PASTA_DADOS = Path(__file__).parent / "dados"
HEADERS = {"User-Agent": "dashboard-macro-br (github.com/MateusEmmanuel/Dashboard-Macroeconomico-BR)"}
TIMEOUT = (10, 120)  # (conexão, leitura) em segundos
TENTATIVAS = 4
JANELA_DIAS = 730  # o BCB responde melhor a consultas de ~2 anos por vez

# BCB: série -> anos de histórico (séries diárias aceitam no máximo 10 anos)
SERIES_BCB = {
    1: 10,  # Dólar comercial (venda)
    432: 10,  # Selic meta
    4390: 11,  # Selic efetiva acumulada no mês
}

# IBGE: (tabela, variável, nº de períodos, classificação)
SERIES_IBGE = [
    (1737, 2265, 120, None),  # IPCA - variação em 12 meses
    (1737, 63, 120, None),  # IPCA - variação mensal
    (5932, 6564, 40, "11255[90707]"),  # PIB - variação trimestre/mesmo trimestre do ano anterior
    (2072, 933, 40, None),  # PIB - valores correntes (R$ milhões)
    (6381, 4099, 120, None),  # PNAD Contínua - taxa de desocupação
]


def get_json(url: str):
    """GET com novas tentativas e espera crescente entre elas."""
    ultimo_erro = None
    for tentativa in range(TENTATIVAS):
        try:
            resposta = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resposta.raise_for_status()
            return resposta.json()
        except Exception as erro:  # noqa: BLE001
            ultimo_erro = erro
            print(f"  tentativa {tentativa + 1}/{TENTATIVAS} falhou: {type(erro).__name__}: {erro}")
            if tentativa < TENTATIVAS - 1:
                time.sleep(5 * (tentativa + 1))
    raise RuntimeError(f"Falha ao consultar {url}: {ultimo_erro}")


def janela_sem_dados(resposta) -> bool:
    """O BCB responde {'erro': {'statusCode': 404, 'detail': '...Value(s) not found'}}
    quando o período consultado não tem nenhum valor."""
    erro = resposta.get("erro") if isinstance(resposta, dict) else None
    return isinstance(erro, dict) and (
        erro.get("statusCode") == 404 or "not found" in str(erro.get("detail", "")).lower()
    )


def baixar_bcb(codigo: int, anos: int) -> list:
    """Baixa uma série do SGS em janelas de ~2 anos e junta os resultados."""
    hoje = date.today()
    atual = hoje - timedelta(days=365 * anos)
    registros = []

    while atual <= hoje:
        fim = min(atual + timedelta(days=JANELA_DIAS - 1), hoje)
        url = (
            f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
            f"?formato=json&dataInicial={atual:%d/%m/%Y}&dataFinal={fim:%d/%m/%Y}"
        )
        parte = get_json(url)
        if janela_sem_dados(parte):
            parte = []  # janela sem valores (ex.: hoje ainda não divulgado): não é erro
        elif not isinstance(parte, list):
            raise RuntimeError(
                f"Resposta inesperada do BCB para a série {codigo} "
                f"({atual:%d/%m/%Y} a {fim:%d/%m/%Y}): {parte}"
            )
        registros.extend(parte)
        atual = fim + timedelta(days=1)
        time.sleep(0.5)

    if not registros:
        raise RuntimeError(f"A série SGS {codigo} não retornou dados.")

    # Séries mensais podem repetir o mês nas bordas das janelas: mantém uma linha por data.
    registros = list({r["data"]: r for r in registros}.values())

    print(f"  {len(registros)} registros, último: {registros[-1]['data']}")
    return registros


def baixar_ibge(tabela: int, variavel: int, n_periodos: int, classificacao) -> dict:
    """Baixa uma variável do SIDRA e devolve {código do período: valor}."""
    url = (
        f"https://servicodados.ibge.gov.br/api/v3/agregados/{tabela}"
        f"/periodos/-{n_periodos}/variaveis/{variavel}?localidades=N1[all]"
    )
    if classificacao:
        url += f"&classificacao={classificacao}"

    try:
        serie = get_json(url)[0]["resultados"][0]["series"][0]["serie"]
    except (IndexError, KeyError, TypeError) as erro:
        raise RuntimeError(f"Estrutura inesperada do IBGE (tabela {tabela}, variável {variavel}).") from erro

    if not serie:
        raise RuntimeError(f"Tabela {tabela}, variável {variavel}: série vazia.")
    return serie


def coletar_bcb() -> dict:
    series = {}
    for codigo, anos in SERIES_BCB.items():
        print(f"BCB, série {codigo}...")
        series[str(codigo)] = baixar_bcb(codigo, anos)
    return series


def coletar_ibge() -> dict:
    series = {}
    for tabela, variavel, n_periodos, classificacao in SERIES_IBGE:
        print(f"IBGE, tabela {tabela}, variável {variavel}...")
        series[f"{tabela}_{variavel}"] = baixar_ibge(tabela, variavel, n_periodos, classificacao)
    return series


def salvar(nome: str, series: dict) -> None:
    PASTA_DADOS.mkdir(exist_ok=True)
    conteudo = {"atualizado_em": datetime.now(timezone.utc).isoformat(), "series": series}
    (PASTA_DADOS / f"{nome}.json").write_text(
        json.dumps(conteudo, ensure_ascii=False), encoding="utf-8"
    )


def main() -> int:
    falhas = []
    for nome, coletar in (("ibge", coletar_ibge), ("bcb", coletar_bcb)):
        try:
            salvar(nome, coletar())
            print(f"{nome}: OK")
        except Exception as erro:  # noqa: BLE001
            print(f"{nome}: FALHOU ({erro}). Mantendo o arquivo anterior, se existir.")
            falhas.append(nome)
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())