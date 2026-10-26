#!/usr/bin/env python3
"""Horário de encerramento das seções no 2º turno de 2026 e comparação 1T x 2T.

Produz em saida/secoes_2t/:
  municipios_presidente.csv   presidente por município, 1T e 2T (API de divulgação, 5.570 x 2 arquivos)
  munzona_presidente.csv      presidente por UF/município/zona/turno (dados abertos votacao_candidato_munzona_2026)
  secoes_2t.csv.gz            por seção no 2T: aptos, comparecimento, abertura, encerramento, votos (boletim de urna)
  zonas_2t_encerramento.csv   por zona: seções que encerraram depois das 17h de Brasília e seus votos
  municipios_2t_encerramento.csv  idem por município
Só biblioteca padrão.
"""
import csv
import gzip
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "saida", "secoes_2t")
RAW = os.path.join(HERE, "raw")
TMP = "/tmp/tse_secoes"
for d in (OUT, RAW, TMP):
    os.makedirs(d, exist_ok=True)
BASE = "https://resultados.tse.jus.br/oficial"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "Accept": "*/*"}
STATUS = {"inicio_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "passos": []}
UFS = ["ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms", "mt", "pa", "pb",
       "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc", "se", "sp", "to"]
# diferença (horas) do fuso local para Brasília: hora_brasilia = hora_local + OFFSET
OFFSET = {"AC": 2, "AM": 1, "RR": 1, "RO": 1, "MT": 1, "MS": 1}
CAND = {"13": "lula", "22": "flavio", "95": "branco", "96": "nulo"}


def log(msg, **kw):
    print(msg, flush=True)
    STATUS["passos"].append({"t": time.strftime("%H:%M:%S", time.gmtime()), "msg": msg, **kw})


def http(url, tries=3, timeout=120):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:
            time.sleep(2 * (i + 1))
    return 0, None


def baixar(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return 200
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=900) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        return 200
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        log(f"erro download {url}: {e!r}")
        return 0


def to_int(v):
    v = (v or "").strip()
    if v in ("", "#NULO#", "#NE#", "-1", "-3"):
        return 0
    try:
        return int(float(v.replace(".", "").replace(",", "")))
    except ValueError:
        return 0


# ------------------------------------------------------------------ 1) municípios via API
def municipios_api():
    code, txt = http(f"{BASE}/ele2026/6259/config/mun-e006259-cm.json")
    if code != 200:
        log(f"config de municípios HTTP {code}")
        return False
    cfg = json.loads(txt)
    muns = [(abr["cd"].lower(), m["cd"], m.get("nm", "")) for abr in cfg["abr"] for m in abr.get("mu", [])]
    log(f"municípios no config: {len(muns)}")
    alvos = [(uf, cd, nm, t, e) for uf, cd, nm in muns for t, e in (("1", "6257"), ("2", "6258"))]

    def um(a):
        uf, cd, nm, t, e = a
        u = f"{BASE}/ele2026/{e}/dados/{uf}/{uf}{cd}-c0001-e{int(e):06d}-u.json"
        code, txt = http(u, tries=3, timeout=60)
        if code != 200 or not txt:
            return (uf, cd, nm, t, None, code)
        try:
            j = json.loads(txt)
        except Exception:
            return (uf, cd, nm, t, None, "json")
        v, e_ = j.get("v", {}) or {}, j.get("e", {}) or {}
        rec = {"vv": to_int(v.get("vv")), "vb": to_int(v.get("vb")), "vn": to_int(v.get("vn")), "tv": to_int(v.get("tv")),
               "aptos": to_int(e_.get("te")), "comparecimento": to_int(e_.get("c")), "abstencao": to_int(e_.get("a")),
               "pst": (j.get("s", {}) or {}).get("pst", "")}
        votos = {}
        for agr in ((j.get("carg") or [{}])[0].get("agr") or []):
            for par in agr.get("par", []) or []:
                for c in par.get("cand", []) or []:
                    votos[str(c.get("n"))] = to_int(c.get("vap"))
        rec["lula"] = votos.get("13", 0); rec["flavio"] = votos.get("22", 0)
        rec["outros"] = sum(vv for n, vv in votos.items() if n not in ("13", "22"))
        return (uf, cd, nm, t, rec, 200)

    rows = [["uf", "cd_mun", "municipio", "turno", "aptos", "comparecimento", "abstencao", "vv", "vb", "vn", "tv",
             "lula", "flavio", "outros", "pst"]]
    falhas = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for uf, cd, nm, t, rec, code in ex.map(um, alvos):
            if rec is None:
                falhas.append((uf, cd, t, code)); continue
            rows.append([uf.upper(), cd, nm, t, rec["aptos"], rec["comparecimento"], rec["abstencao"], rec["vv"], rec["vb"],
                         rec["vn"], rec["tv"], rec["lula"], rec["flavio"], rec["outros"], rec["pst"]])
    with open(os.path.join(OUT, "municipios_presidente.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    log(f"municípios: {len(rows)-1} registros, {len(falhas)} falhas", falhas=falhas[:30])
    return len(rows) > 1


# ------------------------------------------------------------------ 2) munzona (dados abertos)
def munzona():
    u = "https://cdn.tse.jus.br/estatistica/sead/odsele/votacao_candidato_munzona/votacao_candidato_munzona_2026.zip"
    p = os.path.join(TMP, "votacao_candidato_munzona_2026.zip")
    code = baixar(u, p)
    log(f"munzona 2026 HTTP {code} bytes={os.path.getsize(p) if os.path.exists(p) else 0}")
    if code != 200:
        return False
    agg = defaultdict(lambda: defaultdict(int))
    zf = zipfile.ZipFile(p)
    names = [n for n in zf.namelist() if n.upper().endswith("_BRASIL.CSV")] or [n for n in zf.namelist() if n.lower().endswith(".csv")]
    turnos = defaultdict(int)
    for n in names:
        with io.TextIOWrapper(zf.open(n), encoding="latin-1", newline="") as f:
            rd = csv.reader(f, delimiter=";", quotechar='"')
            hdr = [h.strip().upper() for h in next(rd)]
            ix = {h: i for i, h in enumerate(hdr)}
            i_c, i_t, i_uf, i_m, i_nm, i_z, i_nr = (ix["CD_CARGO"], ix["NR_TURNO"], ix["SG_UF"], ix["CD_MUNICIPIO"],
                                                    ix["NM_MUNICIPIO"], ix["NR_ZONA"], ix["NR_CANDIDATO"])
            i_v = ix.get("QT_VOTOS_NOMINAIS_VALIDOS", ix.get("QT_VOTOS_NOMINAIS"))
            for row in rd:
                if len(row) < len(hdr) or row[i_c].strip() != "1":
                    continue
                t = row[i_t].strip(); turnos[t] += 1
                k = (row[i_uf], row[i_m], row[i_nm], row[i_z], t)
                nr = row[i_nr].strip()
                agg[k][CAND.get(nr, "outros")] += to_int(row[i_v])
    rows = [["uf", "cd_mun", "municipio", "zona", "turno", "lula", "flavio", "outros"]]
    for (uf, m, nm, z, t), v in sorted(agg.items()):
        rows.append([uf, m, nm, z, t, v.get("lula", 0), v.get("flavio", 0), v.get("outros", 0)])
    with open(os.path.join(OUT, "munzona_presidente.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    log(f"munzona: {len(rows)-1} linhas UF/mun/zona/turno; linhas de presidente por turno: {dict(turnos)}")
    return True


# ------------------------------------------------------------------ 3) boletins de urna 2T
def descobrir_bu():
    urls = set()
    for pg in ["https://dadosabertos.tse.jus.br/dataset/resultados-2026-boletim-de-urna",
               "https://dadosabertos.tse.jus.br/dataset/resultados-2026",
               "https://dadosabertos.tse.jus.br/api/3/action/package_search?q=boletim+de+urna+2026&rows=50"]:
        code, txt = http(pg, tries=2, timeout=60)
        log(f"descoberta BU: {pg} -> {code}")
        if code == 200 and txt:
            for m in re.findall(r'https?://[^"\'\s<>]*bweb_2t_[A-Z]{2}_\d+\.zip', txt):
                urls.add(m)
    if not urls:  # tentativa por padrão de nome com carimbo desconhecido: lista diretório
        code, txt = http("https://cdn.tse.jus.br/estatistica/sead/eleicoes/eleicoes2026/buweb/", tries=1, timeout=60)
        log(f"listagem buweb -> {code}")
        if code == 200 and txt:
            for m in re.findall(r'bweb_2t_[A-Z]{2}_\d+\.zip', txt):
                urls.add("https://cdn.tse.jus.br/estatistica/sead/eleicoes/eleicoes2026/buweb/" + m)
    return sorted(urls)


def parse_dt(s):
    """'25/10/2026 17:02:11' -> minutos desde 00:00; None se vazio."""
    m = re.search(r"(\d{2}):(\d{2})(?::(\d{2}))?", s or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def boletins_2t():
    urls = descobrir_bu()
    log(f"BU 2T: {len(urls)} arquivos encontrados", urls=urls[:40])
    if not urls:
        return False
    sec = {}
    for u in urls:
        p = os.path.join(TMP, os.path.basename(u))
        code = baixar(u, p)
        if code != 200:
            log(f"BU {u} HTTP {code}"); continue
        zf = zipfile.ZipFile(p)
        for n in [x for x in zf.namelist() if x.lower().endswith(".csv")]:
            with io.TextIOWrapper(zf.open(n), encoding="latin-1", newline="") as f:
                rd = csv.reader(f, delimiter=";", quotechar='"')
                hdr = [h.strip().upper() for h in next(rd)]
                ix = {h: i for i, h in enumerate(hdr)}
                i_cargo = ix.get("CD_CARGO_PERGUNTA", ix.get("CD_CARGO"))
                i_uf, i_m, i_nm, i_z, i_s = ix["SG_UF"], ix["CD_MUNICIPIO"], ix["NM_MUNICIPIO"], ix["NR_ZONA"], ix["NR_SECAO"]
                i_loc = ix.get("NR_LOCAL_VOTACAO"); i_ap, i_cp, i_ab = ix.get("QT_APTOS"), ix.get("QT_COMPARECIMENTO"), ix.get("QT_ABSTENCOES")
                i_ab_dt = next((ix[h] for h in hdr if "ABERTURA" in h), None)
                i_en_dt = next((ix[h] for h in hdr if "ENCERRAMENTO" in h), None)
                i_nv, i_qv = ix.get("NR_VOTAVEL"), ix.get("QT_VOTOS")
                i_tu = ix.get("DS_TIPO_URNA")
                if n == [x for x in zf.namelist() if x.lower().endswith(".csv")][0]:
                    log(f"BU {os.path.basename(u)}: colunas abertura/encerramento = "
                        f"{hdr[i_ab_dt] if i_ab_dt is not None else None} / {hdr[i_en_dt] if i_en_dt is not None else None}")
                for row in rd:
                    if len(row) < len(hdr) or (i_cargo is not None and row[i_cargo].strip() != "1"):
                        continue
                    k = (row[i_uf], row[i_m], row[i_z], row[i_s])
                    r = sec.get(k)
                    if r is None:
                        r = sec[k] = {"nm": row[i_nm], "local": row[i_loc] if i_loc is not None else "",
                                      "aptos": to_int(row[i_ap]) if i_ap is not None else 0,
                                      "comp": to_int(row[i_cp]) if i_cp is not None else 0,
                                      "abst": to_int(row[i_ab]) if i_ab is not None else 0,
                                      "abertura": row[i_ab_dt] if i_ab_dt is not None else "",
                                      "encerramento": row[i_en_dt] if i_en_dt is not None else "",
                                      "tipo_urna": row[i_tu] if i_tu is not None else "",
                                      "lula": 0, "flavio": 0, "branco": 0, "nulo": 0, "outros": 0}
                    if i_nv is not None:
                        r[CAND.get(row[i_nv].strip(), "outros")] += to_int(row[i_qv])
        log(f"BU {os.path.basename(u)}: seções acumuladas {len(sec)}")
        os.remove(p)
    # por seção
    with gzip.open(os.path.join(OUT, "secoes_2t.csv.gz"), "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["uf", "cd_mun", "municipio", "zona", "secao", "local", "tipo_urna", "aptos", "comparecimento", "abstencoes",
                    "abertura", "encerramento", "min_encerramento_brasilia", "depois_17h", "lula", "flavio", "branco", "nulo", "outros"])
        zon = defaultdict(lambda: defaultdict(int)); mun = defaultdict(lambda: defaultdict(int)); dist = defaultdict(lambda: defaultdict(int))
        for (uf, m, z, s), r in sec.items():
            me = parse_dt(r["encerramento"])
            mb = me + 60 * OFFSET.get(uf, 0) if me is not None else None
            tarde = mb is not None and mb > 17 * 60 + 5   # > 17h05 em Brasília (tolerância para fila normal)
            muito_tarde = mb is not None and mb >= 18 * 60
            w.writerow([uf, m, r["nm"], z, s, r["local"], r["tipo_urna"], r["aptos"], r["comp"], r["abst"], r["abertura"],
                        r["encerramento"], mb if mb is not None else "", int(tarde), r["lula"], r["flavio"], r["branco"], r["nulo"], r["outros"]])
            for d, key in ((zon, (uf, m, r["nm"], z)), (mun, (uf, m, r["nm"]))):
                a = d[key]; a["secoes"] += 1; a["aptos"] += r["aptos"]; a["comp"] += r["comp"]; a["lula"] += r["lula"]; a["flavio"] += r["flavio"]
                if tarde:
                    a["secoes_tarde"] += 1; a["aptos_tarde"] += r["aptos"]; a["comp_tarde"] += r["comp"]; a["lula_tarde"] += r["lula"]; a["flavio_tarde"] += r["flavio"]
                if muito_tarde:
                    a["secoes_apos18h"] += 1; a["comp_apos18h"] += r["comp"]; a["lula_apos18h"] += r["lula"]; a["flavio_apos18h"] += r["flavio"]
            if mb is not None:
                dist[uf][mb // 15 * 15] += 1
    cols = ["secoes", "aptos", "comp", "lula", "flavio", "secoes_tarde", "aptos_tarde", "comp_tarde", "lula_tarde", "flavio_tarde",
            "secoes_apos18h", "comp_apos18h", "lula_apos18h", "flavio_apos18h"]
    for nome_arq, d, keyn in (("zonas_2t_encerramento.csv", zon, ["uf", "cd_mun", "municipio", "zona"]),
                              ("municipios_2t_encerramento.csv", mun, ["uf", "cd_mun", "municipio"])):
        with open(os.path.join(OUT, nome_arq), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f); w.writerow(keyn + cols)
            for k, a in sorted(d.items()):
                w.writerow(list(k) + [a.get(c, 0) for c in cols])
    with open(os.path.join(OUT, "distribuicao_encerramento_uf.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["uf", "faixa_min_brasilia", "hora_brasilia", "secoes"])
        for uf in sorted(dist):
            for mb in sorted(dist[uf]):
                w.writerow([uf, mb, f"{mb//60:02d}:{mb%60:02d}", dist[uf][mb]])
    log(f"BU 2T: {len(sec)} seções gravadas")
    return len(sec) > 0


def main():
    ok = {}
    for nome, fn in (("municipios_api", municipios_api), ("munzona", munzona), ("boletins_2t", boletins_2t)):
        try:
            ok[nome] = fn()
        except Exception as e:
            log(f"{nome}: exceção {e!r}"); ok[nome] = False
    STATUS["ok"] = ok; STATUS["fim_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(RAW, "secoes_status.json"), "w", encoding="utf-8") as f:
        json.dump(STATUS, f, ensure_ascii=False, indent=1)
    print(json.dumps(ok))
    sys.exit(0 if any(ok.values()) else 1)


if __name__ == "__main__":
    main()
