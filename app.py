"""Dashboard Macroeconômico do Brasil.

Coleta séries oficiais do Banco Central (SGS) e do IBGE (SIDRA) e exibe os
principais indicadores de acompanhamento do cenário macro em um painel Streamlit.

Como rodar:
    pip install -r requirements.txt
    streamlit run app.py
"""

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="Dashboard Macro BR", page_icon="📊", layout="wide")

TIMEOUT = 30  # segundos
CACHE_TTL = 6 * 3600  # os dados são recarregados a cada 6 horas

# Séries do Banco Central (SGS)
SGS_SELIC_META = 432  # Taxa Selic - meta (% a.a.)
SGS_DOLAR = 1  # Dólar comercial (venda) - R$/US$
SGS_SELIC_MENSAL = 4390  # Selic efetiva acumulada no mês (% a.m.)

# Tabelas do IBGE (SIDRA)
SIDRA_IPCA = 1737  # IPCA - série histórica
SIDRA_IPCA_MENSAL = 63  # variável: variação mensal (%)
SIDRA_IPCA_12M = 2265  # variável: variação acumulada em 12 meses (%)
SIDRA_PIB_VAR = 5932  # PIB - taxa de variação do índice de volume trimestral
SIDRA_PIB_VAR_ANUAL = 6564  # variável: trimestre / mesmo trimestre do ano anterior (%)
SIDRA_PIB_CLASSIF = "11255[90707]"  # classificação: PIB a preços de mercado
SIDRA_PIB_NOMINAL = 2072  # PIB - valores correntes
SIDRA_PIB_NOMINAL_VAR = 933  # variável: valores a preços correntes (R$ milhões)
SIDRA_DESEMPREGO = 6381  # PNAD Contínua - taxa de desocupação (trimestre móvel)
SIDRA_DESEMPREGO_VAR = 4099  # variável: taxa de desocupação (%)

META_INFLACAO = 3.0  # meta contínua vigente (% a.a.)
TOLERANCIA = 1.5  # p.p.


# --------------------------------------------------------------------------- #
# Coleta de dados
# --------------------------------------------------------------------------- #

# Dados Banco Central
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def dados_bacen(codigo: int, anos: int) -> pd.DataFrame:
    """Baixa uma série do SGS/BCB para os últimos `anos` (máx. 10 em séries diárias)."""
    hoje = date.today()
    inicio = hoje - timedelta(days=365 * anos)
    url = (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
        f"?formato=json&dataInicial={inicio:%d/%m/%Y}&dataFinal={hoje:%d/%m/%Y}"
    )
    resposta = requests.get(url, timeout=TIMEOUT)
    resposta.raise_for_status()
    tabela = pd.DataFrame(resposta.json())
    if tabela.empty:
        raise ValueError(f"A série SGS {codigo} não retornou dados no período.")
    tabela["data"] = pd.to_datetime(tabela["data"], dayfirst=True)
    tabela["valor"] = pd.to_numeric(tabela["valor"], errors="coerce")
    return tabela.dropna(subset=["valor"]).reset_index(drop=True)


# Dados IBGE
def _codigo_para_data(codigo: str, trimestral: bool) -> pd.Timestamp:
    """Converte o código de período do IBGE em data.

    Mensal / trimestre móvel: AAAAMM (o mês é o do fim do período).
    Trimestral: AAAAQQ, com QQ = 01..04 (e não o mês).
    """
    ano, resto = int(codigo[:4]), int(codigo[4:])
    mes = 3 * (resto - 1) + 1 if trimestral else resto
    return pd.Timestamp(year=ano, month=mes, day=1)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def dados_ibge(
    tabela: int,
    variavel: int,
    n_periodos: int,
    trimestral: bool = False,
    classificacao: str | None = None,
) -> pd.DataFrame:
    """Baixa uma variável do SIDRA/IBGE para os últimos `n_periodos` disponíveis."""
    url = (
        f"https://servicodados.ibge.gov.br/api/v3/agregados/{tabela}"
        f"/periodos/-{n_periodos}/variaveis/{variavel}?localidades=N1[all]"
    )
    if classificacao:
        url += f"&classificacao={classificacao}"
    resposta = requests.get(url, timeout=TIMEOUT)
    resposta.raise_for_status()
    serie = resposta.json()[0]["resultados"][0]["series"][0]["serie"]

    tabela_df = pd.DataFrame(list(serie.items()), columns=["codigo", "valor"])
    tabela_df["valor"] = pd.to_numeric(tabela_df["valor"], errors="coerce")  # "-" e "..." viram NaN
    tabela_df = tabela_df.dropna(subset=["valor"]).copy()
    tabela_df["período"] = tabela_df["codigo"].map(lambda c: _codigo_para_data(c, trimestral))
    if trimestral:
        tabela_df["rótulo"] = tabela_df["codigo"].map(lambda c: f"{int(c[4:])}T{c[2:4]}")
    return tabela_df.sort_values("período").reset_index(drop=True)


