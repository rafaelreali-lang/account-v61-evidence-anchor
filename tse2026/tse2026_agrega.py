#!/usr/bin/env python3
"""Agrega votos do 1º turno de 2026 por partido e por cargo a partir dos
arquivos oficiais do TSE (Portal de Dados Abertos, "Resultados 2026"):

  votacao_partido_munzona_2026.zip   -> nominais + legenda por partido
  votacao_candidato_munzona_2026.zip -> nominais por candidato (somados pelo partido)

Uso:
  python3 tse2026_agrega.py ARQ [ARQ ...] --out DIR

ARQ pode ser .zip, .csv ou diretório. Dentro de um .zip, se existir
*_BRASIL.csv ele é usado; senão todos os CSVs de UF são lidos.
Somente biblioteca padrão. Lê em streaming (arquivos de GB).
"""
import argparse
import csv
import io
import os
import sys
import unicodedata
import zipfile
from collections import defaultdict

DIREITA = ["PL", "NOVO", "REPUBLICANOS", "PP", "UNIAO", "PRD", "PRTB", "DC", "MISSAO"]
CENTRO_DIREITA = ["PSD", "MDB", "PSDB", "CIDADANIA", "PODE", "SOLIDARIEDADE",
                  "AVANTE", "AGIR", "MOBILIZA"]

# variantes de sigla -> sigla canônica (sem acento, maiúscula)
ALIAS = {
    "PODEMOS": "PODE",
    "PROGRESSISTAS": "PP",
    "UNIAO BRASIL": "UNIAO",
    "SD": "SOLIDARIEDADE",
    "PMN": "MOBILIZA",
    "PTC": "AGIR",
    "PATRIOTA": "PRD",
    "PTB": "PRD",
}

CARGOS = {
    "GOVERNADOR": "Governador",
    "SENADOR": "Senador",
    "DEPUTADO FEDERAL": "Dep. Federal",
    "DEPUTADO ESTADUAL": "Dep. Estadual/Distrital",
    "DEPUTADO DISTRITAL": "Dep. Estadual/Distrital",
}
ORDEM_CARGOS = ["Dep. Estadual/Distrital", "Dep. Federal", "Senador", "Governador"]


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.upper().split())


def norm_partido(sg):
    n = norm(sg)
    return ALIAS.get(n, n)


def to_int(v):
    v = (v or "").strip()
    if v in ("", "#NULO#", "#NE#", "-1"):
        return 0
    return int(float(v))


def iter_csv_sources(paths):
    """Gera (nome, fileobj texto latin-1) para cada CSV encontrado."""
    for p in paths:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                fp = os.path.join(p, f)
                if f.lower().endswith(".csv"):
                    yield fp, open(fp, encoding="latin-1", newline="")
                elif f.lower().endswith(".zip"):
                    yield from iter_csv_sources([fp])
        elif p.lower().endswith(".zip"):
            zf = zipfile.ZipFile(p)
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            brasil = [n for n in names if n.upper().endswith("_BRASIL.CSV")]
            for n in (brasil or names):
                yield f"{p}::{n}", io.TextIOWrapper(zf.open(n), encoding="latin-1", newline="")
        else:
            yield p, open(p, encoding="latin-1", newline="")


class Agregado:
    def __init__(self):
        # [tipo][cargo][partido] -> {"nominal":..,"legenda":..}
        self.v = {"partido": defaultdict(lambda: defaultdict(lambda: [0, 0])),
                  "candidato": defaultdict(lambda: defaultdict(lambda: [0, 0]))}
        self.rows = defaultdict(int)
        self.turnos = defaultdict(int)
        self.cargos_vistos = defaultdict(int)
        self.geracao = set()
        self.ufs = defaultdict(set)
        self.fontes = []


