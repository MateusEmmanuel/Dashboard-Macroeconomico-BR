"""Dashboard Macroeconômico do Brasil.

Exibe os principais indicadores de acompanhamento do cenário macro em um painel
Streamlit. Os dados oficiais do Banco Central (SGS) e do IBGE (SIDRA) são baixados
pelo script `atualizar_dados.py` (BCB: automático todo dia, via GitHub Actions; IBGE:
atualizado a cada divulgação) e salvos em `dados/bcb.json` e `dados/ibge.json`. O painel abre a partir desses arquivos
(rápido e sem depender das APIs) e o botão "Atualizar agora" consulta as APIs na hora,
buscando apenas os dados mais recentes e mesclando com os salvos.

Como rodar:
    pip install -r requirements.txt
    python atualizar_dados.py   # opcional: atualiza os arquivos em dados/
    streamlit run app.py
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="Dashboard Macro BR", page_icon="📊", layout="wide")

PASTA_DADOS = Path(__file__).parent / "dados"
ARQ_BCB = PASTA_DADOS / "bcb.json"
ARQ_IBGE = PASTA_DADOS / "ibge.json"
FUSO_BRASILIA = timezone(timedelta(hours=-3))
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

# Atualização ao vivo (botão da barra lateral)
HEADERS = {"User-Agent": "dashboard-macro-br (github.com/MateusEmmanuel/Dashboard-Macroeconomico-BR)"}
TIMEOUT = (5, 20)  # (conexão, leitura) em segundos, por tentativa
TENTATIVAS = 2

# Séries do Banco Central (SGS)
SGS_SELIC_META = 432  # Taxa Selic - meta (% a.a.)
SGS_DOLAR = 1  # Dólar comercial (venda) - R$/US$
SGS_SELIC_MENSAL = 4390  # Selic efetiva acumulada no mês (% a.m.)

# Tabelas do IBGE (SIDRA)
# Obs.: a classificação do PIB (11255[90707] = PIB a preços de mercado) é aplicada
# no momento do download, em atualizar_dados.py.
SIDRA_IPCA = 1737  # IPCA - série histórica
SIDRA_IPCA_MENSAL = 63  # variável: variação mensal (%)
SIDRA_IPCA_12M = 2265  # variável: variação acumulada em 12 meses (%)
SIDRA_PIB_VAR = 5932  # PIB - taxa de variação do índice de volume trimestral
SIDRA_PIB_VAR_ANUAL = 6564  # variável: taxa trimestre contra trimestre imediatamente anterior (%)
SIDRA_PIB_NOMINAL = 2072  # PIB - valores correntes
SIDRA_PIB_NOMINAL_VAR = 933  # variável: valores a preços correntes (R$ milhões)
SIDRA_DESEMPREGO = 6381  # PNAD Contínua - taxa de desocupação (trimestre móvel)
SIDRA_DESEMPREGO_VAR = 4099  # variável: taxa de desocupação (%)

META_INFLACAO = 3.0  # meta contínua vigente (% a.a.)
TOLERANCIA = 1.5  # p.p.

# Séries consultadas pelo botão "Atualizar agora": (nome, ...)
ATUALIZAR_BCB = [
    ("Selic meta", SGS_SELIC_META),
    ("Dólar", SGS_DOLAR),
    ("Selic efetiva", SGS_SELIC_MENSAL),
]
ATUALIZAR_IBGE = [  # (nome, tabela, variável, classificação)
    ("IPCA 12 meses", SIDRA_IPCA, SIDRA_IPCA_12M, None),
    ("IPCA mensal", SIDRA_IPCA, SIDRA_IPCA_MENSAL, None),
    ("PIB (var. trimestral)", SIDRA_PIB_VAR, SIDRA_PIB_VAR_ANUAL, "11255[90707]"),
    ("PIB (valores correntes)", SIDRA_PIB_NOMINAL, SIDRA_PIB_NOMINAL_VAR, None),
    ("Desocupação", SIDRA_DESEMPREGO, SIDRA_DESEMPREGO_VAR, None),
]


# --------------------------------------------------------------------------- #
# Leitura dos dados (arquivos gerados por atualizar_dados.py)
# --------------------------------------------------------------------------- #

@st.cache_data(show_spinner=False)
def _ler_json(caminho: str, versao: float) -> dict:
    """Lê um JSON de dados. `versao` (data de modificação) invalida o cache."""
    return json.loads(Path(caminho).read_text(encoding="utf-8"))


def ler_arquivo(caminho: Path) -> dict:
    """Lê um arquivo de dados; o cache é renovado sempre que o arquivo muda."""
    return _ler_json(str(caminho), caminho.stat().st_mtime)


def data_atualizacao(caminho: Path) -> str:
    """Data/hora (Brasília) em que o arquivo de dados foi gerado."""
    try:
        momento = datetime.fromisoformat(ler_arquivo(caminho)["atualizado_em"])
        return f"{momento.astimezone(FUSO_BRASILIA):%d/%m/%Y %H:%M}"
    except Exception:  # noqa: BLE001 - arquivo ausente ou em formato inesperado
        return "indisponível"


def obter_serie_bcb(codigo: int) -> list:
    """Série do BCB: versão atualizada ao vivo nesta sessão ou, senão, a do arquivo."""
    ao_vivo = st.session_state.get("ao_vivo", {}).get("bcb", {})
    if str(codigo) in ao_vivo:
        return ao_vivo[str(codigo)]
    return ler_arquivo(ARQ_BCB)["series"][str(codigo)]


def obter_serie_ibge(tabela: int, variavel: int) -> dict:
    """Série do IBGE: versão atualizada ao vivo nesta sessão ou, senão, a do arquivo."""
    chave = f"{tabela}_{variavel}"
    ao_vivo = st.session_state.get("ao_vivo", {}).get("ibge", {})
    if chave in ao_vivo:
        return ao_vivo[chave]
    return ler_arquivo(ARQ_IBGE)["series"][chave]


# --------------------------------------------------------------------------- #
# Atualização ao vivo (botão): busca só os dados recentes e mescla com os salvos
# --------------------------------------------------------------------------- #

def _get_json(url: str):
    """GET com poucas tentativas; usado pelo botão (timeouts curtos)."""
    ultimo_erro = None
    for tentativa in range(TENTATIVAS):
        try:
            resposta = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resposta.raise_for_status()
            return resposta.json()
        except requests.exceptions.RequestException as erro:
            ultimo_erro = erro
            if tentativa < TENTATIVAS - 1:
                time.sleep(1)
    raise ultimo_erro


def _baixar_bcb_recente(codigo: int, base: list) -> list:
    """Baixa os últimos dias da série e mescla com `base` (lista de {data, valor})."""
    hoje = datetime.now(FUSO_BRASILIA).date()
    inicio = hoje - timedelta(days=90)

    if base:
        ultima = datetime.strptime(base[-1]["data"], "%d/%m/%Y").date()
        inicio = ultima - timedelta(days=10)  # sobreposição pequena para não deixar buracos

    url = (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
        f"?formato=json&dataInicial={inicio:%d/%m/%Y}&dataFinal={hoje:%d/%m/%Y}"
    )
    novos = _get_json(url)

    if not isinstance(novos, list):
        raise ValueError(f"Resposta inesperada do BCB para a série {codigo}.")

    por_data = {r["data"]: r["valor"] for r in base}
    por_data.update({r["data"]: r["valor"] for r in novos})

    return [
        {"data": d, "valor": v}
        for d, v in sorted(por_data.items(), key=lambda i: datetime.strptime(i[0], "%d/%m/%Y"))
    ]


def _baixar_ibge_recente(tabela: int, variavel: int, classificacao, base: dict) -> dict:
    """Baixa os últimos períodos da variável e mescla com `base` ({período: valor})."""
    url = (
        f"https://servicodados.ibge.gov.br/api/v3/agregados/{tabela}"
        f"/periodos/-12/variaveis/{variavel}?localidades=N1[all]"
    )
    if classificacao:
        url += f"&classificacao={classificacao}"

    try:
        novos = _get_json(url)[0]["resultados"][0]["series"][0]["serie"]
    except (IndexError, KeyError, TypeError) as erro:
        raise ValueError(f"Estrutura inesperada do IBGE (tabela {tabela}).") from erro

    if not novos:
        raise ValueError(f"Tabela {tabela}: o IBGE não retornou períodos.")

    mesclado = dict(base)
    mesclado.update(novos)
    return mesclado


def atualizar_ao_vivo() -> None:
    """Consulta as APIs em paralelo e guarda o resultado na sessão do usuário.

    Cada série é independente: se uma falhar, as demais são atualizadas e a que falhou
    continua com os dados salvos.
    """
    def base_bcb(codigo):
        try:
            return obter_serie_bcb(codigo)
        except Exception:  # noqa: BLE001
            return []

    def base_ibge(tabela, variavel):
        try:
            return obter_serie_ibge(tabela, variavel)
        except Exception:  # noqa: BLE001
            return {}

    # As bases são lidas aqui (thread principal): o Streamlit não permite
    # acessar a sessão de dentro das threads.
    tarefas = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for nome, codigo in ATUALIZAR_BCB:
            futuro = pool.submit(_baixar_bcb_recente, codigo, base_bcb(codigo))
            tarefas.append((futuro, "bcb", str(codigo), nome))

        for nome, tabela, variavel, classificacao in ATUALIZAR_IBGE:
            futuro = pool.submit(
                _baixar_ibge_recente, tabela, variavel, classificacao, base_ibge(tabela, variavel)
            )
            tarefas.append((futuro, "ibge", f"{tabela}_{variavel}", nome))

    ao_vivo = st.session_state.get("ao_vivo", {"bcb": {}, "ibge": {}})
    erros = []

    for futuro, fonte, chave, nome in tarefas:
        try:
            ao_vivo[fonte][chave] = futuro.result()
        except Exception as erro:  # noqa: BLE001
            print(f"Falha ao atualizar {nome}: {type(erro).__name__}: {erro}")
            erros.append(nome)

    if len(erros) < len(tarefas):
        ao_vivo["momento"] = datetime.now(FUSO_BRASILIA)

    ao_vivo["erros"] = erros
    st.session_state["ao_vivo"] = ao_vivo


# Dados Banco Central
def dados_bacen(codigo: int, anos: int) -> pd.DataFrame:
    """Lê uma série do SGS/BCB para os últimos `anos`."""
    try:
        registros = obter_serie_bcb(codigo)
    except KeyError as erro:
        raise ValueError(f"A série SGS {codigo} não está disponível.") from erro

    tabela = pd.DataFrame(registros)

    if tabela.empty:
        raise ValueError(f"A série SGS {codigo} está vazia.")

    tabela["data"] = pd.to_datetime(tabela["data"], dayfirst=True)
    tabela["valor"] = pd.to_numeric(tabela["valor"], errors="coerce")
    tabela = tabela.dropna(subset=["valor"])

    inicio = pd.Timestamp.today().normalize() - pd.Timedelta(days=365 * anos)
    tabela = (
        tabela[tabela["data"] >= inicio]
        .drop_duplicates(subset="data", keep="last")  # evita datas repetidas
        .sort_values("data")
        .reset_index(drop=True)
    )

    if tabela.empty:
        raise ValueError(f"A série SGS {codigo} não tem dados no período.")

    return tabela


# Dados IBGE
def _codigo_para_data(codigo: str, trimestral: bool) -> pd.Timestamp:
    """Converte o código de período do IBGE em data.

    Mensal / trimestre móvel: AAAAMM.
    Trimestral: AAAAQQ, com QQ = 01..04.
    """
    ano, resto = int(codigo[:4]), int(codigo[4:])
    mes = 3 * (resto - 1) + 1 if trimestral else resto
    return pd.Timestamp(year=ano, month=mes, day=1)


def dados_ibge(
    tabela: int,
    variavel: int,
    n_periodos: int,
    trimestral: bool = False,
) -> pd.DataFrame:
    """Lê uma variável do SIDRA/IBGE para os últimos `n_periodos` disponíveis."""
    try:
        serie = obter_serie_ibge(tabela, variavel)
    except KeyError as erro:
        raise ValueError(
            f"A tabela {tabela}, variável {variavel}, não está disponível."
        ) from erro

    tabela_df = pd.DataFrame(
        list(serie.items()),
        columns=["codigo", "valor"],
    )

    tabela_df["valor"] = pd.to_numeric(tabela_df["valor"], errors="coerce")
    tabela_df = tabela_df.dropna(subset=["valor"]).copy()

    if tabela_df.empty:
        raise ValueError(f"A tabela {tabela} do IBGE não tem valores válidos.")

    tabela_df["período"] = tabela_df["codigo"].map(
        lambda c: _codigo_para_data(c, trimestral)
    )

    if trimestral:
        # AAAAQQ -> "3T21" (trimestre + ano com 2 dígitos)
        tabela_df["rótulo"] = tabela_df["codigo"].map(
            lambda c: f"{int(c[4:])}T{c[2:4]}"
        )

    tabela_df = tabela_df.sort_values("período").reset_index(drop=True)
    return tabela_df.tail(n_periodos).reset_index(drop=True)


def carregar(nome: str, funcao, *args, **kwargs) -> pd.DataFrame | None:
    """Carrega um indicador e mostra um aviso na tela se ele não estiver disponível."""
    try:
        return funcao(*args, **kwargs)

    except Exception as erro:  # noqa: BLE001
        # Os detalhes técnicos ficam nos logs do terminal, não na interface.
        print(f"Falha ao carregar {nome}: {type(erro).__name__}: {erro}")

        st.warning(
            f"⚠️ **{nome}:** indicador indisponível no momento. "
            "Os dados são atualizados diariamente; tente novamente mais tarde."
        )
        return None


# --------------------------------------------------------------------------- #
# Transformações
# --------------------------------------------------------------------------- #

def juro_real_ex_post(
    selic_mensal: pd.DataFrame, ipca_12m: pd.DataFrame
) -> pd.DataFrame:
    """Calcula o juro real ex-post a partir da Selic efetiva e do IPCA em 12 meses."""
    fator_mensal = 1 + selic_mensal.set_index("data")["valor"] / 100

    selic_12m = (
        fator_mensal.rolling(12).apply(lambda x: x.prod(), raw=True) - 1
    ) * 100

    ipca = ipca_12m.set_index("período")["valor"]

    base = pd.concat(
        [
            selic_12m.rename("selic_12m"),
            ipca.rename("ipca_12m"),
        ],
        axis=1,
        join="inner",
    ).dropna()

    base["juro_real"] = (
        (1 + base["selic_12m"] / 100)
        / (1 + base["ipca_12m"] / 100)
        - 1
    ) * 100

    base.index.name = "data"
    return base.reset_index()


# --------------------------------------------------------------------------- #
# Gráficos e indicadores
# --------------------------------------------------------------------------- #

def _ticks_em_portugues(fig) -> None:
    """Rótulos de data do eixo x em português (ex.: jan/22), com espaçamento
    adequado ao período exibido (mensal, trimestral, semestral ou anual)."""
    if not fig.data or fig.data[0].x is None:
        return

    datas = pd.to_datetime(pd.Series(list(fig.data[0].x)))
    if datas.empty:
        return

    inicio, fim = datas.min(), datas.max()
    meses = (fim.year - inicio.year) * 12 + (fim.month - inicio.month)
    passo = 1 if meses <= 14 else 3 if meses <= 40 else 6 if meses <= 90 else 12

    marcas = [
        m
        for m in pd.date_range(inicio.replace(day=1), fim, freq="MS")
        if m >= inicio.normalize() and (m.month - 1) % passo == 0
    ]
    if not marcas:
        return

    fig.update_xaxes(
        tickmode="array",
        tickvals=marcas,
        ticktext=[f"{MESES[m.month - 1]}/{m:%y}" for m in marcas],
    )


def estilo(
    fig,
    titulo_y: str,
    formato_valor: str = ".2f",
    prefixo: str = "",
    sufixo: str = "",
    formato_x: str | None = "%d/%m/%Y",
):
    """Aplica formatação numérica e estilo financeiro aos gráficos."""
    fig.update_layout(
        yaxis_title=titulo_y,
        xaxis_title=None,
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor="#172235",
            bordercolor="#34445c",
            font=dict(
                color="#F4F7FB",
                size=13,
                family="Arial, sans-serif",
            ),
            align="left",
        ),
        margin=dict(l=10, r=10, t=50, b=10),
        legend_title_text="",
    )

    fig.update_yaxes(tickformat=formato_valor)

    if formato_x:  # eixo de datas (os eixos de trimestres usam formato_x=None)
        fig.update_xaxes(hoverformat=formato_x)
        _ticks_em_portugues(fig)

    fig.update_traces(
        hovertemplate=(
            f"<b>{prefixo}%{{y:{formato_valor}}}{sufixo}</b>"
            "<extra></extra>"
        )
    )

    return fig


def grafico_linha(
    df,
    x,
    y,
    titulo,
    titulo_y,
    formato_valor=".2f",
    prefixo="",
    sufixo="",
    formato_x="%d/%m/%Y",
):
    """Cria um gráfico de linha com tooltip padronizado."""
    fig = px.line(df, x=x, y=y, title=titulo)

    return estilo(
        fig,
        titulo_y,
        formato_valor,
        prefixo,
        sufixo,
        formato_x,
    )


def rotulo_periodo(df, tipo: str) -> str | None:
    """Período de referência da última observação (ex.: 'tri. até ago/26')."""
    if df is None or df.empty:
        return None

    ultimo = df.iloc[-1]

    if tipo == "dia":
        return f"{ultimo['data']:%d/%m/%y}"

    if tipo == "trimestre":
        return str(ultimo["rótulo"])

    mes = f"{MESES[ultimo['período'].month - 1]}/{ultimo['período']:%y}"
    return f"tri. até {mes}" if tipo == "tri_movel" else mes


def indicador(
    coluna,
    rotulo,
    df,
    sufixo_delta="",
    formato=".2f",
    prefixo="",
    sufixo="",
    delta_color="normal",
    ignorar_repeticoes=False,
    periodo=None,
    ajuda=None,
):
    """Mostra o último valor e a variação em relação à observação anterior.

    delta_color: "normal" (alta = verde), "inverse" (alta = vermelho, ex.: inflação,
    desemprego) ou "off" (neutro). Com `ignorar_repeticoes`, a variação é calculada
    contra o último valor diferente (útil para a Selic, que muda só a cada Copom).
    """
    if df is None or df.empty:
        coluna.metric(rotulo, "—")
        return

    valores = df["valor"]
    ultimo = valores.iloc[-1]
    referencia = None

    if ignorar_repeticoes:
        anteriores = valores[valores != ultimo]
        if not anteriores.empty:
            referencia = anteriores.iloc[-1]
    elif len(valores) > 1:
        referencia = valores.iloc[-2]

    delta = None
    if referencia is not None:
        variacao = ultimo - referencia
        if abs(variacao) < 0.005:  # sem variação: mostra neutro
            delta, delta_color = f"0.00{sufixo_delta}", "off"
        else:
            delta = f"{variacao:+.2f}{sufixo_delta}"

    valor_formatado = format(ultimo, formato)
    coluna.metric(
        rotulo,
        f"{prefixo}{valor_formatado}{sufixo}",
        delta,
        delta_color=delta_color,
        help=ajuda,
    )


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #

st.title("📊 Dashboard Macroeconômico do Brasil")

st.caption(
    "Indicadores de atividade, inflação, política monetária, câmbio e mercado "
    "de trabalho, com dados do Banco Central (SGS) e do IBGE (SIDRA). Abre com a "
    "última atualização diária e pode ser atualizado na hora pelo botão da barra lateral."
)

with st.sidebar:
    st.header("Configurações")
    anos = st.slider("Histórico (anos)", min_value=1, max_value=10, value=5)

    if st.button(
        "🔄 Atualizar agora",
        help="Consulta o BCB e o IBGE neste momento e traz os dados mais recentes.",
    ):
        with st.spinner("Consultando BCB e IBGE..."):
            atualizar_ao_vivo()

    ao_vivo = st.session_state.get("ao_vivo", {})

    if ao_vivo.get("momento"):
        st.caption(f"✅ Atualizado ao vivo em {ao_vivo['momento']:%d/%m/%Y às %H:%M}.")

    if ao_vivo.get("erros") and not ao_vivo.get("momento"):
        # Aviso só quando nada pôde ser atualizado (ex.: BCB e IBGE fora do ar).
        st.warning(
            "Não foi possível atualizar agora. "
            "Os indicadores seguem com os dados salvos."
        )

    st.caption(
        f"Dados salvos (horário de Brasília; BCB atualizado diariamente, IBGE a cada divulgação): "
        f"BCB em {data_atualizacao(ARQ_BCB)}; IBGE em {data_atualizacao(ARQ_IBGE)}."
    )


# --------------------------------------------------------------------------- #
# Coleta
# --------------------------------------------------------------------------- #

selic = carregar("Selic", dados_bacen, SGS_SELIC_META, anos)
dolar = carregar("Dólar", dados_bacen, SGS_DOLAR, anos)

selic_mensal = carregar(
    "Selic efetiva (acumulada no mês)",
    dados_bacen,
    SGS_SELIC_MENSAL,
    anos + 1,
)

ipca_12m = carregar(
    "IPCA em 12 meses",
    dados_ibge,
    SIDRA_IPCA,
    SIDRA_IPCA_12M,
    anos * 12,
)

ipca_mensal = carregar(
    "IPCA mensal",
    dados_ibge,
    SIDRA_IPCA,
    SIDRA_IPCA_MENSAL,
    anos * 12,
)

pib_var = carregar(
    "PIB (variação trimestral)",
    dados_ibge,
    SIDRA_PIB_VAR,
    SIDRA_PIB_VAR_ANUAL,
    anos * 4,
    trimestral=True,
)

pib_nominal = carregar(
    "PIB (valores correntes)",
    dados_ibge,
    SIDRA_PIB_NOMINAL,
    SIDRA_PIB_NOMINAL_VAR,
    anos * 4,
    trimestral=True,
)

desemprego = carregar(
    "Taxa de desocupação",
    dados_ibge,
    SIDRA_DESEMPREGO,
    SIDRA_DESEMPREGO_VAR,
    anos * 12,
)


# --------------------------------------------------------------------------- #
# Indicadores principais
# --------------------------------------------------------------------------- #

col1, col2, col3, col4, col5 = st.columns(5)

indicador(
    col1,
    "Taxa Selic Meta",
    selic,
    " p.p.",
    formato=".2f",
    sufixo="%",
    ignorar_repeticoes=True,
    periodo=rotulo_periodo(selic, "dia"),
    ajuda="Variação em relação ao valor anterior da meta (última mudança do Copom).",
)

indicador(
    col2,
    "Dólar (fechamento)",
    dolar,
    formato=".2f",
    prefixo="R$ ",
    periodo=rotulo_periodo(dolar, "dia"),
    ajuda=(
        "Dólar comercial (venda), cotação de fechamento divulgada pelo BCB. A variação é "
        "em relação ao dia útil anterior. Não é a cotação em tempo real do mercado."
    ),
)

indicador(
    col3,
    "IPCA (12 meses)",
    ipca_12m,
    " p.p.",
    formato=".2f",
    sufixo="%",
    periodo=rotulo_periodo(ipca_12m, "mes"),
)

indicador(
    col4,
    "PIB (Var. Trimestral)",
    pib_var,
    " p.p.",
    formato=".2f",
    sufixo="%",
    periodo=rotulo_periodo(pib_var, "trimestre"),
    ajuda="Variação do PIB em relação ao trimestre imediatamente anterior, conforme divulgado pelo IBGE (Contas Nacionais Trimestrais).",
)

indicador(
    col5,
    "Taxa de Desocupação",
    desemprego,
    " p.p.",
    formato=".2f",
    sufixo="%",
    periodo=rotulo_periodo(desemprego, "tri_movel"),
    ajuda=(
        "PNAD Contínua em trimestre móvel: média dos 3 meses até o mês indicado. "
        "Difere do dado do trimestre fechado (jan-mar, abr-jun...) divulgado pelo IBGE."
    ),
)


aba_monetaria, aba_inflacao, aba_atividade = st.tabs(
    ["Juros e câmbio", "Inflação", "Atividade e emprego"]
)


# --------------------------------------------------------------------------- #
# Aba: Juros e câmbio
# --------------------------------------------------------------------------- #

with aba_monetaria:
    if selic is not None:
        st.plotly_chart(
            grafico_linha(
                selic,
                "data",
                "valor",
                "Taxa Selic - meta (% a.a.)",
                "% a.a.",
                ".2f",
                sufixo="%",
            ),
            width="stretch",
        )

    if selic_mensal is not None and ipca_12m is not None:
        juro = juro_real_ex_post(selic_mensal, ipca_12m)

        if not juro.empty:
            st.plotly_chart(
                grafico_linha(
                    juro,
                    "data",
                    "juro_real",
                    "Juro real ex-post (Selic efetiva 12m vs. IPCA 12m, % a.a.)",
                    "% a.a.",
                    ".2f",
                    sufixo="%",
                    formato_x="%m/%Y",
                ),
                width="stretch",
            )

            st.caption(
                "Aproximação: (1 + Selic efetiva acumulada em 12 meses) / "
                "(1 + IPCA acumulado em 12 meses) - 1."
            )

    if dolar is not None:
        st.plotly_chart(
            grafico_linha(
                dolar,
                "data",
                "valor",
                "Dólar comercial (R$/US$) - fechamento diário, BCB",
                "R$",
                ",.2f",
                prefixo="R$ ",
            ),
            width="stretch",
        )


# --------------------------------------------------------------------------- #
# Aba: Inflação
# --------------------------------------------------------------------------- #

with aba_inflacao:
    if ipca_12m is not None:
        fig = grafico_linha(
            ipca_12m,
            "período",
            "valor",
            "IPCA acumulado em 12 meses (%)",
            "% em 12 meses",
            ".2f",
            sufixo="%",
            formato_x="%m/%Y",
        )

        fig.add_hline(
            y=META_INFLACAO,
            line_dash="dash",
            annotation_text="Meta atual: 3%",
        )

        fig.add_hrect(
            y0=META_INFLACAO - TOLERANCIA,
            y1=META_INFLACAO + TOLERANCIA,
            fillcolor="#7FB3FF",
            opacity=0.18,
            line_width=0,
        )

        st.plotly_chart(fig, width="stretch")

        st.caption(
            "Faixa sombreada: tolerância de ±1,5 p.p. em torno da meta contínua "
            "de 3% vigente. Para períodos anteriores a 2025, as metas eram "
            "definidas ano a ano."
        )

    if ipca_mensal is not None:
        fig = estilo(
            px.bar(
                ipca_mensal,
                x="período",
                y="valor",
                title="IPCA - variação mensal (%)",
            ),
            "% no mês",
            ".2f",
            sufixo="%",
            formato_x="%m/%Y",
        )

        st.plotly_chart(fig, width="stretch")


# --------------------------------------------------------------------------- #
# Aba: Atividade e emprego
# --------------------------------------------------------------------------- #

with aba_atividade:
    if pib_var is not None:
        fig = estilo(
            px.bar(
                pib_var,
                x="rótulo",
                y="valor",
                title=(
                    "PIB - variação em relação ao trimestre "
                    "imediatamente anterior (%)"
                ),
            ),
            "% no trimestre",
            ".2f",
            sufixo="%",
            formato_x=None,
        )

        fig.update_xaxes(type="category")
        st.plotly_chart(fig, width="stretch")

    if desemprego is not None:
        st.plotly_chart(
            grafico_linha(
                desemprego,
                "período",
                "valor",
                "Taxa de desocupação - PNAD Contínua (trimestre móvel, %)",
                "%",
                ".2f",
                sufixo="%",
                formato_x="%m/%Y",
            ),
            width="stretch",
        )

    if pib_nominal is not None:
        with st.expander("PIB em valores correntes (R$ milhões)"):
            fig = estilo(
                px.bar(
                    pib_nominal,
                    x="rótulo",
                    y="valor",
                    title="PIB trimestral a preços correntes",
                ),
                "R$ milhões",
                ",.0f",
                sufixo=" milhões de R$",
                formato_x=None,
            )

            fig.update_xaxes(type="category")
            st.plotly_chart(fig, width="stretch")

            st.caption(
                "A frequência trimestral captura mudanças conjunturais e pontos "
                "de virada na atividade com maior agilidade em relação aos dados "
                "anuais consolidados."
            )


# --------------------------------------------------------------------------- #
# Fontes e metodologia
# --------------------------------------------------------------------------- #

with st.expander("Fontes e metodologia"):
    st.markdown(
        f"""