def carregar(nome: str, funcao, *args, **kwargs) -> pd.DataFrame | None:
    """Executa uma coleta e mostra um erro amigável na tela se ela falhar."""
    try:
        return funcao(*args, **kwargs)
    except Exception as erro:  # noqa: BLE001 - queremos exibir qualquer falha ao usuário
        st.error(f"Não foi possível carregar **{nome}**: {erro}")
        return None


# --------------------------------------------------------------------------- #
# Transformações
# --------------------------------------------------------------------------- #
def juro_real_ex_post(selic_mensal: pd.DataFrame, ipca_12m: pd.DataFrame) -> pd.DataFrame:
    """Juro real ex-post: Selic efetiva acumulada em 12 meses vs. IPCA acumulado em 12 meses."""
    fator_mensal = 1 + selic_mensal.set_index("data")["valor"] / 100
    selic_12m = (fator_mensal.rolling(12).apply(lambda x: x.prod(), raw=True) - 1) * 100
    ipca = ipca_12m.set_index("período")["valor"]
    base = pd.concat([selic_12m.rename("selic_12m"), ipca.rename("ipca_12m")], axis=1, join="inner").dropna()
    base["juro_real"] = ((1 + base["selic_12m"] / 100) / (1 + base["ipca_12m"] / 100) - 1) * 100
    base.index.name = "data"
    return base.reset_index()


# --------------------------------------------------------------------------- #
# Gráficos e indicadores
# --------------------------------------------------------------------------- #
def estilo(fig, titulo_y: str):
    fig.update_layout(
        yaxis_title=titulo_y,
        xaxis_title=None,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=50, b=10),
        legend_title_text="",
    )
    return fig


def grafico_linha(df, x, y, titulo, titulo_y):
    return estilo(px.line(df, x=x, y=y, title=titulo), titulo_y)


def indicador(coluna, rotulo, df, sufixo_delta=""):
    """Mostra o último valor e a variação em relação à observação anterior."""
    if df is None or df.empty:
        coluna.metric(rotulo, "—")
        return
    ultimo = df["valor"].iloc[-1]
    delta = None
    if len(df) > 1:
        delta = f"{ultimo - df['valor'].iloc[-2]:+.2f}{sufixo_delta}"
    coluna.metric(rotulo, f"{ultimo:.2f}", delta)


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
st.title("📊 Dashboard Macroeconômico do Brasil")
st.caption(
    "Indicadores de atividade, inflação, política monetária, câmbio e mercado de trabalho, "
    "com dados do Banco Central (SGS) e do IBGE (SIDRA) atualizados automaticamente."
)

with st.sidebar:
    st.header("Configurações")
    anos = st.slider("Histórico (anos)", min_value=1, max_value=10, value=5)
    if st.button("🔄 Atualizar dados agora"):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"Consulta feita em {date.today():%d/%m/%Y}. Cache de 6 horas.")

# Coleta
selic = carregar("Selic", dados_bacen, SGS_SELIC_META, anos)
dolar = carregar("Dólar", dados_bacen, SGS_DOLAR, anos)
selic_mensal = carregar("Selic efetiva (acumulada no mês)", dados_bacen, SGS_SELIC_MENSAL, anos + 1)
ipca_12m = carregar("IPCA em 12 meses", dados_ibge, SIDRA_IPCA, SIDRA_IPCA_12M, anos * 12)
ipca_mensal = carregar("IPCA mensal", dados_ibge, SIDRA_IPCA, SIDRA_IPCA_MENSAL, anos * 12)
pib_var = carregar(
    "PIB (variação anual)",
    dados_ibge,
    SIDRA_PIB_VAR,
    SIDRA_PIB_VAR_ANUAL,
    anos * 4,
    trimestral=True,
    classificacao=SIDRA_PIB_CLASSIF,
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
    "Taxa de desocupação", dados_ibge, SIDRA_DESEMPREGO, SIDRA_DESEMPREGO_VAR, anos * 12
)

