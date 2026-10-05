#!/usr/bin/env python3
"""Extrai do TSE os votos do 1º turno de 2026 e agrega por partido e cargo.

Fontes, nesta ordem:
  A) Portal de Dados Abertos - votacao_partido_munzona_2026 / votacao_candidato_munzona_2026
     -> agregado por tse2026_agrega.py (nominais + legenda). Só funciona quando o TSE publicar.
  B) API de divulgação (resultados.tse.jus.br/oficial/ele2026/6259/dados/<uf>/<uf>-c<cargo>-e006259-u.json)
     Estrutura: carg[0].agr[*] (coligação/federação/partido isolado) .par[*] (partido) .cand[*] (candidato)
       par.sg   sigla do partido
       par.tvtn total de votos totalizados nominais (válidos)      par.tvan apurados nominais (inclui sub judice)
       par.tvtl total de votos totalizados de legenda (válidos)    par.tval apurados de legenda
       cand.vap votos apurados do candidato; cand.dvt 'Válido' ou 'Anulado sub judice'
     Topo: v.vnom (válidos nominais), v.vl (válidos legenda), v.vv (válidos), v.vansj (anulados sub judice),
           s.pst (% seções totalizadas).

Só biblioteca padrão. Tudo que foi tentado fica registrado em raw/status.json.
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tse2026_agrega import DIREITA, CENTRO_DIREITA, ORDEM_CARGOS, bloco, fmt, pct  # noqa: E402
ORDEM = ORDEM_CARGOS + ["Presidente"]

RAW = os.path.join(HERE, "raw")
SAIDA = os.path.join(HERE, "saida")
TMP = "/tmp/tse2026_dl"
for d in (RAW, os.path.join(RAW, "samples"), SAIDA, TMP):
    os.makedirs(d, exist_ok=True)

BASE = "https://resultados.tse.jus.br/oficial"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept": "*/*"}
STATUS = {"inicio_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "passos": []}

CARGO_GRUPO = {"1": "Presidente", "3": "Governador", "5": "Senador", "6": "Dep. Federal",
               "7": "Dep. Estadual/Distrital", "8": "Dep. Estadual/Distrital"}
UFS = ["ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms", "mt", "pa", "pb",
       "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc", "se", "sp", "to"]
ALIAS = {"PODEMOS": "PODE", "PROGRESSISTAS": "PP", "UNIAO BRASIL": "UNIAO", "PC DO B": "PCDOB",
         "SD": "SOLIDARIEDADE"}


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


def norm_sigla(sg):
    s = unicodedata.normalize("NFKD", sg or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = " ".join(s.upper().split())
    return ALIAS.get(s, s)


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
def eleicoes_2026(cfg):
    """[(ciclo, cd_eleicao, nome, [ufs], [(cd_cargo, ds_cargo)])] do 1º turno 2026."""
    out = []
    for pl in cfg.get("pl", []):
        ciclo = pl.get("c", "")
        for e in pl.get("e", []):
            if str(e.get("t", "")) != "1":
                continue
            cargos, ufs = [], []
            for abr in e.get("abr", []) or []:
                cd = str(abr.get("cd", "")).lower()
                ufs += UFS if cd == "br" else [cd]
                for cp in abr.get("cp", []) or []:
                    if str(cp.get("cd")) in CARGO_GRUPO:
                        cargos.append((str(cp.get("cd")), cp.get("ds", "")))
            if cargos and ciclo == "ele2026":
                if all(c == "1" for c, _ in cargos):
                    ufs = ["br"]  # presidente: arquivo nacional
                out.append((ciclo, str(e.get("cd")), e.get("nm", ""), sorted(set(ufs)), sorted(set(cargos))))
    return out


def processar_json(j):
    """Lê um arquivo -u.json de UF x cargo. Retorna (partidos, candidatos, totais)."""
    partidos, cands = [], []
    carg = (j.get("carg") or [{}])[0]
    for agr in carg.get("agr", []) or []:
        for par in agr.get("par", []) or []:
            sg = norm_sigla(par.get("sg", ""))
            p = {"sg": sg, "sg_tse": par.get("sg", ""), "num": par.get("n", ""), "nfed": par.get("nfed", ""),
                 "agr_tp": agr.get("tp", ""), "agr_nm": agr.get("nm", ""),
                 "tvtn": to_int_br(par.get("tvtn")), "tvan": to_int_br(par.get("tvan")),
                 "tvtl": to_int_br(par.get("tvtl")), "tval": to_int_br(par.get("tval")),
                 "soma_vap": 0, "soma_vap_valido": 0, "n_cand": 0}
            for c in par.get("cand", []) or []:
                v = to_int_br(c.get("vap"))
                valido = str(c.get("dvt", "")).strip().lower() == "válido"
                p["soma_vap"] += v
                p["soma_vap_valido"] += v if valido else 0
                p["n_cand"] += 1
                cands.append({"sg": sg, "num": c.get("n", ""), "nm": c.get("nm", ""), "nmu": c.get("nmu", ""),
                              "vap": v, "dvt": c.get("dvt", ""), "st": c.get("st", ""), "seq": c.get("seq", "")})
            partidos.append(p)
    v, s = j.get("v", {}) or {}, j.get("s", {}) or {}
    totais = {"vnom": to_int_br(v.get("vnom")), "vl": to_int_br(v.get("vl")), "vv": to_int_br(v.get("vv")),
              "vansj": to_int_br(v.get("vansj")), "vb": to_int_br(v.get("vb")), "vn": to_int_br(v.get("vn")),
              "tv": to_int_br(v.get("tv")), "pst": s.get("pst", ""), "dg": j.get("dg", ""), "hg": j.get("hg", "")}
    return partidos, cands, totais


def divulgacao():
    code, txt, _ = http(f"{BASE}/comum/config/ele-c.json")
    log(f"ele-c.json -> HTTP {code}")
    if code != 200 or not txt:
        return False
    save(os.path.join(RAW, "ele-c.json"), txt)
    eleicoes = eleicoes_2026(json.loads(txt))
    log("eleições 2026 1º turno com cargos-alvo",
        eleicoes=[(c, cd, nm, len(u), cg) for c, cd, nm, u, cg in eleicoes])
    if not eleicoes:
        return False
    padroes = {cd: f"{BASE}/{ciclo}/{cd}/dados/{{uf}}/{{uf}}-c{{cg:04d}}-e{int(cd):06d}-u.json"
               for ciclo, cd, nome, ufs, cargos in eleicoes}
    nome = "; ".join(f"{cd} – {nm}" for _, cd, nm, _, _ in eleicoes)
    padrao = "; ".join(padroes.values())
    cd = ", ".join(padroes)

    # linhas por UF x cargo x partido; por candidato; por UF x cargo
    part_rows = [["uf", "cd_cargo", "cargo", "grupo", "partido", "sigla_tse", "num_partido", "federacao",
                  "tipo_agremiacao", "agremiacao", "n_cand", "nominais_validos_tvtn", "nominais_apurados_tvan",
                  "legenda_validos_tvtl", "legenda_apurados_tval", "soma_vap_cand", "soma_vap_cand_validos"]]
    cand_rows = [["uf", "cd_cargo", "cargo", "grupo", "partido", "numero", "nome", "nome_urna", "votos_vap",
                  "validade", "situacao", "seq"]]
    tot_rows = [["uf", "cd_cargo", "cargo", "grupo", "n_partidos", "n_cand", "soma_tvtn", "soma_tvtl",
                 "soma_vap", "vnom", "vl", "vv", "vansj", "vb", "vn", "tv", "pst", "dg", "hg",
                 "ok_tvtn_eq_vnom", "ok_tvtl_eq_vl", "ok_vap_eq_vnom_mais_vansj"]]
    agg = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # grupo -> sigla -> [nominais, legenda]
    falhas, pst_nao_100 = [], []
    for ciclo_e, cd_e, nome_e, ufs, cargos in eleicoes:
      for uf in ufs:
        for cg, ds in cargos:
            if (cg == "8") != (uf == "df"):
                continue  # distrital só no DF; estadual nos demais
            u = padroes[cd_e].format(uf=uf, cg=int(cg))
            code, txt, _ = http(u, tries=3, timeout=180)
            if code != 200 or not txt:
                falhas.append({"url": u, "http": code})
                continue
            try:
                j = json.loads(txt)
            except Exception as e:
                falhas.append({"url": u, "erro": repr(e)})
                continue
            if uf in ("ac", "df"):
                save(os.path.join(RAW, "samples", os.path.basename(u)), txt)
            grupo = CARGO_GRUPO[cg]
            partidos, cands, t = processar_json(j)
            s_tvtn = sum(p["tvtn"] for p in partidos)
            s_tvtl = sum(p["tvtl"] for p in partidos)
            s_vap = sum(p["soma_vap"] for p in partidos)
            for p in partidos:
                agg[grupo][p["sg"]][0] += p["tvtn"]
                agg[grupo][p["sg"]][1] += p["tvtl"]
                part_rows.append([uf.upper(), cg, ds, grupo, p["sg"], p["sg_tse"], p["num"], p["nfed"], p["agr_tp"],
                                  p["agr_nm"], p["n_cand"], p["tvtn"], p["tvan"], p["tvtl"], p["tval"],
                                  p["soma_vap"], p["soma_vap_valido"]])
            for c in cands:
                cand_rows.append([uf.upper(), cg, ds, grupo, c["sg"], c["num"], c["nm"], c["nmu"], c["vap"],
                                  c["dvt"], c["st"], c["seq"]])
            tot_rows.append([uf.upper(), cg, ds, grupo, len(partidos), len(cands), s_tvtn, s_tvtl, s_vap,
                             t["vnom"], t["vl"], t["vv"], t["vansj"], t["vb"], t["vn"], t["tv"], t["pst"],
                             t["dg"], t["hg"], s_tvtn == t["vnom"], s_tvtl == t["vl"],
                             s_vap == t["vnom"] + t["vansj"]])
            if t["pst"] != "100,00":
                pst_nao_100.append((uf.upper(), ds, t["pst"]))
            print(f"{uf} c{cg} {ds}: {len(partidos)} partidos, {len(cands)} cand, tvtn={s_tvtn} vnom={t['vnom']} "
                  f"tvtl={s_tvtl} vl={t['vl']} pst={t['pst']}", flush=True)
    log(f"divulgação: {len(tot_rows)-1} arquivos lidos, {len(falhas)} falhas", falhas=falhas[:30],
        pst_nao_100=pst_nao_100)
    if len(tot_rows) <= 1:
        return False

    out = os.path.join(SAIDA, "divulgacao")
    os.makedirs(out, exist_ok=True)
    for nome_arq, rows in (("partidos_uf_cargo.csv", part_rows), ("candidatos.csv", cand_rows),
                           ("uf_cargo_totais.csv", tot_rows)):
        with open(os.path.join(out, nome_arq), "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(rows)

    n_ok = sum(1 for r in tot_rows[1:] if r[19] and r[20] and r[21])
    md = ["# TSE – Eleições 2026, 1º turno – votos por partido e por cargo\n",
          f"Fonte: Tribunal Superior Eleitoral, API de divulgação de resultados ({BASE}). "
          f"Eleição `{cd}` – {nome}.",
          f"Arquivos: `{padrao}` (um por UF x cargo). Lidos: {len(tot_rows)-1}; falhas: {len(falhas)}. "
          f"Extraído em {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}.",
          "Votos **nominais válidos** = soma dos votos de todos os candidatos do partido (`par.tvtn`). "
          "**Legenda** = votos na legenda do partido (`par.tvtl`), só em proporcionais. "
          "**Total válidos** = nominais + legenda.",
          "Senador 2026: cada eleitor vota em 2 candidatos. Dep. Distrital (DF) agregado com Dep. Estadual.",
          f"Seções totalizadas (`pst`) = 100,00% em todos os arquivos: {'sim' if not pst_nao_100 else 'NÃO: ' + str(pst_nao_100)}.",
          f"Checagem por UF x cargo (soma partidos = totais do TSE): {n_ok} de {len(tot_rows)-1} ok.\n",
          f"Direita: {', '.join(DIREITA)}  ", f"Centro-direita: {', '.join(CENTRO_DIREITA)}\n"]
    resumo = []
    csv_rows = [["cargo", "partido", "bloco", "nominais_validos", "legenda", "total_validos", "pct_total_validos"]]
    nao_map = set()
    for grupo in ORDEM:
        if grupo not in agg:
            continue
        tab = {sg: (v[0], v[1], v[0] + v[1]) for sg, v in agg[grupo].items()}
        tot_nom = sum(t[0] for t in tab.values())
        tot_leg = sum(t[1] for t in tab.values())
        total = tot_nom + tot_leg
        md += [f"## {grupo}\n",
               f"Nominais válidos: **{fmt(tot_nom)}** · Legenda: **{fmt(tot_leg)}** · Total válidos: **{fmt(total)}**\n",
               "| Partido | Bloco | Nominais válidos | Legenda | Total válidos | % total |",
               "|---|---|---:|---:|---:|---:|"]
        for sg, (nom, leg, tt) in sorted(tab.items(), key=lambda kv: -kv[1][2]):
            b = bloco(sg)
            if b == "Demais":
                nao_map.add(sg)
            md.append(f"| {sg} | {b} | {fmt(nom)} | {fmt(leg)} | {fmt(tt)} | {pct(tt, total)} |")
            csv_rows.append([grupo, sg, b, nom, leg, tt, f"{100.0*tt/total:.4f}" if total else ""])
        d = [sum(t[i] for sg, t in tab.items() if bloco(sg) == "Direita") for i in (0, 1, 2)]
        c = [sum(t[i] for sg, t in tab.items() if bloco(sg) == "Centro-direita") for i in (0, 1, 2)]
        r = [tot_nom - d[0] - c[0], tot_leg - d[1] - c[1], total - d[2] - c[2]]
        md += [f"| **Subtotal Direita** | | **{fmt(d[0])}** | **{fmt(d[1])}** | **{fmt(d[2])}** | **{pct(d[2], total)}** |",
               f"| **Subtotal Centro-direita** | | **{fmt(c[0])}** | **{fmt(c[1])}** | **{fmt(c[2])}** | **{pct(c[2], total)}** |",
               f"| **Direita + Centro-direita** | | **{fmt(d[0]+c[0])}** | **{fmt(d[1]+c[1])}** | **{fmt(d[2]+c[2])}** | **{pct(d[2]+c[2], total)}** |",
               f"| Demais partidos | | {fmt(r[0])} | {fmt(r[1])} | {fmt(r[2])} | {pct(r[2], total)} |",
               f"| Total válidos | | {fmt(tot_nom)} | {fmt(tot_leg)} | {fmt(total)} | 100,00% |\n"]
        resumo.append((grupo, d, c, r, tot_nom, tot_leg, total))

    md += ["## Resumo por bloco – votos nominais válidos (soma dos candidatos)\n",
           "| Cargo | Direita | Centro-direita | Direita + Centro-direita | Demais | Total nominais | % só Direita | % Dir+CD |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for g, d, c, r, tn, tl, tt in resumo:
        md.append(f"| {g} | {fmt(d[0])} | {fmt(c[0])} | {fmt(d[0]+c[0])} | {fmt(r[0])} | {fmt(tn)} | {pct(d[0], tn)} | {pct(d[0]+c[0], tn)} |")
    md += ["", "## Resumo por bloco – total válidos (nominais + legenda)\n",
           "| Cargo | Direita | Centro-direita | Direita + Centro-direita | Demais | Total válidos | % só Direita | % Dir+CD |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for g, d, c, r, tn, tl, tt in resumo:
        md.append(f"| {g} | {fmt(d[2])} | {fmt(c[2])} | {fmt(d[2]+c[2])} | {fmt(r[2])} | {fmt(tt)} | {pct(d[2], tt)} | {pct(d[2]+c[2], tt)} |")
    md += ["", "## Siglas fora das listas Direita / Centro-direita (contadas em Demais)\n",
           ", ".join(sorted(nao_map)) or "(nenhuma)", "",
           "## Checagens por UF x cargo\n",
           "Soma dos partidos vs totais do próprio arquivo do TSE: `tvtn`=`vnom` (nominais válidos), "
           "`tvtl`=`vl` (legenda), soma `vap` = `vnom`+`vansj` (apurados incluem anulados sub judice).\n",
           "| UF | Cargo | Partidos | Cand. | Σ nominais | vnom | Σ legenda | vl | vv | sub judice | pst | ok |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:-:|"]
    for r in tot_rows[1:]:
        ok = "sim" if (r[19] and r[20] and r[21]) else "NÃO"
        md.append(f"| {r[0]} | {r[2]} | {r[4]} | {r[5]} | {fmt(r[6])} | {fmt(r[9])} | {fmt(r[7])} | {fmt(r[10])} | "
                  f"{fmt(r[11])} | {fmt(r[12])} | {r[16]} | {ok} |")
    md.append(f"\n- Falhas de download: {len(falhas)}")
    save(os.path.join(out, "resultado.md"), "\n".join(md))
    with open(os.path.join(out, "votos_por_partido_cargo.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(csv_rows)
    with open(os.path.join(out, "resumo_blocos.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cargo", "base", "direita", "centro_direita", "direita_mais_centro_direita", "demais", "total", "pct_direita", "pct_dir_cd"])
        for g, d, c, r, tn, tl, tt in resumo:
            w.writerow([g, "nominais_validos", d[0], c[0], d[0] + c[0], r[0], tn, f"{100.0*d[0]/tn:.4f}" if tn else "", f"{100.0*(d[0]+c[0])/tn:.4f}" if tn else ""])
            w.writerow([g, "total_validos", d[2], c[2], d[2] + c[2], r[2], tt, f"{100.0*d[2]/tt:.4f}" if tt else "", f"{100.0*(d[2]+c[2])/tt:.4f}" if tt else ""])
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