- **Selic meta** - BCB/SGS, série {SGS_SELIC_META}
- **Selic efetiva acumulada no mês** (base do juro real) - BCB/SGS, série {SGS_SELIC_MENSAL}
- **Dólar comercial (venda), fechamento diário** - BCB/SGS, série {SGS_DOLAR}
- **IPCA** - IBGE/SIDRA, tabela {SIDRA_IPCA} (variáveis {SIDRA_IPCA_MENSAL} e {SIDRA_IPCA_12M})
- **PIB (variação em volume, trimestre contra trimestre imediatamente anterior)** - IBGE/SIDRA,
  tabela {SIDRA_PIB_VAR}, variável {SIDRA_PIB_VAR_ANUAL}
- **PIB a preços correntes** - IBGE/SIDRA, tabela {SIDRA_PIB_NOMINAL}, variável {SIDRA_PIB_NOMINAL_VAR}
- **Taxa de desocupação** - IBGE/SIDRA (PNAD Contínua, trimestre móvel), tabela {SIDRA_DESEMPREGO},
  variável {SIDRA_DESEMPREGO_VAR}

**Atualização dos dados:** uma rotina automática (GitHub Actions) baixa as séries do BCB
todo dia, e as séries do IBGE são atualizadas a cada nova divulgação (IPCA, PNAD e PIB são
mensais ou trimestrais); o resultado é salvo no repositório e o painel abre a partir desses
arquivos, o que o torna rápido e estável. O botão
"Atualizar agora" consulta as APIs na hora, busca apenas os períodos mais recentes e os
junta aos dados salvos (a atualização vale para a sua sessão). Se uma API estiver
lenta ou fora do ar, o indicador correspondente continua com os dados salvos.

**Dólar:** o painel mostra o fechamento diário do dólar comercial (venda) divulgado pelo
BCB, e não a cotação em tempo real do mercado, que varia ao longo do dia.

**Desocupação:** a PNAD Contínua é divulgada em trimestres móveis (média dos 3 meses até o
mês mais recente, como "tri. até ago/26"), que se atualizam todo mês e podem diferir do dado do trimestre
fechado (ex.: abril-junho) citado em outras páginas do IBGE.
"""
    )