#!/usr/bin/env python3
"""Extrai do TSE os votos do 1º turno de 2026 e agrega por partido e cargo.

Fontes, nesta ordem:
  A) Portal de Dados Abertos (CKAN) - votacao_partido_munzona_2026 / votacao_candidato_munzona_2026
     -> agregado por tse2026_agrega.py (nominais + legenda)
  B) API de divulgação (resultados.tse.jus.br) - um JSON por UF x cargo, soma dos votos
     de cada candidato pelo partido (partido = 2 primeiros dígitos do número do candidato)

Só biblioteca padrão. Tudo que foi tentado fica registrado em raw/status.json.
"""
import csv
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tse2026_agrega import DIREITA, CENTRO_DIREITA, ORDEM_CARGOS, bloco, fmt, pct  # noqa: E402

RAW = os.path.join(HERE, "raw")
SAIDA = os.path.join(HERE, "saida")
TMP = "/tmp/tse2026_dl"
for d in (RAW, os.path.join(RAW, "samples"), SAIDA, TMP):
    os.makedirs(d, exist_ok=True)

BASE = "https://resultados.tse.jus.br/oficial"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept": "*/*"}
STATUS = {"inicio_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "passos": []}

# número do partido (TSE) -> sigla canônica (sem acento)
NUM_SIGLA = {
    "10": "REPUBLICANOS", "11": "PP", "12": "PDT", "13": "PT", "14": "PTB", "15": "MDB",
    "16": "PSTU", "17": "PSL", "18": "REDE", "19": "PODE", "20": "PSC", "21": "PCB",
    "22": "PL", "23": "CIDADANIA", "25": "PRD", "27": "DC", "28": "PRTB", "29": "PCO",
    "30": "NOVO", "33": "MOBILIZA", "35": "PMB", "36": "AGIR", "40": "PSB", "43": "PV",
    "44": "UNIAO", "45": "PSDB", "50": "PSOL", "51": "PATRIOTA", "55": "PSD", "65": "PCDOB",
    "70": "AVANTE", "77": "SOLIDARIEDADE", "80": "UP", "90": "PROS",
}
CARGO_GRUPO = {"3": "Governador", "5": "Senador", "6": "Dep. Federal",
               "7": "Dep. Estadual/Distrital", "8": "Dep. Estadual/Distrital"}
UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB",
       "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]


def log(msg, **kw):
    print(msg, flush=True)
    STATUS["passos"].append({"t": time.strftime("%H:%M:%S", time.gmtime()), "msg": msg, **kw})


def http(url, method="GET", binary=False, tries=3, timeout=180):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read() if method == "GET" else b""
                return r.status, (data if binary else data.decode("utf-8", "replace")), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, None, dict(e.headers or {})
        except Exception as e:  # rede
            last = repr(e)
            time.sleep(3 * (i + 1))
    return 0, None, {"erro": last}


def to_int_br(s):
    if s is None:
        return 0
    s = str(s).strip().replace(".", "").replace(",", "")
    return int(s) if s.isdigit() else 0


def save(path, content, binary=False):
    with open(path, "wb" if binary else "w", encoding=None if binary else "utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------- A) dados abertos
def dados_abertos():
    found = []
    code, txt, _ = http("https://dadosabertos.tse.jus.br/api/3/action/package_show?id=resultados-2026")
    log(f"CKAN package_show resultados-2026 -> HTTP {code}")
    if code == 200 and txt:
        save(os.path.join(RAW, "ckan_resultados_2026.json"), txt)
        try:
            res = json.loads(txt).get("result", {}).get("resources", [])
            for r in res:
                u = r.get("url", "")
                if "votacao_partido_munzona" in u or "votacao_candidato_munzona" in u:
                    found.append(u)
            log(f"CKAN: {len(res)} recursos; alvo: {found}")
        except Exception as e:
            log(f"CKAN parse erro: {e!r}")
    for u in ["https://cdn.tse.jus.br/estatistica/sead/odsele/votacao_partido_munzona/votacao_partido_munzona_2026.zip",
              "https://cdn.tse.jus.br/estatistica/sead/odsele/votacao_candidato_munzona/votacao_candidato_munzona_2026.zip"]:
        if u not in found:
            found.append(u)
    ok = []
    for u in found:
        code, _, hdr = http(u, method="HEAD", tries=2, timeout=60)
        log(f"HEAD {u} -> {code} len={hdr.get('Content-Length')}")
        if code == 200:
            ok.append(u)
    if not ok:
        return False
    paths = []
    for u in ok:
        code, data, _ = http(u, binary=True, tries=2, timeout=900)
        if code == 200 and data:
            p = os.path.join(TMP, os.path.basename(u))
            save(p, data, binary=True)
            paths.append(p)
            log(f"baixado {u} ({len(data)} bytes)")
    if not paths:
        return False
    out = os.path.join(SAIDA, "dadosabertos")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tse2026_agrega.py"), *paths, "--out", out],
                       capture_output=True, text=True)
    log(f"tse2026_agrega.py rc={r.returncode}", stderr=r.stderr[-2000:])
    return r.returncode == 0


# ---------------------------------------------------------------- B) divulgação
def walk(obj, path=()):
    """Gera (path, dict) para todo dict dentro de obj."""
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from walk(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, path + (i,))


def eleicoes_de(cfg):
    """Lista (cd_eleicao, turno, nome, uf, cd_cargo, ds_cargo) a partir do ele-c.json."""
    out = []
    for _, d in walk(cfg):
        if "abr" in d and "cd" in d and isinstance(d.get("abr"), list):
            cd = str(d.get("cd"))
            turno = str(d.get("t", ""))
            nome = str(d.get("nm", ""))
            for abr in d["abr"]:
                if not isinstance(abr, dict):
                    continue
                uf = str(abr.get("cd", "")).upper()
                for cp in abr.get("cp", []) or []:
                    if isinstance(cp, dict):
                        out.append((cd, turno, nome, uf, str(cp.get("cd")), str(cp.get("ds", ""))))
    return out


def candidatos_de(j):
    """Extrai lista de candidatos de um JSON de resultado, tolerante a layout."""
    for key in ("cand", "candidatos", "c"):
        v = j.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    # procura primeira lista de dicts com 'vap' ou 'n'
    for _, d in walk(j):
        for k, v in d.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and ("vap" in v[0] or "nm" in v[0]):
                return v
    return []


def divulgacao():
    code, txt, _ = http(f"{BASE}/comum/config/ele-c.json")
    log(f"ele-c.json -> HTTP {code}")
    if code != 200 or not txt:
        return False
    save(os.path.join(RAW, "ele-c.json"), txt)
    cfg = json.loads(txt)
    todas = eleicoes_de(cfg)
    log(f"ele-c.json: {len(todas)} combinações eleição/UF/cargo", amostra=todas[:5])
    alvo = [e for e in todas if e[4] in CARGO_GRUPO and e[3] in UFS and e[1] in ("1", "")]
    # mantém só 2026
    alvo26 = [e for e in alvo if "2026" in e[2] or True]
    cds = sorted({e[0] for e in alvo26})
    log(f"alvo: {len(alvo26)} combinações; códigos de eleição: {cds}")
    if not alvo26:
        return False

    # sonda de padrões de URL com a primeira combinação
    cd0, _, _, uf0, cg0, _ = alvo26[0]
    padroes = [
        "{b}/ele2026/{cd}/dados-simplificados/{uf}/{uf}-c{cg:04d}-e{cd:06d}-r.json",
        "{b}/ele2026/{cd}/dados/{uf}/{uf}-c{cg:04d}-e{cd:06d}-r.json",
        "{b}/ele2026/{cd}/dados/{uf}/{uf}-c{cg:04d}-e{cd:06d}-u.json",
        "{b}/ele2026/{cd}/dados-simplificados/{uf}/{uf}-c{cg:04d}-e{cd:06d}-p.json",
    ]
    sonda = {}
    for p in padroes:
        u = p.format(b=BASE, cd=int(cd0), uf=uf0.lower(), cg=int(cg0))
        code, txt, hdr = http(u, tries=1, timeout=60)
        sonda[u] = {"http": code, "bytes": len(txt or "")}
        if code == 200 and txt:
            save(os.path.join(RAW, "samples", os.path.basename(u)), txt)
            try:
                j = json.loads(txt)
                sonda[u]["chaves_topo"] = sorted(j.keys()) if isinstance(j, dict) else type(j).__name__
                c = candidatos_de(j)
                sonda[u]["n_cand"] = len(c)
                sonda[u]["chaves_cand"] = sorted(c[0].keys()) if c else []
            except Exception as e:
                sonda[u]["erro"] = repr(e)
    log("sonda de padrões", sonda=sonda)
    save(os.path.join(RAW, "schema.json"), json.dumps(sonda, ensure_ascii=False, indent=1))
    usavel = [p for p in padroes
              if sonda.get(p.format(b=BASE, cd=int(cd0), uf=uf0.lower(), cg=int(cg0)), {}).get("n_cand")]
    if not usavel:
        return False
    padrao = usavel[0]
    log(f"padrão escolhido: {padrao}")

    cand_rows = [["cd_eleicao", "uf", "cd_cargo", "cargo", "grupo", "numero", "nome", "num_partido",
                  "partido", "partido_campo_json", "votos", "situacao"]]
    tot_rows = [["cd_eleicao", "uf", "cd_cargo", "cargo", "n_cand", "soma_votos_cand",
                 "vv_json", "vn_json", "vb_json", "tv_json", "pst_json", "pea_json", "psi_json", "dg", "hg"]]
    agg = defaultdict(lambda: defaultdict(int))
    falhas = []
    for cd, turno, nome, uf, cg, ds in alvo26:
        u = padrao.format(b=BASE, cd=int(cd), uf=uf.lower(), cg=int(cg))
        code, txt, _ = http(u, tries=3, timeout=120)
        if code != 200 or not txt:
            falhas.append({"url": u, "http": code})
            continue
        try:
            j = json.loads(txt)
        except Exception as e:
            falhas.append({"url": u, "erro": repr(e)})
            continue
        if uf == "AC":
            save(os.path.join(RAW, "samples", os.path.basename(u)), txt)
        cands = candidatos_de(j)
        grupo = CARGO_GRUPO[cg]
        soma = 0
        for c in cands:
            n = str(c.get("n", "")).strip()
            num_part = n[:2]
            campo = c.get("sgp") or c.get("sg") or c.get("cc") or c.get("part") or c.get("partido") or ""
            sigla = NUM_SIGLA.get(num_part, f"P{num_part}")
            v = to_int_br(c.get("vap", c.get("v", c.get("votos"))))
            soma += v
            agg[grupo][sigla] += v
            cand_rows.append([cd, uf, cg, ds, grupo, n, c.get("nm", ""), num_part, sigla, campo, v,
                              c.get("st", "")])
        tot_rows.append([cd, uf, cg, ds, len(cands), soma, j.get("vv"), j.get("vn"), j.get("vb"),
                         j.get("tv"), j.get("pst"), j.get("pea"), j.get("psi"), j.get("dg"), j.get("hg")])
        print(f"{uf} c{cg} {ds}: {len(cands)} cand, soma={soma}, vv={j.get('vv')}, pst={j.get('pst')}", flush=True)
    log(f"divulgação: {len(tot_rows)-1} arquivos lidos, {len(falhas)} falhas", falhas=falhas[:20])

    out = os.path.join(SAIDA, "divulgacao")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "candidatos.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(cand_rows)
    with open(os.path.join(out, "uf_cargo_totais.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(tot_rows)

    md = ["# TSE – Eleições 2026, 1º turno – votos por partido e por cargo (API de divulgação)\n",
          f"Fonte: {BASE} (Tribunal Superior Eleitoral). Padrão de arquivo: `{padrao}`.",
          f"Extraído em {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. "
          f"Arquivos lidos: {len(tot_rows)-1}; falhas: {len(falhas)}.",
          "Base: votos nominais apurados por candidato (campo `vap`), somados pelo partido do candidato "
          "(2 primeiros dígitos do número). Não inclui votos de legenda.",
          "Senador 2026: cada eleitor vota em 2 candidatos. Dep. Distrital (DF) junto com Dep. Estadual.\n",
          f"Direita: {', '.join(DIREITA)}  ", f"Centro-direita: {', '.join(CENTRO_DIREITA)}\n"]
    resumo = []
    csv_rows = [["cargo", "partido", "bloco", "votos", "pct_cargo"]]
    for grupo in ORDEM_CARGOS:
        if grupo not in agg:
            continue
        tab = agg[grupo]
        total = sum(tab.values())
        md += [f"## {grupo}\n", f"Total de votos nominais: **{fmt(total)}**\n",
               "| Partido | Bloco | Votos | % |", "|---|---|---:|---:|"]
        for p, v in sorted(tab.items(), key=lambda kv: -kv[1]):
            md.append(f"| {p} | {bloco(p)} | {fmt(v)} | {pct(v, total)} |")
            csv_rows.append([grupo, p, bloco(p), v, f"{100.0*v/total:.4f}" if total else ""])
        d = sum(v for p, v in tab.items() if bloco(p) == "Direita")
        cdv = sum(v for p, v in tab.items() if bloco(p) == "Centro-direita")
        rest = total - d - cdv
        md += [f"| **Subtotal Direita** | | **{fmt(d)}** | **{pct(d, total)}** |",
               f"| **Subtotal Centro-direita** | | **{fmt(cdv)}** | **{pct(cdv, total)}** |",
               f"| **Direita + Centro-direita** | | **{fmt(d+cdv)}** | **{pct(d+cdv, total)}** |",
               f"| Demais partidos | | {fmt(rest)} | {pct(rest, total)} |",
               f"| Total | | {fmt(total)} | 100,00% |\n"]
        resumo.append((grupo, d, cdv, rest, total))
    md += ["## Resumo por bloco\n",
           "| Cargo | Direita | Centro-direita | Direita + Centro-direita | Demais | Total | % Dir+CD |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for g, d, cdv, rest, total in resumo:
        md.append(f"| {g} | {fmt(d)} | {fmt(cdv)} | {fmt(d+cdv)} | {fmt(rest)} | {fmt(total)} | {pct(d+cdv, total)} |")
    md += ["", "## Checagens\n"]
    difs = []
    for r in tot_rows[1:]:
        vv = to_int_br(r[6])
        if vv:
            difs.append((r[1], r[3], r[5], vv, vv - r[5]))
    md.append("- Por UF x cargo: soma dos candidatos vs `vv` (votos válidos) do JSON. "
              "Diferença esperada em proporcionais = votos de legenda; em majoritários deve ser 0.")
    md += ["", "| UF | Cargo | Soma cand. | vv JSON | Diferença |", "|---|---|---:|---:|---:|"]
    for uf, ds, s, vv, dif in difs:
        md.append(f"| {uf} | {ds} | {fmt(s)} | {fmt(vv)} | {fmt(dif)} |")
    desconhecidos = sorted({p for g in agg for p in agg[g] if p.startswith("P") and p[1:].isdigit()})
    md.append(f"\n- Números de partido sem sigla mapeada: {desconhecidos or 'nenhum'}")
    md.append(f"- Falhas de download: {len(falhas)}")
    save(os.path.join(out, "resultado.md"), "\n".join(md))
    with open(os.path.join(out, "votos_por_partido_cargo.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(csv_rows)
    with open(os.path.join(out, "resumo_blocos.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cargo", "direita", "centro_direita", "direita_mais_centro_direita", "demais", "total", "pct_dir_cd"])
        for g, d, cdv, rest, total in resumo:
            w.writerow([g, d, cdv, d + cdv, rest, total, f"{100.0*(d+cdv)/total:.4f}" if total else ""])
    return True


def main():
    ok_a = ok_b = False
    try:
        ok_a = dados_abertos()
    except Exception as e:
        log(f"dados abertos: exceção {e!r}")
    try:
        ok_b = divulgacao()
    except Exception as e:
        log(f"divulgação: exceção {e!r}")
    STATUS["fim_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATUS["ok_dados_abertos"] = ok_a
    STATUS["ok_divulgacao"] = ok_b
    save(os.path.join(RAW, "status.json"), json.dumps(STATUS, ensure_ascii=False, indent=1))
    print(json.dumps({"ok_dados_abertos": ok_a, "ok_divulgacao": ok_b}))
    sys.exit(0 if (ok_a or ok_b) else 1)


if __name__ == "__main__":
    main()
