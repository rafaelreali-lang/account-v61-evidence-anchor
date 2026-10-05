#!/usr/bin/env python3
"""Extrai do TSE os votos do 1º turno de 2026 e agrega por partido e cargo.

Fontes, nesta ordem:
  A) Portal de Dados Abertos - votacao_partido_munzona_2026 / votacao_candidato_munzona_2026
     -> agregado por tse2026_agrega.py (nominais + legenda)
  B) API de divulgação (resultados.tse.jus.br) - um JSON por UF x cargo, soma dos votos
     de cada candidato pelo partido (partido = 2 primeiros dígitos do número do candidato)

Só biblioteca padrão. Tudo que foi tentado fica registrado em raw/status.json.
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
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
UFS = ["ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms", "mt", "pa", "pb",
       "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc", "se", "sp", "to"]
SUFIXOS = ["u", "r", "e", "ab", "s", "p", "t", "c", "v"]


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
    paths = []
    for u in found:
        code, data, hdr = http(u, binary=True, tries=2, timeout=900)
        log(f"GET {u} -> {code} bytes={len(data) if data else 0}")
        if code == 200 and data:
            p = os.path.join(TMP, os.path.basename(u))
            save(p, data, binary=True)
            paths.append(p)
            # registra conteúdo do zip e cabeçalho/1ª linha de cada CSV
            try:
                zf = zipfile.ZipFile(p)
                info = []
                for zi in zf.infolist():
                    ent = {"nome": zi.filename, "bytes": zi.file_size}
                    if zi.filename.lower().endswith(".csv"):
                        with io.TextIOWrapper(zf.open(zi), encoding="latin-1") as f:
                            ent["linhas_iniciais"] = [next(f, "").rstrip("\n")[:600] for _ in range(3)]
                    info.append(ent)
                save(os.path.join(RAW, os.path.basename(u) + ".listagem.json"),
                     json.dumps(info, ensure_ascii=False, indent=1))
            except Exception as e:
                log(f"zip {p}: {e!r}")
    if not paths:
        return False
    out = os.path.join(SAIDA, "dadosabertos")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tse2026_agrega.py"), *paths, "--out", out],
                       capture_output=True, text=True)
    log(f"tse2026_agrega.py rc={r.returncode}", stderr=r.stderr[-2000:])
    return r.returncode == 0


# ---------------------------------------------------------------- B) divulgação
def walk(obj, path=()):
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from walk(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, path + (i,))


def listas_de_dicts(j):
    """[(caminho, tamanho, chaves do 1º item)] para toda lista de dicts no JSON."""
    out = []
    for path, d in walk(j):
        for k, v in d.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                out.append(("/".join(map(str, path + (k,))), len(v), sorted(v[0].keys())))
    return out


def candidatos_de(j):
    """(lista de candidatos, dict pai) tolerante a layout (cand no topo ou dentro de abr[])."""
    for path, d in walk(j):
        v = d.get("cand")
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v, d
    for path, d in walk(j):
        for k, v in d.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "vap" in v[0]:
                return v, d
    return [], j


def eleicoes_2026(cfg):
    """[(ciclo, cd_eleicao, nome, [ufs], [(cd_cargo, ds_cargo)])] do 1º turno 2026."""
    out = []
    for pl in cfg.get("pl", []):
        ciclo = pl.get("c", "")
        for e in pl.get("e", []):
            if str(e.get("t", "")) != "1":
                continue
            cargos = []
            ufs = []
            for abr in e.get("abr", []) or []:
                cd = str(abr.get("cd", "")).lower()
                ufs += UFS if cd == "br" else [cd]
                for cp in abr.get("cp", []) or []:
                    if str(cp.get("cd")) in CARGO_GRUPO:
                        cargos.append((str(cp.get("cd")), cp.get("ds", "")))
            if cargos and ciclo == "ele2026":
                out.append((ciclo, str(e.get("cd")), e.get("nm", ""), sorted(set(ufs)), sorted(set(cargos))))
    return out


def padroes_app():
    """Lê o app de resultados do TSE e registra sufixos de arquivos .json usados."""
    achados = {}
    code, html, _ = http(f"{BASE}/app/index.html", tries=1, timeout=60)
    achados["index.html"] = code
    if code != 200 or not html:
        return achados
    scripts = re.findall(r'src="([^"]+\.js)"', html)
    achados["scripts"] = scripts
    pats = defaultdict(int)
    for s in scripts[:12]:
        u = s if s.startswith("http") else f"{BASE}/app/{s.lstrip('./')}"
        code, js, _ = http(u, tries=1, timeout=120)
        if code != 200 or not js:
            continue
        for m in re.findall(r'[-\w${}/.]{0,40}-[a-z]{1,3}\.json', js):
            pats[m] += 1
        for m in re.findall(r'dados-simplificados|arquivo-urna|/dados/|config/mun-', js):
            pats[m] += 1
    achados["padroes"] = sorted(pats.items(), key=lambda kv: -kv[1])[:80]
    return achados


def divulgacao():
    code, txt, _ = http(f"{BASE}/comum/config/ele-c.json")
    log(f"ele-c.json -> HTTP {code}")
    if code != 200 or not txt:
        return False
    save(os.path.join(RAW, "ele-c.json"), txt)
    cfg = json.loads(txt)
    eleicoes = eleicoes_2026(cfg)
    log("eleições 2026 1º turno com cargos-alvo",
        eleicoes=[(c, cd, nm, len(u), cg) for c, cd, nm, u, cg in eleicoes])
    if not eleicoes:
        return False
    try:
        app = padroes_app()
        save(os.path.join(RAW, "app_padroes.json"), json.dumps(app, ensure_ascii=False, indent=1))
        log("padrões do app", n=len(app.get("padroes", [])))
    except Exception as e:
        log(f"app: {e!r}")

    ciclo, cd, nome, ufs, cargos = eleicoes[0]
    cdi = int(cd)
    # ---- sonda: UF pequena (ac), cargo majoritário (3) e proporcional (6)
    sonda = {}
    candidatos_url = []
    for cg in ("3", "6"):
        for suf in SUFIXOS:
            candidatos_url.append(f"{BASE}/{ciclo}/{cd}/dados/ac/ac-c{int(cg):04d}-e{cdi:06d}-{suf}.json")
        candidatos_url.append(f"{BASE}/{ciclo}/{cd}/dados-simplificados/ac/ac-c{int(cg):04d}-e{cdi:06d}-r.json")
        candidatos_url.append(f"{BASE}/{ciclo}/{cd}/dados/ac/ac01120-c{int(cg):04d}-e{cdi:06d}-u.json")
    candidatos_url.append(f"{BASE}/{ciclo}/{cd}/config/mun-e{cdi:06d}-cm.json")
    candidatos_url.append(f"{BASE}/{ciclo}/{cd}/config/ac/ac-e{cdi:06d}-cm.json")
    padrao_ok = None
    for u in candidatos_url:
        code, txt, _ = http(u, tries=1, timeout=60)
        ent = {"http": code, "bytes": len(txt or "")}
        if code == 200 and txt:
            save(os.path.join(RAW, "samples", os.path.basename(u)), txt[:400000])
            try:
                j = json.loads(txt)
                ent["chaves_topo"] = sorted(j.keys()) if isinstance(j, dict) else type(j).__name__
                ent["listas"] = listas_de_dicts(j)[:15]
                c, pai = candidatos_de(j)
                ent["n_cand"] = len(c)
                ent["chaves_cand"] = sorted(c[0].keys()) if c else []
                ent["chaves_pai"] = sorted(pai.keys())[:40]
                if c and "/dados/ac/ac-c" in u and padrao_ok is None:
                    padrao_ok = u.replace("/ac/ac-c0003-", "/{uf}/{uf}-c{cg:04d}-").replace("/ac/ac-c0006-", "/{uf}/{uf}-c{cg:04d}-")
            except Exception as e:
                ent["erro"] = repr(e)
        sonda[u] = ent
        print(f"sonda {u} -> {code} {ent.get('n_cand', '')}", flush=True)
    save(os.path.join(RAW, "schema.json"), json.dumps(sonda, ensure_ascii=False, indent=1))
    log("sonda concluída", padrao=padrao_ok)
    if not padrao_ok:
        return False

    cand_rows = [["cd_eleicao", "uf", "cd_cargo", "cargo", "grupo", "numero", "nome", "num_partido",
                  "partido", "campo_cc", "votos", "situacao"]]
    tot_rows = [["cd_eleicao", "uf", "cd_cargo", "cargo", "n_cand", "soma_votos_cand",
                 "vv", "vn", "vb", "tv", "pst", "pea", "psi", "dg", "hg"]]
    agg = defaultdict(lambda: defaultdict(int))
    falhas = []
    for uf in ufs:
        for cg, ds in cargos:
            if (cg == "8") != (uf == "df"):
                continue  # distrital só no DF; estadual nos demais
            u = padrao_ok.format(uf=uf, cg=int(cg))
            code, txt, _ = http(u, tries=3, timeout=180)
            if code != 200 or not txt:
                falhas.append({"url": u, "http": code})
                continue
            try:
                j = json.loads(txt)
            except Exception as e:
                falhas.append({"url": u, "erro": repr(e)})
                continue
            cands, pai = candidatos_de(j)
            grupo = CARGO_GRUPO[cg]
            soma = 0
            for c in cands:
                n = str(c.get("n", "")).strip()
                num_part = n[:2]
                sigla = NUM_SIGLA.get(num_part, f"P{num_part}")
                v = to_int_br(c.get("vap", c.get("v", c.get("votos"))))
                soma += v
                agg[grupo][sigla] += v
                cand_rows.append([cd, uf.upper(), cg, ds, grupo, n, c.get("nm", ""), num_part, sigla,
                                  c.get("cc", c.get("sgp", "")), v, c.get("st", "")])
            g = lambda k: pai.get(k, j.get(k))  # noqa: E731
            tot_rows.append([cd, uf.upper(), cg, ds, len(cands), soma, g("vv"), g("vn"), g("vb"), g("tv"),
                             g("pst"), g("pea"), g("psi"), j.get("dg"), j.get("hg")])
            print(f"{uf} c{cg} {ds}: {len(cands)} cand, soma={soma}, vv={g('vv')}, pst={g('pst')}", flush=True)
    log(f"divulgação: {len(tot_rows)-1} arquivos lidos, {len(falhas)} falhas", falhas=falhas[:30])
    if len(tot_rows) <= 1:
        return False

    out = os.path.join(SAIDA, "divulgacao")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "candidatos.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(cand_rows)
    with open(os.path.join(out, "uf_cargo_totais.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(tot_rows)

    md = ["# TSE – Eleições 2026, 1º turno – votos por partido e por cargo (API de divulgação)\n",
          f"Fonte: {BASE} (Tribunal Superior Eleitoral). Eleição `{cd}` – {nome}. Padrão: `{padrao_ok}`.",
          f"Extraído em {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. "
          f"Arquivos lidos: {len(tot_rows)-1}; falhas: {len(falhas)}.",
          "Base: votos nominais apurados por candidato (`vap`), somados pelo partido do candidato "
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
    for g_, d, cdv, rest, total in resumo:
        md.append(f"| {g_} | {fmt(d)} | {fmt(cdv)} | {fmt(d+cdv)} | {fmt(rest)} | {fmt(total)} | {pct(d+cdv, total)} |")
    md += ["", "## Checagens\n",
           "- Por UF x cargo: soma dos candidatos vs `vv` (votos válidos) do JSON. "
           "Diferença esperada em proporcionais = votos de legenda; em majoritários deve ser 0. "
           "`pst` = % de seções totalizadas.", "",
           "| UF | Cargo | N cand | Soma cand. | vv | Diferença | pst |", "|---|---|---:|---:|---:|---:|---:|"]
    for r in tot_rows[1:]:
        vv = to_int_br(r[6])
        md.append(f"| {r[1]} | {r[3]} | {r[4]} | {fmt(r[5])} | {fmt(vv)} | {fmt(vv - r[5])} | {r[10]} |")
    desconhecidos = sorted({p for g_ in agg for p in agg[g_] if p.startswith("P") and p[1:].isdigit()})
    md.append(f"\n- Números de partido sem sigla mapeada: {desconhecidos or 'nenhum'}")
    md.append(f"- Falhas de download: {len(falhas)}")
    save(os.path.join(out, "resultado.md"), "\n".join(md))
    with open(os.path.join(out, "votos_por_partido_cargo.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(csv_rows)
    with open(os.path.join(out, "resumo_blocos.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cargo", "direita", "centro_direita", "direita_mais_centro_direita", "demais", "total", "pct_dir_cd"])
        for g_, d, cdv, rest, total in resumo:
            w.writerow([g_, d, cdv, d + cdv, rest, total, f"{100.0*(d+cdv)/total:.4f}" if total else ""])
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
