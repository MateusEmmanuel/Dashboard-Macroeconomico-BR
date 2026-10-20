# 📊 Dashboard Macroeconômico do Brasil

Painel interativo em **Python + Streamlit** para acompanhar os principais indicadores da economia brasileira, com dados coletados automaticamente das APIs oficiais do **Banco Central (SGS)** e do **IBGE (SIDRA)**.

👉 **[Ver o dashboard ao vivo](https://dashboard-macroeconomico-br.streamlit.app/)**

<!-- Substitua pelo print do seu dashboard. Salve a imagem em imagens/dashboard.png -->
![Visão geral do dashboard](imagens)

## O que o dashboard mostra

| Aba | Indicadores | Fonte |
|---|---|---|
| Juros e câmbio | Selic meta, juro real ex-post (Selic vs. IPCA 12m), dólar comercial | BCB/SGS |
| Inflação | IPCA acumulado em 12 meses (com a meta de 3% e a tolerância de ±1,5 p.p.) e IPCA mensal | IBGE/SIDRA |
| Atividade e emprego | PIB (variação em volume vs. mesmo trimestre do ano anterior), PIB a preços correntes, taxa de desocupação (PNAD Contínua) | IBGE/SIDRA |

Também há indicadores resumidos no topo (último valor e variação em relação à observação anterior) e um seletor de histórico de 1 a 10 anos na barra lateral.

## Fontes dos dados

| Indicador | API | Código |
|---|---|---|
| Selic meta (% a.a.) | BCB/SGS | série 432 |
| Dólar comercial (venda) | BCB/SGS | série 1 |
| IPCA (mensal e 12 meses) | IBGE/SIDRA | tabela 1737, variáveis 63 e 2265 |
| PIB, variação em volume (trimestre / mesmo trimestre do ano anterior) | IBGE/SIDRA | tabela 5932, variável 6564 |
| PIB a preços correntes | IBGE/SIDRA | tabela 2072, variável 933 |
| Taxa de desocupação | IBGE/SIDRA | tabela 6381, variável 4099 |

## Como funciona

- Os dados são consultados diretamente nas APIs, sempre até a data atual (sem datas fixas no código).
- O resultado fica em cache por 6 horas (`st.cache_data`), e o botão **Atualizar dados agora** limpa o cache.
- Os códigos de período trimestrais do IBGE (`AAAAQQ`) são convertidos para trimestres, e não para meses.
- O juro real ex-post é uma aproximação: `(1 + Selic efetiva acumulada em 12 meses) / (1 + IPCA acumulado em 12 meses) - 1`.
- Séries diárias do BCB são limitadas a 10 anos por consulta, por isso o seletor de histórico vai até 10 anos.

## Como rodar localmente

```bash
git clone https://github.com/MateusEmmanuel/Dashboard-Macroeconomico-BR.git
cd Dashboard-Macroeconomico-BR

python -m venv .venv
# Windows: .venv\Scripts\activate    |    Mac/Linux: source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

O app abre em `http://localhost:8501`.

## Estrutura

```
├── app.py              # aplicação Streamlit (coleta, transformação e gráficos)
├── requirements.txt    # dependências
├── README.md
└── imagens/            # prints do dashboard
```

## Tecnologias

Python · Streamlit · Pandas · Plotly · Requests

## Próximos passos

- [ ] Indicadores fiscais (resultado primário e dívida/PIB)
- [ ] Setor externo (balança comercial e reservas internacionais)
- [ ] Expectativas de mercado (Boletim Focus)
- [ ] Download dos dados em CSV
