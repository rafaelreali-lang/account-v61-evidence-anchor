#!/usr/bin/env python3
"""Série histórica dos 2º turnos presidenciais a partir dos dados abertos do TSE.

Para cada ano com 2º turno (1989, 2002, 2006, 2010, 2014, 2018, 2022) baixa:
  votacao_candidato_munzona_{ano}.zip  -> votos por candidato a presidente, por turno
  detalhe_votacao_munzona_{ano}.zip    -> aptos, comparecimento, abstenções, brancos, nulos, por turno
e grava saida/historico/{presidente_turnos.csv, totais_turnos.csv, serie_2t.csv, serie_2t.md}.
Só biblioteca padrão; leitura em streaming dentro do zip.
"""
import csv
import io
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "saida", "historico")
RAW = os.path.join(HERE, "raw")
TMP = "/tmp/tse_hist"
for d in (OUT, RAW, TMP):
    os.makedirs(d, exist_ok=True)
CDN = "https://cdn.tse.jus.br/estatistica/sead/odsele"
ANOS = [int(a) for a in (sys.argv[1:] or ["1989", "2002", "2006", "2010", "2014", "2018", "2022"])]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
STATUS = {"inicio_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "passos": []}


def log(msg, **kw):
    print(msg, flush=True)
    STATUS["passos"].append({"t": time.strftime("%H:%M:%S", time.gmtime()), "msg": msg, **kw})


def baixar(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return 200, os.path.getsize(dest)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        return 200, os.path.getsize(dest)
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception as e:
        log(f"erro download {url}: {e!r}")
        return 0, 0


def csvs_do_zip(zpath):
    """Gera (nome, leitor csv) para o CSV BRASIL se existir, senão para todos os CSVs."""
    zf = zipfile.ZipFile(zpath)
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    brasil = [n for n in names if n.upper().endswith("_BRASIL.CSV")]
    for n in (brasil or names):
        with io.TextIOWrapper(zf.open(n), encoding="latin-1", newline="") as f:
            yield n, csv.reader(f, delimiter=";", quotechar='"')


def to_int(v):
    v = (v or "").strip()
    if v in ("", "#NULO#", "#NE#", "-1", "-3"):
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


def ler_candidatos(zpath, ano):
    """{(turno): {(nr, nome_urna, partido): votos}} para o cargo Presidente."""
    out = defaultdict(lambda: defaultdict(int))
    linhas = 0
    for nome, rd in csvs_do_zip(zpath):
        hdr = [h.strip().upper() for h in next(rd)]
        ix = {h: i for i, h in enumerate(hdr)}
        i_cargo, i_dsc, i_turno = ix.get("CD_CARGO"), ix.get("DS_CARGO"), ix["NR_TURNO"]
        i_nr, i_nm, i_sg = ix.get("NR_CANDIDATO"), ix.get("NM_URNA_CANDIDATO", ix.get("NM_CANDIDATO")), ix.get("SG_PARTIDO")
        i_v = ix.get("QT_VOTOS_NOMINAIS_VALIDOS")
        i_vn = ix.get("QT_VOTOS_NOMINAIS")
        for row in rd:
            if len(row) < len(hdr):
                continue
            linhas += 1
            cargo = row[i_cargo].strip() if i_cargo is not None else ""
            if cargo != "1" and not (i_dsc is not None and row[i_dsc].strip().upper().startswith("PRESIDENTE")):
                continue
            v = to_int(row[i_v]) if i_v is not None and row[i_v].strip() not in ("", "#NULO#", "-1") else to_int(row[i_vn])
            key = (row[i_nr].strip() if i_nr is not None else "", row[i_nm].strip().upper(), row[i_sg].strip().upper() if i_sg is not None else "")
            out[row[i_turno].strip()][key] += v
        log(f"{ano} candidatos: {nome} {linhas} linhas, colunas votos: validos={'sim' if i_v is not None else 'não'}")
    return out


CAMPOS_DET = ["QT_APTOS", "QT_COMPARECIMENTO", "QT_ABSTENCOES", "QT_VOTOS", "QT_VOTOS_NOMINAIS", "QT_VOTOS_BRANCOS",
              "QT_VOTOS_NULOS", "QT_VOTOS_LEGENDA", "QT_VOTOS_ANULADOS", "QT_VOTOS_VALIDOS", "QT_VOTOS_NOMINAIS_VALIDOS",
              "QT_VOTOS_PENDENTES", "QT_VOTOS_ANULADOS_APU_SEP"]


def ler_detalhe(zpath, ano):
    """{turno: {campo: soma}} para o cargo Presidente."""
    out = defaultdict(lambda: defaultdict(int))
    for nome, rd in csvs_do_zip(zpath):
        hdr = [h.strip().upper() for h in next(rd)]
        ix = {h: i for i, h in enumerate(hdr)}
        i_cargo, i_dsc, i_turno = ix.get("CD_CARGO"), ix.get("DS_CARGO"), ix["NR_TURNO"]
        campos = [(c, ix[c]) for c in CAMPOS_DET if c in ix]
        n = 0
        for row in rd:
            if len(row) < len(hdr):
                continue
            cargo = row[i_cargo].strip() if i_cargo is not None else ""
            if cargo != "1" and not (i_dsc is not None and row[i_dsc].strip().upper().startswith("PRESIDENTE")):
                continue
            n += 1
            t = row[i_turno].strip()
            for c, i in campos:
                out[t][c] += to_int(row[i])
        log(f"{ano} detalhe: {nome} {n} linhas de presidente, campos: {[c for c, _ in campos]}")
    return out


def main():
    cand_rows = [["ano", "turno", "nr", "nome_urna", "partido", "votos"]]
    tot_rows = [["ano", "turno"] + CAMPOS_DET]
    serie = []
    for ano in ANOS:
        zc = os.path.join(TMP, f"votacao_candidato_munzona_{ano}.zip")
        zd = os.path.join(TMP, f"detalhe_votacao_munzona_{ano}.zip")
        c1, s1 = baixar(f"{CDN}/votacao_candidato_munzona/votacao_candidato_munzona_{ano}.zip", zc)
        c2, s2 = baixar(f"{CDN}/detalhe_votacao_munzona/detalhe_votacao_munzona_{ano}.zip", zd)
        log(f"{ano}: candidato HTTP {c1} {s1} bytes; detalhe HTTP {c2} {s2} bytes")
        cands = ler_candidatos(zc, ano) if c1 == 200 else {}
        det = ler_detalhe(zd, ano) if c2 == 200 else {}
        for t in sorted(cands):
            for (nr, nm, sg), v in sorted(cands[t].items(), key=lambda kv: -kv[1]):
                cand_rows.append([ano, t, nr, nm, sg, v])
        for t in sorted(det):
            tot_rows.append([ano, t] + [det[t].get(c, 0) for c in CAMPOS_DET])
        if "1" in cands and "2" in cands:
            c1t = sorted(cands["1"].items(), key=lambda kv: -kv[1])
            c2t = sorted(cands["2"].items(), key=lambda kv: -kv[1])
            (k_a, va1), (k_b, vb1) = c1t[0], c1t[1]        # A = líder do 1T, B = segundo
            v2 = dict(c2t)
            va2 = v2.get(k_a, 0); vb2 = v2.get(k_b, 0)
            if va2 == 0 or vb2 == 0:  # chave pode mudar (nome de urna); casa por número
                by_nr = {k[0]: v for k, v in c2t}
                va2 = va2 or by_nr.get(k_a[0], 0); vb2 = vb2 or by_nr.get(k_b[0], 0)
            val1 = sum(cands["1"].values()); val2 = sum(cands["2"].values())
            terc = val1 - va1 - vb1
            d1, d2 = det.get("1", {}), det.get("2", {})
            serie.append({"ano": ano, "lider_1t": f"{k_a[1]} ({k_a[2]})", "segundo_1t": f"{k_b[1]} ({k_b[2]})",
                          "lider_1t_votos_1t": va1, "lider_1t_votos_2t": va2, "segundo_1t_votos_1t": vb1, "segundo_1t_votos_2t": vb2,
                          "terceiros_1t": terc, "validos_1t": val1, "validos_2t": val2,
                          "ganho_lider": va2 - va1, "ganho_segundo": vb2 - vb1,
                          "aptos_1t": d1.get("QT_APTOS", 0), "aptos_2t": d2.get("QT_APTOS", 0),
                          "comparecimento_1t": d1.get("QT_COMPARECIMENTO", 0), "comparecimento_2t": d2.get("QT_COMPARECIMENTO", 0),
                          "abstencoes_1t": d1.get("QT_ABSTENCOES", 0), "abstencoes_2t": d2.get("QT_ABSTENCOES", 0),
                          "brancos_1t": d1.get("QT_VOTOS_BRANCOS", 0), "brancos_2t": d2.get("QT_VOTOS_BRANCOS", 0),
                          "nulos_1t": d1.get("QT_VOTOS_NULOS", 0), "nulos_2t": d2.get("QT_VOTOS_NULOS", 0),
                          "venceu_2t": "lider_1t" if va2 > vb2 else "segundo_1t"})
    with open(os.path.join(OUT, "presidente_turnos.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(cand_rows)
    with open(os.path.join(OUT, "totais_turnos.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(tot_rows)
    if serie:
        with open(os.path.join(OUT, "serie_2t.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(serie[0].keys()))
            w.writeheader(); w.writerows(serie)
        fmt = lambda n: f"{int(n):,}".replace(",", ".")  # noqa: E731
        md = ["# Segundos turnos presidenciais – série TSE (dados abertos)\n",
              "| Ano | Líder 1T | 1T | 2T | Segundo 1T | 1T | 2T | Terceiros | Válidos 1T | Válidos 2T | Comparec. 1T | Comparec. 2T | B+N 1T | B+N 2T | Venceu |",
              "|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for s in serie:
            md.append(f"| {s['ano']} | {s['lider_1t']} | {fmt(s['lider_1t_votos_1t'])} | {fmt(s['lider_1t_votos_2t'])} | {s['segundo_1t']} | "
                      f"{fmt(s['segundo_1t_votos_1t'])} | {fmt(s['segundo_1t_votos_2t'])} | {fmt(s['terceiros_1t'])} | {fmt(s['validos_1t'])} | "
                      f"{fmt(s['validos_2t'])} | {fmt(s['comparecimento_1t'])} | {fmt(s['comparecimento_2t'])} | "
                      f"{fmt(s['brancos_1t'] + s['nulos_1t'])} | {fmt(s['brancos_2t'] + s['nulos_2t'])} | {s['venceu_2t']} |")
        with open(os.path.join(OUT, "serie_2t.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        print("\n".join(md))
    STATUS["fim_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(RAW, "historico_status.json"), "w", encoding="utf-8") as f:
        json.dump(STATUS, f, ensure_ascii=False, indent=1)
    sys.exit(0 if serie else 1)


if __name__ == "__main__":
    main()
