#!/usr/bin/env python3
"""
Conferência diária dos dados do Milhão.

Roda depois do robô de dados e antes de publicar. Confere dado a dado o que vai para o site:

  1. Preço e série de cada ativo: preço positivo, datas em ordem, variação do dia batendo com a série,
     saltos que parecem desdobramento sem ajuste, ativo parado há muitos dias.
  2. Segunda fonte: o fechamento de hoje dos ativos mais negociados (e de todo ativo que andou demais)
     é comparado com o Yahoo Finance. Se não bater, o ativo fica com o último dado conferido.
  3. Comparação com o que estava no ar: preço que mudou demais de um dia para o outro precisa ser
     confirmado pela segunda fonte.
  4. Proventos: valor positivo, datas válidas, pagamento depois da data com, sem repetidos,
     nenhum provento maior que o próprio preço.
  5. Indicadores: P/L, P/VP, DY, valor de mercado etc. são recalculados a partir da base e corrigidos
     se não baterem; número inválido some do site (vira "—") em vez de aparecer errado.
  6. Juros e moedas (Banco Central), Tesouro Direto e cripto: faixas plausíveis e comparação com ontem;
     cripto também é comparada com o Mercado Bitcoin.

Regra de ouro: na dúvida, o site mostra menos, nunca errado.
  - corrigido: o dado foi recalculado e está certo agora
  - ocultado: o dado não passou e foi tirado do site
  - segurado: o ativo ficou com o último dado conferido (o de ontem)
  - em conferência: o ativo segue no ar com um aviso para o cliente
  - bloqueio: erro em massa (ex.: metade dos ativos sumiu). A publicação para e o site
    continua com os dados de ontem. O GitHub avisa por e-mail que a rodada falhou.

Uso:
    python etl/conferencia.py --dados site/data --anterior https://milhao.pages.dev/data
    python etl/conferencia.py --dados /tmp/dados --sem-rede          # testes, sem internet
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:  # modo sem rede não precisa
    requests = None

AGORA = dt.datetime.now()
UA = {"User-Agent": "Mozilla/5.0 (Milhao conferencia; +https://milhao.pages.dev)"}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{tk}.SA?range=10d&interval=1d"
MERCADO_BITCOIN = "https://api.mercadobitcoin.net/api/v4/tickers?symbols={pares}"

TOLERANCIA_EXTERNA = 0.02      # 2% de diferença para a segunda fonte
MUDANCA_GRANDE = 0.25          # preço que andou mais de 25% desde ontem precisa ser confirmado
SALTO_SERIE = (0.45, 2.2)      # queda >55% ou alta >120% de um pregão para o outro
AMOSTRA_EXTERNA = 150          # quantos dos mais negociados conferir com a segunda fonte


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------
def ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def div(a, b):
    return a / b if ok(a) and ok(b) and b != 0 else None


def r(v, casas=4):
    return round(v, casas) if ok(v) else None


def difere(a, b, rel=0.01, abs_=1e-6) -> bool:
    """True se a e b não batem (tolerância relativa de 1%)."""
    if a is None and b is None:
        return False
    if not ok(a) or not ok(b):
        return True
    return abs(a - b) > max(abs_, rel * max(abs(a), abs(b)))


def _br(txt: str) -> str:
    return txt.replace(",", "§").replace(".", ",").replace("§", ".")


def brl(v, casas=2) -> str:
    return "R$ " + _br(f"{v:,.{casas}f}") if ok(v) else "–"


def pc(v, casas=0, sinal=False) -> str:
    return _br(f"{v * 100:{'+' if sinal else ''}.{casas}f}") + "%" if ok(v) else "–"


def dbr(s) -> str:
    s = str(s)[:10]
    return f"{s[8:10]}/{s[5:7]}/{s[:4]}" if data_ok(s) else s


def data_ok(s) -> bool:
    try:
        dt.date.fromisoformat(str(s)[:10])
        return len(str(s)) >= 10
    except ValueError:
        return False


def ler(p: Path, padrao=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def gravar(p: Path, obj, compacto=True):
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":") if compacto else None,
                            indent=None if compacto else 1, default=str), encoding="utf-8")


class Rede:
    """Busca na internet com tempo limite; sem rede (testes), tudo volta None."""

    def __init__(self, ligada: bool):
        self.ligada = ligada and requests is not None
        self.s = requests.Session() if self.ligada else None
        if self.s:
            self.s.headers.update(UA)

    def json(self, url: str, tentativas=2, timeout=15):
        if not self.ligada:
            return None
        for i in range(tentativas):
            try:
                resp = self.s.get(url, timeout=timeout)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    return None
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1 + i)
        return None


class Registro:
    """Anota cada problema encontrado, com o antes e o depois."""

    NIVEIS = ("corrigido", "ocultado", "segurado", "em_conferencia", "aviso", "bloqueio")

    def __init__(self):
        self.itens: list[dict] = []
        self.campos = 0

    def add(self, alvo, campo, nivel, msg, antes=None, depois=None):
        assert nivel in self.NIVEIS
        self.itens.append({"alvo": alvo, "campo": campo, "nivel": nivel, "msg": msg,
                           "antes": antes, "depois": depois})

    def de(self, alvo):
        return [i for i in self.itens if i["alvo"] == alvo]

    def conta(self, nivel):
        return sum(1 for i in self.itens if i["nivel"] == nivel)


# --------------------------------------------------------------------------
# 1. preço e série
# --------------------------------------------------------------------------
def conferir_serie(tk, d, R: Registro, ultimo_pregao: str) -> bool:
    """Arruma a série de preços. Devolve False se o preço do ativo não é confiável."""
    pontos = d.get("precos") or []
    R.campos += len(pontos)
    limpos, vistos = [], set()
    for p in pontos:
        if (isinstance(p, list) and len(p) >= 2 and data_ok(p[0]) and ok(p[1]) and p[1] > 0
                and p[0] not in vistos):
            limpos.append(p)
            vistos.add(p[0])
    limpos.sort(key=lambda p: p[0])
    if len(limpos) != len(pontos) or limpos != pontos:
        R.add(tk, "precos", "corrigido", f"série de preços arrumada ({len(pontos) - len(limpos)} ponto(s) inválido(s) ou repetido(s) removido(s))",
              len(pontos), len(limpos))
        d["precos"] = pontos = limpos
    if len(pontos) < 2:
        R.add(tk, "preco", "em_conferencia", "série de preços curta demais para conferir")
        return False

    preco, data = d.get("preco"), d.get("data")
    if not ok(preco) or preco <= 0:
        R.add(tk, "preco", "segurado", "preço inválido", preco, None)
        return False
    if difere(preco, pontos[-1][1], rel=1e-6) or data != pontos[-1][0]:
        R.add(tk, "preco", "corrigido", "preço do dia não batia com o último ponto da série",
              [data, preco], pontos[-1][:2])
        d["preco"], d["data"] = pontos[-1][1], pontos[-1][0]

    var = div(pontos[-1][1] - pontos[-2][1], pontos[-2][1])
    if difere(d.get("var_dia"), r(var), rel=0.001, abs_=0.0002):
        R.add(tk, "var_dia", "corrigido", "variação do dia recalculada pela série", d.get("var_dia"), r(var))
        d["var_dia"] = r(var)

    # salto que parece desdobramento/grupamento sem ajuste: as variações longas ficam erradas
    um_ano = (dt.date.fromisoformat(pontos[-1][0]) - dt.timedelta(days=366)).isoformat()
    for a, b in zip(pontos, pontos[1:]):
        k = b[1] / a[1]
        if not (SALTO_SERIE[0] <= k <= SALTO_SERIE[1]):
            pct = pc(k - 1, 0, True)
            if b[0] >= um_ano:
                for campo in ("var_12m", "var_30d"):
                    if d.get(campo) is not None and (campo == "var_12m" or b[0] >= (dt.date.fromisoformat(pontos[-1][0]) - dt.timedelta(days=31)).isoformat()):
                        R.add(tk, campo, "ocultado", f"salto de {pct} em {dbr(b[0])} (desdobramento ou grupamento sem ajuste?)",
                              d.get(campo), None)
                        d[campo] = None
            else:
                R.add(tk, "precos", "aviso", f"salto de {pct} em {dbr(b[0])} na série antiga")
    if data and data < (dt.date.fromisoformat(ultimo_pregao) - dt.timedelta(days=7)).isoformat():
        R.add(tk, "data", "aviso", f"sem negócios desde {dbr(data)}")
    return True


# --------------------------------------------------------------------------
# 2. proventos
# --------------------------------------------------------------------------
def proventos_12m(lista, ate: str):
    ini = (dt.date.fromisoformat(ate) - dt.timedelta(days=365)).isoformat()
    return sum(p["valor"] for p in lista if p.get("data_com") and ini < p["data_com"] <= ate)


def conferir_proventos(tk, d, R: Registro):
    lista = d.get("proventos")
    if not isinstance(lista, list):
        return
    R.campos += len(lista) * 4
    preco, limite = d.get("preco"), (AGORA.date() + dt.timedelta(days=400)).isoformat()
    bons, vistos, removidos = [], set(), []
    for p in lista:
        v, dc, pg = p.get("valor"), p.get("data_com"), p.get("pagamento")
        chave = (p.get("tipo"), dc, round(v, 8) if ok(v) else v, pg)
        if not ok(v) or v <= 0:
            removidos.append((p, "valor inválido"))
        elif not data_ok(dc) or dc > limite:
            removidos.append((p, "data com inválida"))
        elif ok(preco) and v > preco:
            removidos.append((p, f"valor de {brl(v, 4)} maior que o preço da cota/ação ({brl(preco)})"))
        elif chave in vistos:
            removidos.append((p, "repetido"))
        else:
            if pg and data_ok(pg) and pg < dc:
                R.add(tk, "proventos", "aviso", f"pagamento ({dbr(pg)}) antes da data com ({dbr(dc)})")
            if ok(preco) and v > 0.4 * preco:
                R.add(tk, "proventos", "aviso", f"provento alto: {brl(v, 4)} em {dbr(dc)} ({pc(v / preco)} do preço)")
            vistos.add(chave)
            bons.append(p)
    for p, motivo in removidos:
        R.add(tk, "proventos", "corrigido", f"provento removido: {motivo}", p, None)
    if removidos:
        d["proventos"] = bons


# --------------------------------------------------------------------------
# 3. indicadores
# --------------------------------------------------------------------------
DEPENDEM_DE_ACOES = ("pl", "pvp", "psr", "p_ebit", "p_ativo", "p_capgiro", "p_acl", "ev_ebit", "ev_ebitda",
                     "lpa", "vpa", "payout")


def conferir_indicadores(tk, d, R: Registro):
    ind = d.get("ind")
    if not isinstance(ind, dict):
        return
    R.campos += len(ind)
    preco, data, tipo = d.get("preco"), d.get("data"), d.get("tipo")

    # número inválido (NaN, infinito, absurdo) some do site
    for k, v in list(ind.items()):
        if v is not None and (not ok(v) or abs(v) > 1e7) and k != "magic_number":
            R.add(tk, k, "ocultado", "valor inválido", v, None)
            ind[k] = None

    # DY pelos proventos dos últimos 12 meses (vale para todos os tipos)
    if ok(preco) and data and isinstance(d.get("proventos"), list):
        dy = r(div(proventos_12m(d["proventos"], data), preco))
        if difere(ind.get("dy"), dy, rel=0.01, abs_=0.0005) and not (ind.get("dy") is None and not dy):
            R.add(tk, "dy", "corrigido", "dividend yield recalculado pelos proventos de 12 meses", ind.get("dy"), dy)
            ind["dy"] = dy
        if ok(ind.get("dy")) and ind["dy"] > 0.6:
            R.add(tk, "dy", "ocultado", f"dividend yield de {pc(ind['dy'])} fora do plausível", ind["dy"], None)
            ind["dy"] = None

    if tipo in ("fii", "fiagro"):
        vp = ind.get("vp_cota")
        if vp is not None and (not ok(vp) or vp <= 0):
            R.add(tk, "vp_cota", "ocultado", "valor patrimonial da cota inválido", vp, None)
            ind["vp_cota"] = vp = None
        pvp = r(div(preco, vp))
        if difere(ind.get("pvp"), pvp, rel=0.005):
            R.add(tk, "pvp", "corrigido", "P/VP recalculado (preço ÷ valor patrimonial da cota)", ind.get("pvp"), pvp)
            ind["pvp"] = pvp
        if ok(pvp) and not (0.05 <= pvp <= 10):
            R.add(tk, "pvp", "ocultado", f"P/VP de {pvp:.2f} fora do plausível para fundo", pvp, None)
            ind["pvp"] = None
        lista = d.get("proventos") or []
        ult = lista[0]["valor"] if lista else None
        if difere(ind.get("ultimo_rendimento"), ult, rel=1e-6) and not (ind.get("ultimo_rendimento") is None and ult is None):
            R.add(tk, "ultimo_rendimento", "corrigido", "último rendimento conferido com a lista", ind.get("ultimo_rendimento"), ult)
            ind["ultimo_rendimento"] = ult
        magic = math.ceil(preco / ult) if ok(preco) and ok(ult) and ult > 0 else None
        if ind.get("magic_number") != magic:
            ind["magic_number"] = magic
        if isinstance(d.get("yields"), dict) and data:
            for m in (1, 3, 6, 12):
                ini = (dt.date.fromisoformat(data) - dt.timedelta(days=30.5 * m)).isoformat()
                y = r(div(sum(p["valor"] for p in lista if p.get("data_com") and p["data_com"] > ini), preco))
                if difere(d["yields"].get(f"{m}m"), y, rel=0.01, abs_=0.0005) and not (d["yields"].get(f"{m}m") is None and not y):
                    R.add(tk, f"yield_{m}m", "corrigido", f"rendimento de {m} mês(es) recalculado", d["yields"].get(f"{m}m"), y)
                    d["yields"][f"{m}m"] = y
        return

    base = d.get("base")
    if tipo != "acao" or not isinstance(base, dict):
        return
    R.campos += len(base)
    n, lucro, pl_ = base.get("acoes"), base.get("lucro_12m"), base.get("patrimonio")
    receita, ebit = base.get("receita_12m"), base.get("ebit_12m")

    # quantidade de ações: se o valor de mercado fica absurdo perto da receita/patrimônio, a quantidade está errada
    vm = preco * n if ok(preco) and ok(n) and n > 0 else None
    suspeita = None
    if vm is not None:
        refs = [x for x in (receita, pl_ if ok(pl_) and pl_ > 0 else None) if ok(x) and x > 0]
        if refs and (max(vm / x for x in refs) < 0.0005 or min(vm / x for x in refs) > 5000):
            suspeita = "quantidade de ações parece errada (valor de mercado incompatível com receita e patrimônio)"
    if suspeita:
        R.add(tk, "acoes", "em_conferencia", suspeita, n, None)
        for k in DEPENDEM_DE_ACOES:
            if ind.get(k) is not None:
                ind[k] = None
        R.add(tk, "valuation", "ocultado", "P/L, P/VP, LPA, VPA e afins tirados até a quantidade de ações ser conferida")
        base["valor_mercado"] = base["valor_firma"] = base["acoes"] = None
        d["graham"] = None
        return

    if difere(base.get("valor_mercado"), vm, rel=0.002):
        R.add(tk, "valor_mercado", "corrigido", "valor de mercado recalculado (preço × ações)", base.get("valor_mercado"), vm)
        base["valor_mercado"] = vm
        dl = base.get("divida_liquida")
        base["valor_firma"] = vm + dl if vm is not None and ok(dl) else None

    lpa, vpa = div(lucro, n), div(pl_, n)
    esperado = {
        "lpa": r(lpa), "vpa": r(vpa),
        "pl": r(div(preco, lpa)) if ok(lpa) and lpa > 0 else None,
        "pvp": r(div(preco, vpa)),
        "psr": r(div(vm, receita)), "p_ebit": r(div(vm, ebit)),
        "roe": r(div(lucro, pl_)), "m_liq": r(div(lucro, receita)),
    }
    for k, v in esperado.items():
        if k in ind and difere(ind.get(k), v, rel=0.01, abs_=0.0005):
            R.add(tk, k, "corrigido", "recalculado a partir do balanço e do preço", ind.get(k), v)
            ind[k] = v
    # patrimônio negativo: ROE e P/VP perdem o sentido
    if ok(pl_) and pl_ < 0:
        for k in ("roe", "pvp"):
            if ind.get(k) is not None:
                R.add(tk, k, "ocultado", "patrimônio líquido negativo: indicador sem sentido", ind[k], None)
                ind[k] = None

    # valuation
    graham = r(math.sqrt(22.5 * lpa * vpa), 2) if ok(lpa) and ok(vpa) and lpa > 0 and vpa > 0 else None
    if difere(d.get("graham"), graham, rel=0.005) and not (d.get("graham") is None and graham is None):
        R.add(tk, "graham", "corrigido", "preço justo de Graham recalculado", d.get("graham"), graham)
        d["graham"] = graham
    for k in ("graham", "bazin"):
        v = d.get(k)
        if v is not None and (not ok(v) or v <= 0 or (ok(preco) and not (0.02 <= v / preco <= 50))):
            R.add(tk, k, "ocultado", "preço justo fora do plausível", v, None)
            d[k] = None

    ref = base.get("referencia")
    if ref and data_ok(ref) and ref < (AGORA.date() - dt.timedelta(days=500)).isoformat():
        R.add(tk, "base", "aviso", f"último balanço é de {dbr(ref)}")


# --------------------------------------------------------------------------
# 4. segunda fonte (Yahoo Finance)
# --------------------------------------------------------------------------
def fechamentos_yahoo(rede: Rede, tk: str) -> dict[str, float]:
    j = rede.json(YAHOO.format(tk=tk), tentativas=2, timeout=12)
    try:
        res = j["chart"]["result"][0]
        off = res.get("meta", {}).get("gmtoffset", -10800)
        ts, cl = res["timestamp"], res["indicators"]["quote"][0]["close"]
        return {dt.datetime.utcfromtimestamp(t + off).date().isoformat(): c for t, c in zip(ts, cl) if ok(c)}
    except (TypeError, KeyError, IndexError):
        return {}


# --------------------------------------------------------------------------
# 5. juros, Tesouro e cripto
# --------------------------------------------------------------------------
def conferir_indicadores_bcb(dados: Path, ontem: dict | None, R: Registro):
    p = dados / "indicadores.json"
    j = ler(p)
    if not j:
        return
    faixas = [("selic", "meta", 0.0, 0.5), ("cdi", "ao_ano", 0.0, 0.5), ("ipca", "12m", -0.1, 0.5),
              ("dolar", "atual", 1.0, 20.0)]
    ruim = []
    for grupo, campo, lo, hi in faixas:
        v = (j.get(grupo) or {}).get(campo)
        R.campos += 1
        if v is not None and (not ok(v) or not lo <= v <= hi):
            ruim.append(f"{grupo} {campo} = {v}")
    if ontem:
        s0, s1 = (ontem.get("selic") or {}).get("meta"), (j.get("selic") or {}).get("meta")
        if ok(s0) and ok(s1) and abs(s1 - s0) > 0.02:
            ruim.append(f"Selic mudou de {pc(s0, 2)} para {pc(s1, 2)} de um dia para o outro")
        d0, d1 = (ontem.get("dolar") or {}).get("atual"), (j.get("dolar") or {}).get("atual")
        if ok(d0) and ok(d1) and abs(d1 / d0 - 1) > 0.15:
            ruim.append(f"dólar mudou {pc(d1 / d0 - 1, 0, True)} de um dia para o outro")
    if ruim:
        if ontem:
            gravar(p, ontem)
            R.add("indicadores", "indicadores", "segurado", "; ".join(ruim) + " — ficaram os indicadores de ontem")
        else:
            R.add("indicadores", "indicadores", "em_conferencia", "; ".join(ruim))


def conferir_tesouro(dados: Path, R: Registro):
    p = dados / "tesouro.json"
    j = ler(p)
    if not j or not isinstance(j.get("titulos"), list):
        return
    mudou = False
    for t in j["titulos"]:
        R.campos += 4
        for k in ("pu_compra", "pu_venda"):
            v = t.get(k)
            if v is not None and (not ok(v) or v <= 0 or v > 1e6):
                R.add(f"tesouro:{t.get('id')}", k, "ocultado", "preço unitário inválido", v, None)
                t[k], mudou = None, True
        for k in ("taxa_compra", "taxa_venda"):
            v = t.get(k)
            if v is not None and (not ok(v) or not -5 <= v <= 40):
                R.add(f"tesouro:{t.get('id')}", k, "ocultado", "taxa fora do plausível", v, None)
                t[k], mudou = None, True
    if mudou:
        gravar(p, j)


def conferir_cripto(dados: Path, ontem: dict | None, rede: Rede, R: Registro):
    p = dados / "cripto.json"
    j = ler(p)
    if not j or not isinstance(j.get("moedas"), list):
        return
    ontem_preco = {m[0]: m[3] for m in (ontem or {}).get("moedas", []) if isinstance(m, list) and len(m) > 3}
    boas = []
    for m in j["moedas"]:
        R.campos += 2
        if not (isinstance(m, list) and len(m) >= 4 and ok(m[3]) and m[3] > 0):
            R.add("cripto", str(m[:2] if isinstance(m, list) else m), "ocultado", "moeda com preço inválido", m, None)
            continue
        p0 = ontem_preco.get(m[0])
        if ok(p0) and p0 > 0 and abs(m[3] / p0 - 1) > 0.6 and m[1] not in ("USDT", "USDC"):
            R.add("cripto", m[1], "aviso", f"{m[1]} mudou {pc(m[3] / p0 - 1, 0, True)} desde ontem")
        if m[1] in ("USDT", "USDC") and ok(p0) and abs(m[3] / p0 - 1) > 0.15:
            R.add("cripto", m[1], "ocultado", f"stablecoin {m[1]} com preço fora do lugar", m[3], None)
            continue
        boas.append(m)
    # segunda fonte: Bitcoin e Ethereum no Mercado Bitcoin
    if j.get("fonte") != "Mercado Bitcoin":
        mb = rede.json(MERCADO_BITCOIN.format(pares="BTC-BRL,ETH-BRL")) or []
        ref = {t.get("pair", "").split("-")[0]: float(t["last"]) for t in mb if isinstance(t, dict) and t.get("last")}
        for m in boas:
            if m[1] in ref and ref[m[1]] > 0:
                dif = abs(m[3] / ref[m[1]] - 1)
                if dif > 0.05:
                    R.add("cripto", m[1], "segurado" if ontem else "em_conferencia",
                          f"{m[1]}: {brl(m[3], 0)} aqui contra {brl(ref[m[1]], 0)} no Mercado Bitcoin ({pc(dif, 1)})")
                    if ontem:
                        gravar(p, ontem)
                        return
    if len(boas) != len(j["moedas"]):
        j["moedas"] = boas
        gravar(p, j)


# --------------------------------------------------------------------------
# rodada completa
# --------------------------------------------------------------------------
def conferir(dados: Path, anterior: str | None, rede: Rede, amostra=AMOSTRA_EXTERNA) -> dict:
    R = Registro()
    meta = ler(dados / "meta.json", {})
    lista = ler(dados / "ativos.json", [])
    ultimo_pregao = meta.get("ultimo_pregao") or AGORA.date().isoformat()
    base_ant = anterior.rstrip("/") if anterior else None
    meta_ant = rede.json(f"{base_ant}/meta.json") if base_ant else None
    lista_ant = rede.json(f"{base_ant}/ativos.json") if base_ant else None
    ontem = {a["t"]: a for a in (lista_ant or []) if isinstance(a, dict) and a.get("t")}
    if meta_ant and meta_ant.get("demo"):   # o que está no ar é demonstração: não serve de comparação
        meta_ant, ontem = None, {}

    # ---- globais: nada de publicar menos ou mais velho do que já está no ar
    if meta_ant:
        if meta_ant.get("ultimo_pregao") and ultimo_pregao < meta_ant["ultimo_pregao"]:
            R.add("geral", "ultimo_pregao", "bloqueio",
                  f"os dados de hoje ({dbr(ultimo_pregao)}) são mais velhos que os do site ({dbr(meta_ant['ultimo_pregao'])})")
        n0 = len(ontem)
        if n0 >= 50 and len(lista) < 0.9 * n0:
            R.add("geral", "ativos", "bloqueio", f"sumiram ativos demais: {n0} no ar, {len(lista)} hoje")

    detalhes: dict[str, dict] = {}
    for a in lista:
        tk = a.get("t")
        d = ler(dados / "ativo" / f"{tk}.json")
        if not d:
            R.add(tk, "arquivo", "em_conferencia", "ativo sem página de detalhes")
            continue
        detalhes[tk] = d

    # ---- dado a dado
    precisa_externo: set[str] = set()
    ruins: set[str] = set()
    por_externo: set[str] = set()
    for tk, d in detalhes.items():
        if not conferir_serie(tk, d, R, ultimo_pregao):
            ruins.add(tk)
            continue
        conferir_proventos(tk, d, R)
        conferir_indicadores(tk, d, R)
        o = ontem.get(tk)
        if o and ok(o.get("preco")) and o["preco"] > 0:
            mov = d["preco"] / o["preco"] - 1
            if abs(mov) > MUDANCA_GRANDE:
                precisa_externo.add(tk)
                R.add(tk, "preco", "aviso", f"preço mudou {pc(mov, 0, True)} desde o que estava no ar ({brl(o['preco'])} → {brl(d['preco'])})")
        if ok(d.get("var_dia")) and abs(d["var_dia"]) > 0.2:
            precisa_externo.add(tk)

    # ---- segunda fonte
    por_liq = sorted(detalhes, key=lambda t: -(detalhes[t].get("liquidez") or 0))
    alvo_ext = list(dict.fromkeys([t for t in por_liq if t in precisa_externo] + por_liq[:amostra]))
    ext = {"fonte": "Yahoo Finance", "conferidos": 0, "bateram": 0, "sem_par": 0, "divergentes": [], "indisponivel": False}
    if rede.ligada and alvo_ext:
        falhas_seguidas = 0
        for tk in alvo_ext:
            if tk in ruins:
                continue
            fech = fechamentos_yahoo(rede, tk)
            if not fech:
                falhas_seguidas += 1
                ext["sem_par"] += 1
                if falhas_seguidas >= 15 and ext["conferidos"] == 0:
                    ext["indisponivel"] = True
                    break
                continue
            falhas_seguidas = 0
            d = detalhes[tk]
            outro = fech.get(d["data"])
            if outro is None:
                ext["sem_par"] += 1
                continue
            k = d["preco"] / outro if outro else 0
            if any(abs(k / f - 1) < 0.03 for f in (10, 100, 1000, 0.1, 0.01, 0.001)):
                ext["sem_par"] += 1   # a segunda fonte está em outra unidade (ex.: centavos): não serve para comparar
                continue
            ext["conferidos"] += 1
            dif = abs(d["preco"] / outro - 1)
            if dif <= TOLERANCIA_EXTERNA or abs(d["preco"] - outro) <= 0.02 + 1e-9:   # ativos de centavos: 2 centavos de folga
                ext["bateram"] += 1
                precisa_externo.discard(tk)
            else:
                ext["divergentes"].append({"t": tk, "data": d["data"], "nosso": d["preco"], "outro": round(outro, 4),
                                           "dif": round(dif, 4)})
                R.add(tk, "preco", "segurado",
                      f"fechamento de {dbr(d['data'])} não bateu com a segunda fonte: {brl(d['preco'])} aqui, {brl(outro)} no Yahoo ({pc(dif, 1)})")
                ruins.add(tk)
                por_externo.add(tk)
            time.sleep(0.15)
    else:
        ext["indisponivel"] = True
    for tk in precisa_externo - ruins:
        R.add(tk, "preco", "em_conferencia", "mudança grande de preço que a segunda fonte não pôde confirmar")
    if ext["conferidos"] >= 30 and len(ext["divergentes"]) > 0.2 * ext["conferidos"]:
        R.add("geral", "precos", "bloqueio",
              f"{len(ext['divergentes'])} de {ext['conferidos']} preços não bateram com a segunda fonte (erro em massa)")

    # ---- quem não passou: fica com o último dado conferido ou vai com aviso
    status: dict[str, dict] = {}
    for tk in ruins:
        nivel_seg = any(i["nivel"] == "segurado" for i in R.de(tk))
        antigo = rede.json(f"{base_ant}/ativo/{tk}.json") if base_ant and ontem else None
        motivo = next((i["msg"] for i in R.de(tk) if i["nivel"] in ("segurado", "em_conferencia")), "dado em conferência")
        ant_conf = (antigo or {}).get("conferencia") or {}
        if (tk in por_externo and ant_conf.get("status") == "segurado" and ant_conf.get("desde")
                and data_ok(ant_conf["desde"]) and detalhes[tk].get("data")
                and (dt.date.fromisoformat(detalhes[tk]["data"][:10]) - dt.date.fromisoformat(ant_conf["desde"][:10])).days > 4):
            # já ficou segurado dias seguidos e a B3 (fonte oficial) segue firme: não congela o ativo, vai com aviso
            status[tk] = {"status": "em_conferencia", "motivo": motivo, "em": AGORA.isoformat(timespec="minutes")}
            R.add(tk, "preco", "em_conferencia", "a segunda fonte segue diferente há dias: vale o preço da B3, com aviso")
            continue
        if antigo and ok(antigo.get("preco")):
            R.itens = [i for i in R.itens if not (i["alvo"] == tk and i["nivel"] in ("corrigido", "ocultado"))]
            antigo["conferencia"] = {"status": "segurado", "motivo": motivo, "desde": antigo.get("data"),
                                     "em": AGORA.isoformat(timespec="minutes")}
            detalhes[tk] = antigo
            status[tk] = antigo["conferencia"]
            if not nivel_seg:
                R.add(tk, "preco", "segurado", "ficou com o último dado conferido")
        else:
            status[tk] = {"status": "em_conferencia", "motivo": motivo, "em": AGORA.isoformat(timespec="minutes")}
            if nivel_seg:
                R.add(tk, "preco", "em_conferencia", "sem dado anterior para segurar: vai com aviso")
    for tk in detalhes:
        if tk in status:
            continue
        niveis = {i["nivel"] for i in R.de(tk)}
        if "em_conferencia" in niveis:
            motivo = next(i["msg"] for i in R.de(tk) if i["nivel"] == "em_conferencia")
            status[tk] = {"status": "em_conferencia", "motivo": motivo, "em": AGORA.isoformat(timespec="minutes")}
        elif niveis & {"corrigido", "ocultado"}:
            status[tk] = {"status": "corrigido", "em": AGORA.isoformat(timespec="minutes")}

    afetados = sum(1 for s in status.values() if s["status"] in ("segurado", "em_conferencia"))
    if len(detalhes) >= 50 and afetados > max(15, 0.05 * len(detalhes)):
        R.add("geral", "ativos", "bloqueio", f"{afetados} ativos com problema no preço (mais de 5%): parece erro na fonte")

    # ---- grava o que passou
    por_tk = {a["t"]: a for a in lista}
    for tk, d in detalhes.items():
        if tk in status:
            d["conferencia"] = status[tk]
        else:
            d["conferencia"] = {"status": "ok", "em": AGORA.isoformat(timespec="minutes")}
        gravar(dados / "ativo" / f"{tk}.json", d)
        a = por_tk.get(tk)
        if a is None:
            continue
        ind, base = d.get("ind") or {}, d.get("base") or {}
        a.update({"preco": d.get("preco"), "var_dia": d.get("var_dia"), "var_12m": d.get("var_12m"),
                  "graham": d.get("graham"), "bazin": d.get("bazin"), "vm": base.get("valor_mercado"),
                  "magic": ind.get("magic_number"), "vp": ind.get("vp_cota")})
        if d.get("var_12m") is None or (tk in status and "var_30d" in {i["campo"] for i in R.de(tk) if i["nivel"] == "ocultado"}):
            a["var_30d"] = None if d.get("var_12m") is None else a.get("var_30d")
        for k in ("dy", "pl", "pvp", "roe", "m_liq", "roic", "ev_ebit", "dl_ebitda", "cagr_rec", "cagr_luc", "liq_corr"):
            if k in a:
                a[k] = ind.get(k)
        st = status.get(tk, {}).get("status")
        if st in ("segurado", "em_conferencia"):
            a["conf"] = st
        else:
            a.pop("conf", None)
    gravar(dados / "ativos.json", lista)

    ind_ant = rede.json(f"{base_ant}/indicadores.json") if base_ant and meta_ant else None
    cripto_ant = rede.json(f"{base_ant}/cripto.json") if base_ant and meta_ant else None
    conferir_indicadores_bcb(dados, ind_ant, R)
    conferir_tesouro(dados, R)
    conferir_cripto(dados, cripto_ant, rede, R)

    resumo = {
        "feito_em": AGORA.isoformat(timespec="minutes"),
        "ultimo_pregao": ultimo_pregao,
        "ativos": len(detalhes),
        "campos_conferidos": R.campos,
        "ativos_ok": sum(1 for tk in detalhes if tk not in status),
        "corrigidos": R.conta("corrigido"),
        "ocultados": R.conta("ocultado"),
        "segurados": sorted(tk for tk, s in status.items() if s["status"] == "segurado"),
        "em_conferencia": sorted(tk for tk, s in status.items() if s["status"] == "em_conferencia"),
        "avisos": R.conta("aviso"),
        "segunda_fonte": ext,
        "bloqueado": R.conta("bloqueio") > 0,
        "bloqueios": [i["msg"] for i in R.itens if i["nivel"] == "bloqueio"],
    }
    relatorio = {**resumo, "itens": R.itens[:1500], "itens_total": len(R.itens)}
    gravar(dados / "conferencia.json", relatorio)
    meta["conferencia"] = {k: v for k, v in resumo.items() if k not in ("segunda_fonte",)} | {
        "segunda_fonte": {k: ext[k] for k in ("fonte", "conferidos", "bateram", "indisponivel")}}
    gravar(dados / "meta.json", meta, compacto=False)
    return relatorio


def resumo_markdown(rel: dict) -> str:
    ext = rel["segunda_fonte"]
    linhas = [
        "## Conferência dos dados do Milhão",
        "",
        f"**{'🚫 PUBLICAÇÃO BLOQUEADA' if rel['bloqueado'] else '✅ Liberado para publicar'}** · pregão de {rel['ultimo_pregao']}",
        "",
        f"- {rel['ativos']} ativos, " + f"{rel['campos_conferidos']:,}".replace(",", ".") + " dados conferidos",
        f"- {rel['ativos_ok']} ativos sem nenhum ajuste",
        f"- {rel['corrigidos']} dados corrigidos, {rel['ocultados']} ocultados, {rel['avisos']} avisos",
        f"- segurados (ficaram com o dado de ontem): {', '.join(rel['segurados']) or 'nenhum'}",
        f"- em conferência (no ar com aviso): {', '.join(rel['em_conferencia']) or 'nenhum'}",
        f"- segunda fonte ({ext['fonte']}): " + ("indisponível hoje" if ext["indisponivel"] and not ext["conferidos"]
                                                 else f"{ext['bateram']} de {ext['conferidos']} preços bateram"),
    ]
    for b in rel["bloqueios"]:
        linhas.append(f"- **bloqueio:** {b}")
    graves = [i for i in rel["itens"] if i["nivel"] in ("segurado", "em_conferencia", "ocultado")][:60]
    if graves:
        linhas += ["", "| Ativo | Dado | O que houve |", "|---|---|---|"]
        linhas += [f"| {i['alvo']} | {i['campo']} | {i['nivel']}: {i['msg']} |" for i in graves]
    return "\n".join(linhas) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dados", default="site/data")
    ap.add_argument("--anterior", default="https://milhao.pages.dev/data",
                    help="endereço dos dados que estão no ar agora (para comparar com ontem)")
    ap.add_argument("--sem-rede", action="store_true", help="não busca nada na internet (testes)")
    ap.add_argument("--amostra", type=int, default=AMOSTRA_EXTERNA)
    a = ap.parse_args()
    rede = Rede(not a.sem_rede)
    rel = conferir(Path(a.dados), None if a.sem_rede else a.anterior, rede, a.amostra)
    md = resumo_markdown(rel)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    return 3 if rel["bloqueado"] else 0


if __name__ == "__main__":
    sys.exit(main())
