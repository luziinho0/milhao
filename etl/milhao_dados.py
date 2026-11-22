#!/usr/bin/env python3
"""
Robô de dados do Milhão.

Baixa arquivos públicos (B3 e CVM), calcula os indicadores e grava os JSON
que o site lê em data/. Pensado para rodar uma vez por dia no GitHub Actions.

Uso:
    python etl/milhao_dados.py --saida data
    python etl/milhao_dados.py --saida data --fonte-local tests/fixtures   # sem internet

Cada fonte roda isolada: se uma falhar, o robô registra o aviso em
data/meta.json e segue com as outras.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import math
import os
import statistics
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd

try:
    import requests
except ImportError:  # modo offline não precisa
    requests = None

HOJE = dt.date.today()
AVISOS: list[str] = []

URL_COTAHIST = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP"
URL_DFP = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip"
URL_ITR = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_{ano}.zip"
URL_FCA = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{ano}.zip"
URL_FII = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{ano}.zip"
URL_PROV_CIA = ("https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/"
                "CompanyCall/GetListedSupplementCompany/{param}")
URL_PROV_FII = ("https://sistemaswebb3-listados.b3.com.br/fundsProxy/"
                "fundsCall/GetListedSupplementFunds/{param}")


def aviso(msg: str) -> None:
    print("AVISO:", msg, file=sys.stderr)
    AVISOS.append(msg)


# --------------------------------------------------------------------------
# Download (ou leitura local)
# --------------------------------------------------------------------------
class Fonte:
    def __init__(self, local: str | None, cache: str | None):
        self.local = Path(local) if local else None
        self.cache = Path(cache) if cache else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)
        self.sessao = requests.Session() if (requests and not self.local) else None
        if self.sessao:
            self.sessao.headers["User-Agent"] = "Mozilla/5.0 (Milhao robo de dados)"

    def baixar(self, url: str, nome: str | None = None, cache_dias: float = 0.5,
               tentativas: int = 3, timeout: int = 120, silencioso: bool = False,
               guardar: bool = True) -> bytes | None:
        """guardar=False: arquivos enormes que mudam todo dia não vão para o cache (ele tem limite)."""
        nome = nome or url.rsplit("/", 1)[-1]
        if self.local:
            p = self.local / nome
            return p.read_bytes() if p.exists() else None
        if self.cache:
            p = self.cache / nome
            if p.exists() and (time.time() - p.stat().st_mtime) < cache_dias * 86400:
                return p.read_bytes()
        for tentativa in range(tentativas):
            try:
                r = self.sessao.get(url, timeout=timeout)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                if self.cache and guardar:
                    (self.cache / nome).write_bytes(r.content)
                return r.content
            except Exception as e:  # noqa: BLE001
                if tentativa == tentativas - 1:
                    if not silencioso:
                        aviso(f"falha ao baixar {url}: {e}")
                    return None
                time.sleep(2 * (tentativa + 1))
        return None


def ler_csv_zip(conteudo: bytes, contem: str) -> pd.DataFrame | None:
    """Lê do zip o CSV cujo nome contém o trecho pedido (padrão CVM: ; e latin-1)."""
    with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
        for n in z.namelist():
            if contem in n:
                with z.open(n) as f:
                    return pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
    return None


def num(x) -> float | None:
    try:
        v = float(str(x).replace(",", ".")) if x not in (None, "") else None
        return v if v is not None and math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def r(v, casas=4):
    return None if v is None or not math.isfinite(v) else round(v, casas)


def limpar(obj):
    """NaN e infinito não existem em JSON: viram null (senão o navegador não lê o arquivo)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: limpar(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [limpar(v) for v in obj]
    if hasattr(obj, "item"):  # números do numpy
        return limpar(obj.item())
    return obj


# --------------------------------------------------------------------------
# 1. Cotações (COTAHIST da B3)
# --------------------------------------------------------------------------
CODIGOS_BDI: dict = {}   # resumo dos códigos BDI do mercado à vista (diagnóstico para novas classes de ativo)


def ler_cotahist(texto: str) -> list[dict]:
    """Layout oficial COTAHIST.AAAA.TXT (posições 1-based do manual da B3)."""
    linhas = []
    for ln in texto.splitlines():
        if not ln.startswith("01") or len(ln) < 245:
            continue
        codbdi, tpmerc = ln[10:12], ln[24:27]
        if tpmerc == "010":
            chave = f"{codbdi}|{ln[39:49].strip()[:3]}"
            item = CODIGOS_BDI.setdefault(chave, {"linhas": 0, "exemplos": []})
            item["linhas"] += 1
            tk = ln[12:24].strip()
            if len(item["exemplos"]) < 12 and tk not in item["exemplos"]:
                item["exemplos"].append(tk)
        # 02 lote padrão (ações e parte dos BDRs), 12 FII, 13 Fiagro, 14 ETF, 34/36 BDRs
        if tpmerc != "010" or codbdi not in ("02", "12", "13", "14", "34", "36"):
            continue
        linhas.append({
            "data": ln[2:10],
            "bdi": codbdi,
            "ticker": ln[12:24].strip(),
            "nome": ln[27:39].strip(),
            "especi": ln[39:49].strip(),
            "abertura": int(ln[56:69]) / 100,
            "maxima": int(ln[69:82]) / 100,
            "minima": int(ln[82:95]) / 100,
            "fechamento": int(ln[108:121]) / 100,
            "volume": int(ln[170:188]) / 100,
            "fatcot": int(ln[210:217] or 1) or 1,
            "isin": ln[230:242].strip(),
        })
    return linhas


FIAGROS: set[str] = set()  # tickers que já apareceram com BDI 13 (a B3 passou a listar Fiagros junto dos ETFs)


def tipo_ativo(bdi: str, especi: str, ticker: str = "") -> str:
    """Classe do ativo pelo código BDI e pela especificação do COTAHIST."""
    if bdi == "12":
        return "fii"
    if bdi == "13" or ticker in FIAGROS:
        return "fiagro"
    if bdi == "14":
        return "etf"
    if bdi in ("34", "36") or (especi or "").startswith("DR"):
        return "bdr"
    return "acao"


def carregar_cotacoes(fonte: Fonte, anos: int) -> pd.DataFrame:
    partes = []
    for ano in range(HOJE.year - anos, HOJE.year + 1):
        b = fonte.baixar(URL_COTAHIST.format(ano=ano), cache_dias=0.3 if ano == HOJE.year else 30)
        if not b:
            aviso(f"COTAHIST {ano} indisponível")
            continue
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            texto = z.read(z.namelist()[0]).decode("latin-1")
        partes.extend(ler_cotahist(texto))
    if not partes:
        raise SystemExit("Sem cotações: não dá para montar a base.")
    df = pd.DataFrame(partes)
    df["fechamento"] = df["fechamento"] / df["fatcot"]
    df["data"] = pd.to_datetime(df["data"], format="%Y%m%d")
    return df.sort_values(["ticker", "data"])


# --------------------------------------------------------------------------
# 2. Cadastro e demonstrações (CVM)
# --------------------------------------------------------------------------
def carregar_fca(fonte: Fonte) -> tuple[pd.DataFrame, pd.DataFrame]:
    for ano in (HOJE.year, HOJE.year - 1):
        b = fonte.baixar(URL_FCA.format(ano=ano), cache_dias=7)
        if b:
            vm = ler_csv_zip(b, "valor_mobiliario")
            geral = ler_csv_zip(b, "_geral_")
            if vm is not None and geral is not None:
                return vm, geral
    aviso("FCA indisponível: sem ligação entre ticker e empresa")
    return pd.DataFrame(), pd.DataFrame()


DOCS = ("DRE", "BPA", "BPP", "DFC_MI", "DFC_MD")


def carregar_demonstracoes(fonte: Fonte, anos: int) -> dict[str, pd.DataFrame]:
    """Junta DFP (anual) e ITR (trimestral). Consolidado quando existe."""
    tabs: dict[str, list] = defaultdict(list)
    capital = []
    pedidos = [("DFP", URL_DFP, a) for a in range(HOJE.year - anos, HOJE.year)]
    pedidos += [("ITR", URL_ITR, a) for a in (HOJE.year - 1, HOJE.year)]
    for tipo, url, ano in pedidos:
        b = fonte.baixar(url.format(ano=ano), cache_dias=1 if ano >= HOJE.year - 1 else 30)
        if not b:
            continue
        for doc in DOCS:
            for escopo in ("con", "ind"):
                d = ler_csv_zip(b, f"_{doc}_{escopo}_")
                if d is not None and len(d):
                    d["DOC"], d["TIPO"], d["ESCOPO"] = doc, tipo, escopo
                    tabs[doc].append(d)
        c = ler_csv_zip(b, "composicao_capital")
        if c is not None:
            capital.append(c)
    out = {k: pd.concat(v, ignore_index=True) for k, v in tabs.items()}
    out["CAPITAL"] = pd.concat(capital, ignore_index=True) if capital else pd.DataFrame()
    for k in DOCS[:4]:
        if k not in out:
            aviso(f"demonstração {k} indisponível")
    return out


def preparar(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["CD_CVM"] = df["CD_CVM"].str.lstrip("0")
    df["VL"] = df["VL_CONTA"].map(num)
    mil = df["ESCALA_MOEDA"].str.upper().eq("MIL")
    df.loc[mil, "VL"] = df.loc[mil, "VL"] * 1000
    df["FIM"] = pd.to_datetime(df["DT_FIM_EXERC"])
    if "DT_INI_EXERC" in df:
        df["INI"] = pd.to_datetime(df["DT_INI_EXERC"], errors="coerce")
    # prefere consolidado; se a empresa só publica individual, usa individual
    tem_con = set(df.loc[df["ESCOPO"] == "con", "CD_CVM"])
    df = df[(df["ESCOPO"] == "con") | ~df["CD_CVM"].isin(tem_con)]
    # versão mais recente de cada documento
    df["VERSAO"] = pd.to_numeric(df["VERSAO"], errors="coerce")
    df = df.sort_values("VERSAO").drop_duplicates(
        ["CD_CVM", "FIM", "INI" if "INI" in df else "FIM", "CD_CONTA", "TIPO"], keep="last")
    return df


def conta(df: pd.DataFrame, codigo: str) -> pd.DataFrame:
    return df[df["CD_CONTA"] == codigo]


# --------------------------------------------------------------------------
# 3. Proventos (B3)
# --------------------------------------------------------------------------
def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


TIPOS_PROVENTO = {"DIVIDENDO": "Dividendo", "JRS CAP PROPRIO": "JCP", "JUROS SOBRE CAPITAL PROPRIO": "JCP",
                  "RENDIMENTO": "Rendimento", "AMORTIZACAO": "Amortização", "RESTITUICAO DE CAPITAL": "Restituição"}


def tipo_provento(label: str) -> str:
    l = (label or "").strip().upper()
    return TIPOS_PROVENTO.get(l, l.title() or "Provento")


def data_br(s):
    try:
        return dt.datetime.strptime(str(s)[:10], "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def achar_lista(obj, chave):
    if isinstance(obj, dict):
        if chave in obj:
            return obj[chave] or []
        for v in obj.values():
            res = achar_lista(v, chave)
            if res:
                return res
    if isinstance(obj, list):
        for v in obj:
            res = achar_lista(v, chave)
            if res:
                return res
    return []


def carregar_proventos(fonte: Fonte, emissores: dict[str, str]) -> tuple[dict, dict]:
    """emissores: código de 4 letras -> 'acao' | 'fii'. Devolve proventos por ISIN e eventos por ISIN."""
    from concurrent.futures import ThreadPoolExecutor

    def url_de(cod, tipo):
        if tipo == "fii":
            return URL_PROV_FII.format(param=b64({"cnpj": "0", "identifierFund": cod, "typeFund": 7}))
        return URL_PROV_CIA.format(param=b64({"issuingCompany": cod, "language": "pt-br"}))

    def buscar(item):
        cod, tipo = item
        return cod, fonte.baixar(url_de(cod, tipo), nome=f"proventos_{cod}.json", cache_dias=1,
                                 tentativas=2, timeout=20, silencioso=True)

    itens = sorted(emissores.items())
    # teste rápido: se a B3 não responder aos primeiros, não insiste no resto
    amostra = [buscar(i) for i in itens[:6]]
    if not fonte.local and not any(b for _, b in amostra):
        aviso("consulta de proventos da B3 não respondeu; site sai sem dividendos hoje")
        return {}, {}
    with ThreadPoolExecutor(max_workers=1 if fonte.local else 8) as ex:
        respostas = amostra + list(ex.map(buscar, itens[6:]))
    falhas = sum(1 for _, b in respostas if not b)
    if falhas:
        aviso(f"proventos: {falhas} de {len(respostas)} emissores sem resposta da B3")
    print(f"   proventos: {len(respostas) - falhas} emissores lidos", flush=True)

    dinheiro: dict[str, list] = defaultdict(list)
    eventos: dict[str, list] = defaultdict(list)
    for cod, b in respostas:
        if not b:
            continue
        try:
            js = json.loads(b.decode("utf-8"))
        except ValueError:
            continue
        for p in achar_lista(js, "cashDividends"):
            isin = p.get("isinCode") or p.get("assetIssued")
            valor = num(p.get("rate"))
            if not isin or not valor:
                continue
            dinheiro[isin].append({
                "tipo": tipo_provento(p.get("label") or p.get("typeStock")),
                "data_com": data_br(p.get("lastDatePrior")),
                "pagamento": data_br(p.get("paymentDate")),
                "valor": round(valor, 8),
            })
        for e in achar_lista(js, "stockDividends"):
            isin = e.get("isinCode") or e.get("assetIssued")
            fator = num(e.get("factor"))
            if isin and fator:
                eventos[isin].append({"tipo": (e.get("label") or "").upper(),
                                      "data_com": data_br(e.get("lastDatePrior")), "fator": fator})
    return dinheiro, eventos


def fator_ajuste(evento: dict) -> float | None:
    """Quantas ações novas por ação antiga. Conferir com os eventos reais da B3."""
    t, f = evento["tipo"], evento["fator"]
    if "DESDOBRAMENTO" in t or "BONIFICA" in t:
        return 1 + f / 100
    if "GRUPAMENTO" in t:
        return f / 100 if f < 100 else 1 / f
    return None


# --------------------------------------------------------------------------
# 4. FIIs (informe mensal CVM)
# --------------------------------------------------------------------------
def carregar_fii(fonte: Fonte) -> tuple[pd.DataFrame, pd.DataFrame]:
    geral, comp = [], []
    for ano in (HOJE.year - 1, HOJE.year):
        b = fonte.baixar(URL_FII.format(ano=ano), cache_dias=1)
        if not b:
            continue
        g, c = ler_csv_zip(b, "_geral_"), ler_csv_zip(b, "_complemento_")
        if g is not None:
            geral.append(g)
        if c is not None:
            comp.append(c)
    if not comp:
        aviso("informe mensal de FII indisponível")
        return pd.DataFrame(), pd.DataFrame()
    return pd.concat(geral, ignore_index=True), pd.concat(comp, ignore_index=True)


# --------------------------------------------------------------------------
# 5. Montagem
# --------------------------------------------------------------------------
def serie_preco(df_t: pd.DataFrame, eventos_isin: list) -> list[list]:
    """Fechamentos ajustados por desdobramento/grupamento; diário no último ano, semanal antes."""
    s = df_t.set_index("data")["fechamento"].astype(float)
    for e in eventos_isin:
        f, d = fator_ajuste(e), e.get("data_com")
        if f and d:
            s.loc[s.index <= pd.Timestamp(d)] = s.loc[s.index <= pd.Timestamp(d)] / f
    corte = s.index.max() - pd.Timedelta(days=370)
    antigo = s[s.index < corte].resample("W-FRI").last().dropna()
    antigo = antigo[antigo.index < corte]   # a última semana leva a data da sexta e repetiria um dia da parte diária
    s = pd.concat([antigo, s[s.index >= corte]])
    return [[d.strftime("%Y-%m-%d"), round(v, 2)] for d, v in s.items()]


def serie_ohlc(df_t: pd.DataFrame, eventos_isin: list) -> dict:
    """Candles diários (abertura, máxima, mínima, fechamento e volume financeiro), ajustados por desdobramento."""
    d = df_t.set_index("data")[["abertura", "maxima", "minima", "fechamento", "volume", "fatcot"]].copy()
    for c in ("abertura", "maxima", "minima"):
        d[c] = d[c] / d["fatcot"]  # o fechamento já vem dividido pelo fator de cotação
    for e in eventos_isin:
        f, dia = fator_ajuste(e), e.get("data_com")
        if f and dia:
            m = d.index <= pd.Timestamp(dia)
            d.loc[m, ["abertura", "maxima", "minima", "fechamento"]] = d.loc[m, ["abertura", "maxima", "minima", "fechamento"]] / f
    d = d[d["fechamento"] > 0]
    # alguns pregões vêm sem abertura/máxima/mínima: usa o fechamento para não desenhar candle quebrado
    for c in ("abertura", "maxima", "minima"):
        d.loc[d[c] <= 0, c] = d["fechamento"]
    d["maxima"] = d[["maxima", "abertura", "fechamento"]].max(axis=1)
    d["minima"] = d[["minima", "abertura", "fechamento"]].min(axis=1)
    casas = 2 if d["fechamento"].median() >= 1 else 4
    return {
        "t": [x.strftime("%Y-%m-%d") for x in d.index],
        "o": [round(v, casas) for v in d["abertura"]], "h": [round(v, casas) for v in d["maxima"]],
        "l": [round(v, casas) for v in d["minima"]], "c": [round(v, casas) for v in d["fechamento"]],
        "v": [int(v) for v in d["volume"]],
    }


def variacao(pontos, dias):
    if len(pontos) < 2:
        return None
    alvo = (dt.date.fromisoformat(pontos[-1][0]) - dt.timedelta(days=dias)).isoformat()
    base = next((p[1] for p in pontos if p[0] >= alvo), None)
    return div(pontos[-1][1] - base, base) if base else None


def ttm(dre: pd.DataFrame, cvm: str, codigo: str) -> tuple[float | None, str | None]:
    """Últimos 12 meses: anual mais recente + acumulado do ano atual - acumulado do ano anterior."""
    d = dre[(dre["CD_CVM"] == cvm) & (dre["CD_CONTA"] == codigo)]
    if d.empty:
        return None, None
    anual = d[(d["TIPO"] == "DFP") & (d["ORDEM_EXERC"].str.upper().str.startswith("ÚLTIMO"))]
    anual = anual.sort_values("FIM")
    itr = d[d["TIPO"] == "ITR"]
    if "INI" in itr:
        itr = itr[itr["INI"].dt.month.eq(1) & itr["INI"].dt.day.eq(1)]  # acumulado no ano
    ult_anual = anual.iloc[-1] if len(anual) else None
    if len(itr):
        ult = itr.sort_values("FIM").iloc[-1]
        if ult_anual is None or ult["FIM"] > ult_anual["FIM"]:
            ant = itr[(itr["FIM"] == ult["FIM"] - pd.DateOffset(years=1))]
            ano_ant = anual[anual["FIM"].dt.year == ult["FIM"].year - 1]
            if len(ant) and len(ano_ant):
                return ano_ant.iloc[-1]["VL"] + ult["VL"] - ant.iloc[-1]["VL"], ult["FIM"].date().isoformat()
    if ult_anual is not None:
        return ult_anual["VL"], ult_anual["FIM"].date().isoformat()
    return None, None


def saldo(bal: pd.DataFrame, cvm: str, codigo: str, data=None):
    d = bal[(bal["CD_CVM"] == cvm) & (bal["CD_CONTA"] == codigo)]
    if data is not None:
        d = d[d["FIM"] == data]
    else:
        d = d[d["ORDEM_EXERC"].str.upper().str.startswith("ÚLTIMO")]
    if d.empty:
        return None
    return d.sort_values("FIM").iloc[-1]["VL"]


def anual(df: pd.DataFrame, cvm: str, codigo: str) -> dict[int, float]:
    d = df[(df["CD_CVM"] == cvm) & (df["CD_CONTA"] == codigo) & (df["TIPO"] == "DFP")]
    out = {}
    for _, row in d.iterrows():
        if row["FIM"].month == 12 and row["VL"] is not None:
            out.setdefault(row["FIM"].year, row["VL"])  # ÚLTIMO vence PENÚLTIMO pela ordem de leitura
    for _, row in d[d["ORDEM_EXERC"].str.upper().str.startswith("ÚLTIMO")].iterrows():
        if row["FIM"].month == 12:
            out[row["FIM"].year] = row["VL"]
    return dict(sorted(out.items()))


def acoes_em_circulacao(capital: pd.DataFrame, cnpj: str) -> float | None:
    if capital.empty:
        return None
    col_cnpj = next((c for c in capital.columns if "CNPJ" in c.upper()), None)
    col_data = next((c for c in capital.columns if "DATA_REFER" in c.upper() or "DT_REFER" in c.upper()), None)
    # colunas reais da CVM: QT_ACAO_TOTAL_CAP_INTEGR e QT_ACAO_TOTAL_TESOURO
    col_tot = next((c for c in capital.columns if "TOTAL" in c.upper() and "INTEGR" in c.upper()), None)
    col_tes = next((c for c in capital.columns if "TOTAL" in c.upper() and "TESOUR" in c.upper()), None)
    if not (col_cnpj and col_tot):
        return None
    d = capital[capital[col_cnpj] == cnpj]
    if d.empty:
        return None
    d = d.sort_values(col_data) if col_data else d
    ult = d.iloc[-1]
    total = num(ult[col_tot]) or 0
    tes = num(ult[col_tes]) if col_tes else 0
    return (total - (tes or 0)) or None


def cagr(serie: dict[int, float], anos=5):
    if len(serie) < anos + 1:
        return None
    ks = sorted(serie)
    ini, fim = serie[ks[-1 - anos]], serie[ks[-1]]
    if not ini or not fim or ini <= 0 or fim <= 0:
        return None
    return (fim / ini) ** (1 / anos) - 1


def proventos_12m(lista, ate: str):
    ini = (dt.date.fromisoformat(ate) - dt.timedelta(days=365)).isoformat()
    return sum(p["valor"] for p in lista if p.get("data_com") and ini < p["data_com"] <= ate)


def proventos_por_ano(lista) -> dict[int, float]:
    out = defaultdict(float)
    for p in lista:
        if p.get("data_com"):
            out[int(p["data_com"][:4])] += p["valor"]
    return dict(out)


def mediana(vals):
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    return statistics.median(vals) if vals else None


INDICADORES = [
    # chave, nome, grupo, formato
    ("pl", "P/L", "Valuation", "x"), ("pvp", "P/VP", "Valuation", "x"),
    ("psr", "P/Receita (PSR)", "Valuation", "x"), ("p_ebit", "P/Ebit", "Valuation", "x"),
    ("p_ativo", "P/Ativo", "Valuation", "x"), ("p_capgiro", "P/Cap. Giro", "Valuation", "x"),
    ("p_acl", "P/Ativo Circ. Líq.", "Valuation", "x"), ("ev_ebit", "EV/Ebit", "Valuation", "x"),
    ("ev_ebitda", "EV/Ebitda", "Valuation", "x"), ("lpa", "LPA", "Valuation", "r$"),
    ("vpa", "VPA", "Valuation", "r$"),
    ("m_bruta", "Margem bruta", "Eficiência", "%"), ("m_ebit", "Margem Ebit", "Eficiência", "%"),
    ("m_ebitda", "Margem Ebitda", "Eficiência", "%"), ("m_liq", "Margem líquida", "Eficiência", "%"),
    ("giro", "Giro dos ativos", "Eficiência", "x"),
    ("roe", "ROE", "Rentabilidade", "%"), ("roa", "ROA", "Rentabilidade", "%"),
    ("roic", "ROIC", "Rentabilidade", "%"),
    ("dy", "Dividend yield", "Dividendos", "%"), ("payout", "Payout", "Dividendos", "%"),
    ("liq_corr", "Liquidez corrente", "Endividamento", "x"),
    ("dl_ebitda", "Dív. líq./Ebitda", "Endividamento", "x"), ("dl_ebit", "Dív. líq./Ebit", "Endividamento", "x"),
    ("dl_pl", "Dív. líq./Patrimônio", "Endividamento", "x"), ("db_pl", "Dív. bruta/Patrimônio", "Endividamento", "x"),
    ("pl_ativo", "Patrimônio/Ativos", "Endividamento", "x"), ("passivo_ativo", "Passivos/Ativos", "Endividamento", "x"),
    ("cagr_rec", "CAGR receitas 5 anos", "Crescimento", "%"), ("cagr_luc", "CAGR lucros 5 anos", "Crescimento", "%"),
]


def conta_lucro(dre: pd.DataFrame | None) -> str:
    """Lucro do período: 3.11 nas empresas em geral; bancos e seguradoras usam outra numeração."""
    if dre is None or dre.empty:
        return "3.11"
    nivel1 = dre[dre["CD_CONTA"].str.fullmatch(r"3\.\d\d", na=False)].drop_duplicates("CD_CONTA")
    ds = nivel1["DS_CONTA"].fillna("").str.lower()
    cand = nivel1[ds.str.contains("lucro") & ds.str.contains("per[ií]odo") & ~ds.str.contains("antes")
                  & ~ds.str.contains("continuad")]
    if len(cand):
        return sorted(cand["CD_CONTA"])[-1]
    return "3.11" if (dre["CD_CONTA"] == "3.11").any() else "3.11"


RE_CAPEX = r"(?:imobiliz|intang)"
RE_NAO_CAPEX = r"(?:venda|alien|receb|baixa|resgate|dividend)"


def linhas_capex(dfc: pd.DataFrame) -> pd.DataFrame:
    """Compra de imobilizado e intangível (linhas 6.02.xx da DFC), somadas por período."""
    ds = dfc["DS_CONTA"].fillna("").str.lower()
    d = dfc[dfc["CD_CONTA"].str.fullmatch(r"6\.02\.\d\d", na=False) & ds.str.contains(RE_CAPEX)
            & ~ds.str.contains(RE_NAO_CAPEX)]
    d = d[d["VL"].fillna(0) < 0]
    if d.empty:
        return d
    chaves = [c for c in ("CD_CVM", "TIPO", "ORDEM_EXERC", "FIM", "INI") if c in d]
    g = d.groupby(chaves, dropna=False, as_index=False)["VL"].sum()
    g["CD_CONTA"] = "CAPEX"
    return g


def fluxo_de_caixa(dfc: pd.DataFrame | None, cvm: str) -> dict:
    """Caixa das operações, investimentos, financiamentos e fluxo de caixa livre, por ano e nos 12 meses."""
    if dfc is None or dfc.empty:
        return {}
    capex = linhas_capex(dfc)
    anual_ = lambda cod, df=dfc: anual(df, cvm, cod) if df is not None and len(df) else {}  # noqa: E731
    fco, fci, fcf = anual_("6.01"), anual_("6.02"), anual_("6.03")
    cpx = anual_("CAPEX", capex)
    anos = sorted(set(fco) | set(fci))
    por_ano = {}
    for a in anos:
        c = cpx.get(a, 0.0) if fco.get(a) is not None else None
        por_ano[str(a)] = {"fco": fco.get(a), "fci": fci.get(a), "fcf": fcf.get(a), "capex": c,
                           "fcl": (fco[a] + c) if fco.get(a) is not None and c is not None else None}
    fco12, ref = ttm(dfc, cvm, "6.01")
    fci12, _ = ttm(dfc, cvm, "6.02")
    fcf12, _ = ttm(dfc, cvm, "6.03")
    cpx12, _ = ttm(capex, cvm, "CAPEX") if len(capex) else (0.0, None)
    doze = {"fco": fco12, "fci": fci12, "fcf": fcf12, "capex": cpx12 or 0.0,
            "fcl": (fco12 + (cpx12 or 0.0)) if fco12 is not None else None, "referencia": ref}
    return {"anual": por_ano, "12m": doze}


def montar_acao(t, preco, pontos, cad, dem, capital, provs) -> dict:
    cvm, cnpj = cad["cvm"], cad["cnpj"]
    dre, bpa, bpp = dem.get("DRE"), dem.get("BPA"), dem.get("BPP")
    dfc = dem.get("DFC_MI") if dem.get("DFC_MI") is not None else dem.get("DFC_MD")
    cod_lucro = conta_lucro(dre)
    receita, ref = ttm(dre, cvm, "3.01") if dre is not None else (None, None)
    bruto, _ = ttm(dre, cvm, "3.03") if dre is not None else (None, None)
    ebit, _ = ttm(dre, cvm, "3.05") if dre is not None else (None, None)
    lucro, _ = ttm(dre, cvm, cod_lucro) if dre is not None else (None, None)
    da = None
    if dfc is not None:
        dep = dfc[(dfc["CD_CVM"] == cvm) & dfc["CD_CONTA"].str.startswith("6.01.01")
                  & dfc["DS_CONTA"].str.contains("eprecia", na=False)]
        if len(dep):
            dep = dep[dep["TIPO"] == "DFP"].sort_values("FIM")
            if len(dep):
                da = abs(dep.iloc[-1]["VL"] or 0)
    ebitda = ebit + da if (ebit is not None and da) else None

    g = lambda bal, c: saldo(bal, cvm, c) if bal is not None else None  # noqa: E731
    ativo, ac = g(bpa, "1"), g(bpa, "1.01")
    caixa = (g(bpa, "1.01.01") or 0) + (g(bpa, "1.01.02") or 0)
    # patrimônio líquido: 2.03 nas empresas em geral; nos bancos fica em outra conta de 1º nível (ex.: 2.08)
    cod_pl = "2.03"
    if bpp is not None and len(bpp):
        nivel1 = bpp[bpp["CD_CONTA"].str.fullmatch(r"2\.\d\d", na=False)
                     & bpp["DS_CONTA"].str.contains("atrim", case=False, na=False)]
        if len(nivel1):
            cod_pl = nivel1["CD_CONTA"].iloc[0]
    pc, pnc, pl = g(bpp, "2.01"), g(bpp, "2.02"), g(bpp, cod_pl)
    divida = (g(bpp, "2.01.04") or 0) + (g(bpp, "2.02.01") or 0)
    passivo = (pc or 0) + (pnc or 0) if pc is not None else None
    dl = divida - caixa
    n = acoes_em_circulacao(capital, cnpj)
    # algumas empresas informam a quantidade de ações em milhares (ex.: Vale, Itaú):
    # se o P/VP sai absurdo e fica normal com x1000, a quantidade estava em milhares
    if n and pl and pl > 0:
        pvp_bruto = preco / (pl / n)
        if pvp_bruto < 0.02 and 0.05 <= pvp_bruto * 1000 <= 60:
            n *= 1000
    elif n and receita and receita > 0 and preco * n / receita < 0.001:
        n *= 1000
    vm = preco * n if n else None
    ev = vm + dl if vm is not None else None
    lpa, vpa = div(lucro, n), div(pl, n)

    lista = sorted(provs, key=lambda p: p.get("data_com") or "", reverse=True)
    hoje_iso = pontos[-1][0]
    prov12 = proventos_12m(lista, hoje_iso)
    por_ano = proventos_por_ano(lista)
    # média dos últimos 5 anos fechados, mas só dos anos que a B3 informa (ela costuma mandar pouco histórico)
    anos5 = [v for v in (por_ano.get(a, 0) for a in range(HOJE.year - 5, HOJE.year)) if v > 0]
    media5 = sum(anos5) / len(anos5) if anos5 else None

    ind = {
        "pl": div(preco, lpa) if lpa and lpa > 0 else None, "pvp": div(preco, vpa),
        "psr": div(vm, receita), "p_ebit": div(vm, ebit), "p_ativo": div(vm, ativo),
        "p_capgiro": div(vm, (ac - pc) if ac is not None and pc is not None else None),
        "p_acl": div(vm, (ac - passivo) if ac is not None and passivo is not None else None),
        "ev_ebit": div(ev, ebit), "ev_ebitda": div(ev, ebitda), "lpa": lpa, "vpa": vpa,
        "m_bruta": div(bruto, receita), "m_ebit": div(ebit, receita), "m_ebitda": div(ebitda, receita),
        "m_liq": div(lucro, receita), "giro": div(receita, ativo),
        "roe": div(lucro, pl), "roa": div(lucro, ativo),
        "roic": div(ebit * 0.66 if ebit is not None else None, (pl or 0) + dl if pl else None),
        "dy": div(prov12, preco), "payout": div(prov12 * n if n else None, lucro) if lucro and lucro > 0 else None,
        "liq_corr": div(ac, pc), "dl_ebitda": div(dl, ebitda), "dl_ebit": div(dl, ebit),
        "dl_pl": div(dl, pl), "db_pl": div(divida, pl), "pl_ativo": div(pl, ativo),
        "passivo_ativo": div(passivo, ativo),
    }
    rec_a = anual(dre, cvm, "3.01") if dre is not None else {}
    luc_a = anual(dre, cvm, cod_lucro) if dre is not None else {}
    ind["cagr_rec"], ind["cagr_luc"] = cagr(rec_a), cagr(luc_a)

    graham = math.sqrt(22.5 * lpa * vpa) if lpa and vpa and lpa > 0 and vpa > 0 else None
    # Bazin: com menos de 3 anos de histórico, vale o pago nos últimos 12 meses
    base_bazin = prov12 if (len(anos5) < 3 and prov12) else media5
    bazin = base_bazin / 0.06 if base_bazin else None

    # checklist com o que dá para calcular a partir das fontes abertas
    anos_bolsa = (dt.date.fromisoformat(hoje_iso) - dt.date.fromisoformat(pontos[0][0])).days / 365
    dy_anos = []
    for a in range(HOJE.year - 5, HOJE.year):
        fim_ano = [p[1] for p in pontos if p[0].startswith(str(a))]
        if fim_ano:
            dy_anos.append(div(por_ano.get(a, 0), fim_ano[-1]) or 0)
    liq = cad.get("liquidez")
    checklist = [
        {"item": "Negociada na bolsa há mais de 5 anos", "ok": anos_bolsa >= 4.9},
        {"item": "Lucro em todos os anos disponíveis", "ok": bool(luc_a) and all(v > 0 for v in luc_a.values())},
        {"item": "Pagou mais de 5% de dividendos por ano nos últimos 5 anos",
         "ok": len(dy_anos) == 5 and all(v > 0.05 for v in dy_anos)},
        {"item": "ROE acima de 10%", "ok": (ind["roe"] or 0) > 0.10},
        {"item": "Dívida líquida menor que o patrimônio", "ok": pl is not None and dl < pl},
        {"item": "Receita cresceu em 5 anos", "ok": (ind["cagr_rec"] or 0) > 0},
        {"item": "Lucro cresceu em 5 anos", "ok": (ind["cagr_luc"] or 0) > 0},
        {"item": "Liquidez diária acima de R$ 10 milhões", "ok": (liq or 0) > 10e6},
    ]

    hist = {}
    for a in sorted(set(rec_a) | set(luc_a)):
        fim_ano = [p[1] for p in pontos if p[0].startswith(str(a))]
        pl_a = None
        if bpp is not None:
            pl_a = saldo(bpp, cvm, cod_pl, pd.Timestamp(f"{a}-12-31"))
        lp = div(luc_a.get(a), n)
        hist[a] = {
            "receita": rec_a.get(a), "lucro": luc_a.get(a),
            "m_liq": div(luc_a.get(a), rec_a.get(a)),
            "roe": div(luc_a.get(a), pl_a),
            "pl": div(fim_ano[-1], lp) if fim_ano and lp and lp > 0 else None,
            "dy": div(por_ano.get(a, 0), fim_ano[-1]) if fim_ano else None,
            "proventos": por_ano.get(a, 0),
        }

    return {
        "ind": {k: r(v) for k, v in ind.items()},
        "base": {"receita_12m": receita, "lucro_12m": lucro, "ebit_12m": ebit, "patrimonio": pl,
                 "ativo": ativo, "divida_bruta": divida, "divida_liquida": dl, "acoes": n,
                 "valor_mercado": vm, "valor_firma": ev, "referencia": ref},
        "graham": r(graham, 2), "bazin": r(bazin, 2),
        "checklist": [{"item": c["item"], "ok": bool(c["ok"])} for c in checklist],
        "pontos": sum(1 for c in checklist if c["ok"]),
        "historico": {str(k): {kk: r(vv) for kk, vv in v.items()} for k, v in hist.items()},
        "fluxo": fluxo_de_caixa(dfc, cvm),
        "banco": bool(cod_lucro != "3.11" or (dre is not None and len(dre) and dre["DS_CONTA"].fillna("").str.contains(
            "intermedia", case=False).any())),
        "proventos": lista[:120], "media_proventos_5a": r(media5),
    }


def montar_fii(t, preco, pontos, info, provs) -> dict:
    lista = sorted(provs, key=lambda p: p.get("data_com") or "", reverse=True)
    hoje_iso = pontos[-1][0]
    prov12 = proventos_12m(lista, hoje_iso)
    vp = info.get("vp_cota")
    ult = lista[0]["valor"] if lista else None
    yields = {}
    for m in (1, 3, 6, 12):
        ini = (dt.date.fromisoformat(hoje_iso) - dt.timedelta(days=30.5 * m)).isoformat()
        yields[f"{m}m"] = r(div(sum(p["valor"] for p in lista if p.get("data_com") and p["data_com"] > ini), preco))
    return {
        "ind": {"dy": r(div(prov12, preco)), "pvp": r(div(preco, vp)), "vp_cota": r(vp, 2),
                "ultimo_rendimento": ult, "magic_number": math.ceil(preco / ult) if ult else None},
        "yields": yields,
        "fundo": {k: info.get(k) for k in ("cnpj", "segmento", "mandato", "gestao", "publico",
                                             "cotistas", "cotas", "patrimonio", "taxa_adm")},
        "proventos": lista[:120],
        "proventos_estimados": info.get("estimado", False),
    }


URL_SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados?formato=json&dataInicial={ini}&dataFinal={fim}"


def serie_bcb(fonte: Fonte, serie: int, anos: int = 5) -> list[tuple[str, float]]:
    """Série do SGS do Banco Central (dados públicos e gratuitos) como [(AAAA-MM-DD, valor)]."""
    fim = HOJE
    ini = fim - dt.timedelta(days=365 * anos + 5)
    url = URL_SGS.format(serie=serie, ini=ini.strftime("%d/%m/%Y"), fim=fim.strftime("%d/%m/%Y"))
    b = fonte.baixar(url, nome=f"sgs_{serie}.json", cache_dias=0.3, silencioso=True)
    if not b:
        return []
    try:
        dados = json.loads(b.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in dados if isinstance(dados, list) else []:
        try:
            d = dt.datetime.strptime(item["data"], "%d/%m/%Y").date().isoformat()
            out.append((d, float(str(item["valor"]).replace(",", "."))))
        except Exception:  # noqa: BLE001
            continue
    return out


def montar_indicadores(fonte: Fonte) -> dict | None:
    """CDI, Selic, IPCA e dólar (PTAX) do Banco Central."""
    cdi, selic, ipca, dolar = serie_bcb(fonte, 12), serie_bcb(fonte, 432), serie_bcb(fonte, 433), serie_bcb(fonte, 1)
    if not (cdi or selic or ipca or dolar):
        aviso("indicadores do Banco Central indisponíveis")
        return None
    out: dict = {"fonte": "Banco Central do Brasil (SGS)"}
    if cdi:
        idx, serie = 1.0, []
        for d, v in cdi:
            idx *= 1 + v / 100
            serie.append([d, round(idx, 6)])
        ult = serie[-1][1]
        base12 = next((p[1] for p in reversed(serie) if p[0] <= (dt.date.fromisoformat(serie[-1][0]) - dt.timedelta(days=365)).isoformat()), None)
        out["cdi"] = {"ao_ano": r((1 + cdi[-1][1] / 100) ** 252 - 1), "12m": r(ult / base12 - 1) if base12 else None,
                      "data": cdi[-1][0], "serie": serie}
    if selic:
        mensal = {}
        for d, v in selic:
            mensal[d[:7]] = [d, v]
        out["selic"] = {"meta": selic[-1][1] / 100, "data": selic[-1][0], "serie": [[d, v / 100] for d, v in mensal.values()]}
    if ipca:
        serie12 = []
        for i in range(11, len(ipca)):
            acum = 1.0
            for _, v in ipca[i - 11:i + 1]:
                acum *= 1 + v / 100
            serie12.append([ipca[i][0], round(acum - 1, 6)])
        out["ipca"] = {"12m": serie12[-1][1] if serie12 else None, "mes": ipca[-1][1] / 100, "data": ipca[-1][0], "serie": serie12}
    if dolar:
        semanal = {}
        for d, v in dolar:
            semanal[dt.date.fromisoformat(d).strftime("%G-%V")] = [d, v]
        serie = list(semanal.values())
        base12 = next((v for d, v in reversed(dolar) if d <= (dt.date.fromisoformat(dolar[-1][0]) - dt.timedelta(days=365)).isoformat()), None)
        out["dolar"] = {"atual": dolar[-1][1], "data": dolar[-1][0], "var_12m": r(dolar[-1][1] / base12 - 1) if base12 else None,
                        "serie": serie}
    return out


def montar_agenda(detalhes: dict, hoje: dt.date) -> dict:
    """Agenda de proventos do mercado: o que já foi anunciado e o radar (meses em que cada ação
    costuma ter data com, pelo histórico de até 3 anos que a base tiver). Projeção, não promessa."""
    inicio = (hoje - dt.timedelta(days=45)).isoformat()
    anunciados, radar = [], []
    for tk, d in detalhes.items():
        provs = [p for p in d.get("proventos") or [] if p.get("data_com") and p.get("valor")]
        preco = d.get("preco")
        for p in provs:
            ref = max(p["data_com"], p.get("pagamento") or "")
            if ref >= inicio:
                anunciados.append({"t": tk, "tipo": p.get("tipo"), "data_com": p["data_com"], "pagamento": p.get("pagamento"),
                                   "valor": r(p["valor"], 6), "dy": r(div(p["valor"], preco))})
        if d.get("tipo") != "acao":
            continue
        # valor com data com em cada mês (AAAA-MM) do histórico
        por_mes = defaultdict(float)
        for p in provs:
            por_mes[p["data_com"][:7]] += p["valor"]
        ja = {m for m in por_mes if m >= hoje.replace(day=1).isoformat()[:7]}
        if not por_mes:
            continue
        cobertura = min(por_mes)  # até onde vai o histórico desta ação
        for k in range(0, 12):
            mes_n = (hoje.month - 1 + k) % 12 + 1
            ano_n = hoje.year + (hoje.month - 1 + k) // 12
            chave = f"{ano_n}-{mes_n:02d}"
            # mesmo mês nos até 3 anos anteriores que a base cobre (a B3 costuma trazer só o último ano)
            olhados = [f"{ano_n - j}-{mes_n:02d}" for j in (1, 2, 3)
                       if f"{ano_n - j}-{mes_n:02d}" >= cobertura and f"{ano_n - j}-{mes_n:02d}" < hoje.isoformat()[:7]]
            achou = [por_mes[m] for m in olhados if por_mes.get(m)]
            if olhados and len(achou) >= min(2, len(olhados)) and chave not in ja:
                radar.append({"t": tk, "mes": chave, "anos": len(achou), "de": len(olhados),
                              "valor": r(sum(achou) / len(achou), 6), "dy": r(div(sum(achou) / len(achou), preco))})
    anunciados.sort(key=lambda x: (x["data_com"], x["t"]))
    radar.sort(key=lambda x: (x["mes"], -(x["anos"]), x["t"]))
    return {"gerado_em": hoje.isoformat(), "anunciados": anunciados, "radar": radar}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default="data")
    ap.add_argument("--anos", type=int, default=5)
    ap.add_argument("--fonte-local", default=None, help="pasta com os arquivos já baixados (testes)")
    ap.add_argument("--cache", default=".cache-dados")
    a = ap.parse_args()

    fonte = Fonte(a.fonte_local, None if a.fonte_local else a.cache)
    saida = Path(a.saida)
    (saida / "ativo").mkdir(parents=True, exist_ok=True)
    (saida / "ohlc").mkdir(parents=True, exist_ok=True)

    print("1/5 cotações", flush=True)
    cot = carregar_cotacoes(fonte, a.anos)
    ultimo_pregao = cot["data"].max()
    ativos_vivos = cot.loc[cot["data"] >= ultimo_pregao - pd.Timedelta(days=10), "ticker"].unique()
    cot = cot[cot["ticker"].isin(ativos_vivos)]

    print("2/5 cadastro e demonstrações", flush=True)
    vm, geral = carregar_fca(fonte)
    cadastro: dict[str, dict] = {}
    if not vm.empty:
        col_tk = next(c for c in vm.columns if "Codigo_Negociacao" in c)
        for _, row in vm.dropna(subset=[col_tk]).iterrows():
            cadastro[row[col_tk].strip()] = {"cnpj": row.get("CNPJ_Companhia"),
                                             "cvm": str(row.get("Codigo_CVM", "")).lstrip("0")}
        if not geral.empty:
            g = geral.drop_duplicates("CNPJ_Companhia", keep="last").set_index("CNPJ_Companhia")
            for c in cadastro.values():
                if c["cnpj"] in g.index:
                    linha = g.loc[c["cnpj"]]
                    c["nome"] = linha.get("Nome_Empresarial")
                    c["setor"] = linha.get("Setor_Atividade")
                    c["cvm"] = c["cvm"] or str(linha.get("Codigo_CVM", "")).lstrip("0")
    dem = {k: preparar(v) if k != "CAPITAL" and len(v) else v
           for k, v in carregar_demonstracoes(fonte, a.anos + 1).items()}
    capital = dem.get("CAPITAL", pd.DataFrame())

    print("3/5 FIIs", flush=True)
    fgeral, fcomp = carregar_fii(fonte)
    fii_info: dict[str, dict] = {}
    if not fcomp.empty:
        fcomp = fcomp.sort_values("Data_Referencia")
        ultimo = fcomp.drop_duplicates("CNPJ_Fundo_Classe" if "CNPJ_Fundo_Classe" in fcomp else "CNPJ_Fundo", keep="last")
        col_cnpj = "CNPJ_Fundo_Classe" if "CNPJ_Fundo_Classe" in fcomp else "CNPJ_Fundo"
        g = fgeral.drop_duplicates(col_cnpj, keep="last").set_index(col_cnpj) if not fgeral.empty else None
        for _, row in ultimo.iterrows():
            cnpj = row[col_cnpj]
            isin = g.loc[cnpj].get("Codigo_ISIN") if g is not None and cnpj in g.index else None
            if not isin:
                continue
            fii_info[isin] = {
                "cnpj": cnpj, "vp_cota": num(row.get("Valor_Patrimonial_Cotas")),
                "cotistas": num(row.get("Total_Numero_Cotistas")), "cotas": num(row.get("Cotas_Emitidas")),
                "patrimonio": num(row.get("Patrimonio_Liquido")),
                "taxa_adm": num(row.get("Percentual_Despesas_Taxa_Administracao")),
                "segmento": g.loc[cnpj].get("Segmento_Atuacao") if g is not None else None,
                "mandato": g.loc[cnpj].get("Mandato") if g is not None else None,
                "gestao": g.loc[cnpj].get("Tipo_Gestao") if g is not None else None,
                "publico": g.loc[cnpj].get("Publico_Alvo") if g is not None else None,
                "nome": g.loc[cnpj].get("Nome_Fundo_Classe") or g.loc[cnpj].get("Nome_Fundo") if g is not None else None,
                "dy_mes": [(d, num(v)) for d, v in fcomp[fcomp[col_cnpj] == cnpj][["Data_Referencia", "Percentual_Dividend_Yield_Mes"]].values],
            }

    print("4/5 proventos", flush=True)
    FIAGROS.update(cot.loc[cot["bdi"] == "13", "ticker"].unique())
    ultimos = cot.sort_values("data").groupby("ticker").tail(1).set_index("ticker")
    emissores = {}
    for tk, row in ultimos.iterrows():
        tp = tipo_ativo(row["bdi"], row["especi"], tk)
        if tp in ("etf", "bdr"):  # sem consulta de proventos na B3 por enquanto
            continue
        emissores[tk[:4]] = "fii" if tp in ("fii", "fiagro") else emissores.get(tk[:4], "acao")
    dinheiro, eventos = carregar_proventos(fonte, emissores)

    print("5/5 montando arquivos", flush=True)
    # separa as demonstrações por empresa uma vez só (evita varrer a base inteira a cada ação)
    por_cvm = {k: dict(tuple(v.groupby("CD_CVM"))) for k, v in dem.items()
               if k != "CAPITAL" and isinstance(v, pd.DataFrame) and len(v)}
    col_cnpj_cap = next((c for c in capital.columns if "CNPJ" in c.upper()), None) if not capital.empty else None
    cap_por_cnpj = dict(tuple(capital.groupby(col_cnpj_cap))) if col_cnpj_cap else {}
    vazio_cap = capital.iloc[0:0] if not capital.empty else capital
    lista_ativos, detalhes = [], {}
    for tk, grupo in cot.groupby("ticker"):
        u = grupo.iloc[-1]
        isin, tipo = u["isin"], tipo_ativo(u["bdi"], u["especi"], tk)
        liq = grupo.tail(21)["volume"].mean()
        if tipo == "bdr" and not liq >= 50_000:  # só BDRs com negócio de verdade (média de R$ 50 mil por dia)
            continue
        pontos = serie_preco(grupo, eventos.get(isin, []))
        if len(pontos) >= 5:
            (saida / "ohlc" / f"{tk}.json").write_text(json.dumps(
                limpar(serie_ohlc(grupo, eventos.get(isin, []))), separators=(",", ":")), encoding="utf-8")
        if len(pontos) < 5:
            continue
        preco = pontos[-1][1]
        prov = dinheiro.get(isin, [])
        if tipo == "fiagro":
            det = montar_fii(tk, preco, pontos, {}, prov)
            nome, setor = u["nome"], "Fiagros"
        elif tipo in ("etf", "bdr"):
            lista_p = sorted(prov, key=lambda p: p.get("data_com") or "", reverse=True)
            det = {"ind": {"dy": r(div(proventos_12m(lista_p, pontos[-1][0]), preco)) if lista_p else None},
                   "proventos": lista_p[:120]}
            nome, setor = u["nome"], "ETFs" if tipo == "etf" else "BDRs"
        elif tipo == "fii":
            info = fii_info.get(isin, {})
            if not prov and info.get("dy_mes") and info.get("vp_cota"):
                # o informe traz o DY do mês sobre o valor patrimonial; aceita fração ou percentual
                prov = [{"tipo": "Rendimento", "data_com": str(d)[:10], "pagamento": None,
                         "valor": round((v if v < 0.05 else v / 100) * info["vp_cota"], 4)}
                        for d, v in info["dy_mes"] if v]
                info["estimado"] = True
            det = montar_fii(tk, preco, pontos, info, prov)
            nome = info.get("nome") or u["nome"]
            setor = info.get("segmento") or "Fundos imobiliários"
        else:
            cad = cadastro.get(tk)
            if not cad:
                det = {"ind": {}, "proventos": sorted(prov, key=lambda p: p.get("data_com") or "", reverse=True)}
                nome, setor = u["nome"], "Sem cadastro na CVM"
            else:
                cad = {**cad, "liquidez": liq}
                dem_emp = {k: g.get(cad["cvm"]) for k, g in por_cvm.items()}
                dem_emp = {k: v for k, v in dem_emp.items() if v is not None}
                det = montar_acao(tk, preco, pontos, cad, dem_emp, cap_por_cnpj.get(cad["cnpj"], vazio_cap), prov)
                det["cnpj"] = cad.get("cnpj")  # vai no texto de bens e direitos do Imposto de Renda
                nome, setor = cad.get("nome") or u["nome"], cad.get("setor") or "Outros"
        det.update({
            "ticker": tk, "nome": nome, "tipo": tipo, "setor": setor, "isin": isin,
            "preco": preco, "data": pontos[-1][0],
            "var_dia": r(div(pontos[-1][1] - pontos[-2][1], pontos[-2][1])),
            "var_12m": r(variacao(pontos, 365)), "liquidez": round(liq), "precos": pontos,
        })
        detalhes[tk] = det

    # médias do setor para comparar cada indicador
    por_setor = defaultdict(list)
    for d in detalhes.values():
        por_setor[(d["tipo"], d["setor"])].append(d)
    for d in detalhes.values():
        pares = por_setor[(d["tipo"], d["setor"])]
        d["setor_mediana"] = {k: r(mediana([p.get("ind", {}).get(k) for p in pares]))
                              for k in d.get("ind", {})}
        d["pares"] = [p["ticker"] for p in sorted(pares, key=lambda p: -(p["liquidez"] or 0))
                      if p["ticker"] != d["ticker"]][:8]
        ind = d.get("ind", {})
        lista_ativos.append({
            "t": d["ticker"], "n": d["nome"], "tipo": d["tipo"], "setor": d["setor"],
            "preco": d["preco"], "var_dia": d["var_dia"], "var_12m": d["var_12m"], "liq": d["liquidez"],
            "var_30d": r(variacao(d["precos"], 30)),
            "lucro": d.get("base", {}).get("lucro_12m"), "receita": d.get("base", {}).get("receita_12m"),
            **{k: ind.get(k) for k in ("dy", "pl", "pvp", "roe", "m_liq", "roic", "ev_ebit", "dl_ebitda",
                                       "cagr_rec", "cagr_luc", "liq_corr")},
            "vm": d.get("base", {}).get("valor_mercado"),
            "graham": d.get("graham"), "bazin": d.get("bazin"), "pontos": d.get("pontos"),
            "magic": ind.get("magic_number"), "vp": ind.get("vp_cota"),
        })
        (saida / "ativo" / f"{d['ticker']}.json").write_text(
            json.dumps(limpar(d), ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")

    lista_ativos.sort(key=lambda x: -(x["liq"] or 0))
    indicadores = montar_indicadores(fonte)
    if indicadores:
        (saida / "indicadores.json").write_text(json.dumps(limpar(indicadores), ensure_ascii=False, separators=(",", ":")),
                                                encoding="utf-8")
    # Consolidador de carteira: renda fixa, Tesouro, fundos e cripto (módulo à parte; falha sem derrubar o resto)
    consolidado = {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import consolidador
        consolidado = consolidador.gerar(fonte, saida, HOJE, aviso)
    except Exception as e:  # noqa: BLE001
        aviso(f"consolidador: {e}")
    (saida / "codigos_bdi.json").write_text(json.dumps(dict(sorted(CODIGOS_BDI.items(), key=lambda kv: -kv[1]["linhas"])),
                                                       ensure_ascii=False, indent=1), encoding="utf-8")
    (saida / "agenda.json").write_text(json.dumps(limpar(montar_agenda(detalhes, ultimo_pregao.date())), ensure_ascii=False,
                                                  separators=(",", ":"), default=str), encoding="utf-8")
    (saida / "ativos.json").write_text(json.dumps(limpar(lista_ativos), ensure_ascii=False, separators=(",", ":"),
                                                  default=str), encoding="utf-8")
    (saida / "meta.json").write_text(json.dumps({
        "atualizado_em": dt.datetime.now().isoformat(timespec="minutes"),
        "ultimo_pregao": ultimo_pregao.date().isoformat(),
        "ativos": len(lista_ativos),
        "indicadores": [{"chave": k, "nome": n, "grupo": g, "formato": f} for k, n, g, f in INDICADORES],
        "fontes": ["B3 COTAHIST", "CVM DFP/ITR/FCA", "CVM Informe Mensal FII", "B3 proventos",
                   "Banco Central SGS", "Tesouro Transparente", "CVM Informe Diário de Fundos", "CoinGecko"],
        "consolidador": consolidado,
        "demo": bool(a.fonte_local and "fixtures" in str(a.fonte_local)),
        "avisos": AVISOS,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ok: {len(lista_ativos)} ativos em {saida}/")


if __name__ == "__main__":
    main()