def processar(fonte_nome, fobj, ag):
    rd = csv.reader(fobj, delimiter=";", quotechar='"')
    header = next(rd)
    idx = {h.strip().upper(): i for i, h in enumerate(header)}
    need = ["NR_TURNO", "DS_CARGO", "SG_PARTIDO"]
    for n in need:
        if n not in idx:
            raise SystemExit(f"{fonte_nome}: coluna {n} ausente. Cabeçalho: {header[:12]}...")
    if "QT_VOTOS_LEGENDA_VALIDOS" in idx:
        tipo = "partido"
    elif "SQ_CANDIDATO" in idx or "NM_CANDIDATO" in idx:
        tipo = "candidato"
    else:
        raise SystemExit(f"{fonte_nome}: não reconhecido como votacao_partido nem votacao_candidato")
    i_turno, i_cargo, i_part = idx["NR_TURNO"], idx["DS_CARGO"], idx["SG_PARTIDO"]
    i_nom = idx.get("QT_VOTOS_NOMINAIS_VALIDOS", idx.get("QT_VOTOS_NOMINAIS"))
    i_leg = idx.get("QT_VOTOS_LEGENDA_VALIDOS")
    i_uf = idx.get("SG_UF")
    i_dt, i_hh = idx.get("DT_GERACAO"), idx.get("HH_GERACAO")
    if i_nom is None:
        raise SystemExit(f"{fonte_nome}: sem coluna de votos nominais")
    ag.fontes.append((fonte_nome, tipo))
    n = 0
    for row in rd:
        if not row or len(row) <= i_part:
            continue
        n += 1
        turno = row[i_turno].strip()
        ag.turnos[turno] += 1
        if turno != "1":
            continue
        cargo_raw = norm(row[i_cargo])
        ag.cargos_vistos[cargo_raw] += 1
        cargo = CARGOS.get(cargo_raw)
        if cargo is None:
            continue
        part = norm_partido(row[i_part])
        cel = ag.v[tipo][cargo][part]
        cel[0] += to_int(row[i_nom])
        if i_leg is not None:
            cel[1] += to_int(row[i_leg])
        if i_uf is not None:
            ag.ufs[cargo].add(row[i_uf].strip())
        if i_dt is not None and n <= 5:
            ag.geracao.add(f"{row[i_dt]} {row[i_hh] if i_hh is not None else ''}".strip())
    ag.rows[fonte_nome] = n


def fmt(n):
    return f"{n:,}".replace(",", ".")


def pct(a, b):
    return f"{(100.0 * a / b):.2f}%".replace(".", ",") if b else "n/d"


def bloco(p):
    if p in DIREITA:
        return "Direita"
    if p in CENTRO_DIREITA:
        return "Centro-direita"
    return "Demais"