# Indicadores principais
col1, col2, col3, col4, col5 = st.columns(5)
indicador(col1, "Selic meta (% a.a.)", selic, " p.p.")
indicador(col2, "Dólar (R$)", dolar)
indicador(col3, "IPCA 12m (%)", ipca_12m, " p.p.")
indicador(col4, "PIB a/a (%)", pib_var, " p.p.")
indicador(col5, "Desocupação (%)", desemprego, " p.p.")

aba_monetaria, aba_inflacao, aba_atividade = st.tabs(
    ["Juros e câmbio", "Inflação", "Atividade e emprego"]
)

with aba_monetaria:
    if selic is not None:
        st.plotly_chart(
            grafico_linha(selic, "data", "valor", "Taxa Selic - meta (% a.a.)", "% a.a."),
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
                ),
                width="stretch",
            )
            st.caption(
                "Aproximação: (1 + Selic efetiva acumulada em 12 meses) / (1 + IPCA acumulado em 12 meses) - 1."
            )
    if dolar is not None:
        st.plotly_chart(
            grafico_linha(dolar, "data", "valor", "Dólar comercial (R$/US$)", "R$"),
            width="stretch",
        )

with aba_inflacao:
    if ipca_12m is not None:
        fig = grafico_linha(
            ipca_12m, "período", "valor", "IPCA acumulado em 12 meses (%)", "% em 12 meses"
        )
        fig.add_hline(y=META_INFLACAO, line_dash="dash", annotation_text="Meta atual: 3%")
        fig.add_hrect(
            y0=META_INFLACAO - TOLERANCIA, y1=META_INFLACAO + TOLERANCIA, opacity=0.1, line_width=0
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "Faixa sombreada: tolerância de ±1,5 p.p. em torno da meta contínua de 3% vigente. "
            "Para períodos anteriores a 2025, as metas eram definidas ano a ano."
        )
    if ipca_mensal is not None:
        fig = estilo(
            px.bar(ipca_mensal, x="período", y="valor", title="IPCA - variação mensal (%)"), "% no mês"
        )
        st.plotly_chart(fig, width="stretch")

with aba_atividade:
    if pib_var is not None:
        fig = estilo(
            px.bar(
                pib_var,
                x="rótulo",
                y="valor",
                title="PIB - variação em relação ao mesmo trimestre do ano anterior (%)",
            ),
            "% a/a",
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
            ),
            width="stretch",
        )
    if pib_nominal is not None:
        with st.expander("PIB em valores correntes (R$ milhões)"):
            fig = estilo(
                px.bar(pib_nominal, x="rótulo", y="valor", title="PIB trimestral a preços correntes"),
                "R$ milhões",
            )
            fig.update_xaxes(type="category")
            st.plotly_chart(fig, width="stretch")
            st.caption(
                "A frequência trimestral captura mudanças conjunturais e pontos de virada "
                "na atividade com maior agilidade em relação aos dados anuais consolidados."    
            )

with st.expander("Fontes e metodologia"):
    st.markdown(
        f"""
- **Selic meta** - BCB/SGS, série {SGS_SELIC_META}
- **Dólar comercial (venda)** - BCB/SGS, série {SGS_DOLAR}
- **Selic efetiva acumulada no mês** (base do juro real) - BCB/SGS, série {SGS_SELIC_MENSAL}
- **IPCA** - IBGE/SIDRA, tabela {SIDRA_IPCA} (variáveis {SIDRA_IPCA_MENSAL} e {SIDRA_IPCA_12M})
- **PIB (variação em volume, trimestre / mesmo trimestre do ano anterior)** - IBGE/SIDRA,
  tabela {SIDRA_PIB_VAR}, variável {SIDRA_PIB_VAR_ANUAL}
- **PIB a preços correntes** - IBGE/SIDRA, tabela {SIDRA_PIB_NOMINAL}, variável {SIDRA_PIB_NOMINAL_VAR}
- **Taxa de desocupação** - IBGE/SIDRA (PNAD Contínua), tabela {SIDRA_DESEMPREGO},
  variável {SIDRA_DESEMPREGO_VAR}

Os dados são consultados diretamente nas APIs oficiais e ficam em cache por 6 horas.
"""
    )