def gerar(ag, out):
    os.makedirs(out, exist_ok=True)
    # fonte principal: partido (nominal+legenda) se existir, senão candidato (nominal)
    if ag.v["partido"]:
        principal, base = "partido", "nominais válidos + legenda válidos"
    elif ag.v["candidato"]:
        principal, base = "candidato", "nominais válidos (soma dos candidatos por partido)"
    else:
        raise SystemExit("Nenhum dado do 1º turno nos cargos-alvo foi encontrado.")
    dados = ag.v[principal]

    md = []
    md.append("# TSE – Eleições 2026, 1º turno – votos por partido e por cargo\n")
    md.append("Fonte: Tribunal Superior Eleitoral, Portal de Dados Abertos, Resultados 2026.")
    for f, t in ag.fontes:
        md.append(f"- Arquivo: `{f}` (tipo: votacao_{t}_munzona, {fmt(ag.rows[f])} linhas)")
    if ag.geracao:
        md.append(f"- DT_GERACAO/HH_GERACAO do arquivo: {', '.join(sorted(ag.geracao))}")
    md.append(f"- Base de votos: {base}. Apenas NR_TURNO = 1.")
    md.append("- Senador 2026: renovação de 2/3, cada eleitor vota em 2 candidatos; "
              "os votos de Senador somam ~2x o número de eleitores que votaram.")
    md.append("- Dep. Distrital (DF) agregado junto com Dep. Estadual.\n")
    md.append(f"Direita: {', '.join(DIREITA)}  ")
    md.append(f"Centro-direita: {', '.join(CENTRO_DIREITA)}\n")

    csv_rows = [["cargo", "partido", "bloco", "votos_nominais", "votos_legenda", "votos_total", "pct_validos_cargo"]]
    resumo = []
    nao_mapeados = set()
    for cargo in ORDEM_CARGOS:
        if cargo not in dados:
            continue
        tab = {p: (c[0], c[1], c[0] + c[1]) for p, c in dados[cargo].items()}
        total = sum(t[2] for t in tab.values())
        md.append(f"## {cargo}\n")
        md.append(f"UFs presentes: {len(ag.ufs.get(cargo, []))}. Total de votos válidos ({base}): **{fmt(total)}**\n")
        md.append("| Partido | Bloco | Nominais | Legenda | Total | % válidos |")
        md.append("|---|---|---:|---:|---:|---:|")
        for p, (nom, leg, tot) in sorted(tab.items(), key=lambda kv: -kv[1][2]):
            b = bloco(p)
            if b == "Demais":
                nao_mapeados.add(p)
            md.append(f"| {p} | {b} | {fmt(nom)} | {fmt(leg)} | {fmt(tot)} | {pct(tot, total)} |")
            csv_rows.append([cargo, p, b, nom, leg, tot, f"{100.0*tot/total:.4f}" if total else ""])
        d = sum(t[2] for p, t in tab.items() if bloco(p) == "Direita")
        cd = sum(t[2] for p, t in tab.items() if bloco(p) == "Centro-direita")
        rest = total - d - cd
        md.append(f"| **Subtotal Direita** | | | | **{fmt(d)}** | **{pct(d, total)}** |")
        md.append(f"| **Subtotal Centro-direita** | | | | **{fmt(cd)}** | **{pct(cd, total)}** |")
        md.append(f"| **Direita + Centro-direita** | | | | **{fmt(d+cd)}** | **{pct(d+cd, total)}** |")
        md.append(f"| Demais partidos | | | | {fmt(rest)} | {pct(rest, total)} |")
        md.append(f"| Total válidos | | | | {fmt(total)} | 100,00% |\n")
        resumo.append((cargo, d, cd, rest, total))

    md.append("## Resumo por bloco\n")
    md.append("| Cargo | Direita | Centro-direita | Direita + Centro-direita | Demais | Total válidos | % Dir+CD |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    for cargo, d, cd, rest, total in resumo:
        md.append(f"| {cargo} | {fmt(d)} | {fmt(cd)} | {fmt(d+cd)} | {fmt(rest)} | {fmt(total)} | {pct(d+cd, total)} |")
    md.append("")

    md.append("## Siglas fora das listas Direita / Centro-direita (contadas em Demais)\n")
    md.append(", ".join(sorted(nao_mapeados)) or "(nenhuma)")
    md.append("")

    md.append("## Checagens de consistência\n")
    md.append(f"- NR_TURNO encontrados (linhas): {dict(ag.turnos)}")
    md.append(f"- DS_CARGO encontrados no 1º turno (linhas): {dict(ag.cargos_vistos)}")
    md.append("- Soma dos partidos de cada cargo = total do cargo: verdadeiro por construção "
              "(o total é a soma de todas as linhas filtradas; não há linhas descartadas por partido).")
    if ag.v["partido"] and ag.v["candidato"]:
        md.append("- Cruzamento nominais: arquivo partido x arquivo candidato, por cargo e partido:")
        difs = 0
        for cargo in ORDEM_CARGOS:
            a, b = ag.v["partido"].get(cargo, {}), ag.v["candidato"].get(cargo, {})
            for p in sorted(set(a) | set(b)):
                na, nb = a.get(p, [0, 0])[0], b.get(p, [0, 0])[0]
                if na != nb:
                    difs += 1
                    md.append(f"  - DIVERGÊNCIA {cargo} / {p}: partido={fmt(na)} candidato={fmt(nb)}")
        md.append(f"  - divergências: {difs}")
    else:
        md.append("- Cruzamento partido x candidato: não feito (só um tipo de arquivo foi fornecido).")
    md.append("- Totais por cargo devem ser conferidos contra o painel do TSE "
              "(resultados.tse.jus.br, 'votos válidos' por cargo/UF); não feito automaticamente.\n")

    with open(os.path.join(out, "resultado.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    with open(os.path.join(out, "votos_por_partido_cargo.csv"), "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(csv_rows)
    with open(os.path.join(out, "resumo_blocos.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cargo", "direita", "centro_direita", "direita_mais_centro_direita", "demais", "total_validos", "pct_dir_cd"])
        for cargo, d, cd, rest, total in resumo:
            w.writerow([cargo, d, cd, d + cd, rest, total, f"{100.0*(d+cd)/total:.4f}" if total else ""])
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arquivos", nargs="+")
    ap.add_argument("--out", default="saida")
    a = ap.parse_args()
    ag = Agregado()
    for nome, fobj in iter_csv_sources(a.arquivos):
        with fobj:
            print(f"lendo {nome} ...", file=sys.stderr)
            processar(nome, fobj, ag)
    print(gerar(ag, a.out))


if __name__ == "__main__":
    main()
