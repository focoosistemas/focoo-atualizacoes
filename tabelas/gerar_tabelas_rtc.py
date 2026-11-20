"""Gera tabelas/rtc_tabelas.json (Reforma Tributária) a partir do repositório oficial da Receita Federal
(ReceitaFederal/calculadora-rtc-regime-regular, licença MIT): monta o banco com os scripts publicados e extrai
o que o Emissor usa: CST, cClassTrib por tipo de nota, reduções, alíquotas de referência por vigência,
indicadores de operação e a correlação NBS x LC 116 da NFS-e.

Uso: python3 gerar_tabelas_rtc.py <pasta do repositório da Receita> <arquivo de saída>
"""
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import sys

DOCS = {2: "NF-e", 3: "NFC-e", 10: "NFS-e"}


def montar(repo):
    base = os.path.join(repo, "flyway", "sql")
    arqs = ([os.path.join(base, "criacao", "beforeMigrate.sql"), os.path.join(base, "criacao", "B0001__sistema_tributario_completo.sql")]
            + sorted(glob.glob(os.path.join(base, "manutencao", "V*.sql")), key=lambda x: int(re.search(r"V(\d+)", os.path.basename(x)).group(1)))
            + [os.path.join(base, "criacao", "afterMigrate.sql")])
    con = sqlite3.connect(":memory:")
    for a in arqs:
        with open(a, encoding="utf-8") as f:
            con.executescript("PRAGMA foreign_keys=OFF;\n" + f.read())
    if con.execute("PRAGMA foreign_key_check").fetchall():
        raise SystemExit("banco montado com chaves estrangeiras inconsistentes")
    con.row_factory = sqlite3.Row
    return con


def vig(r, p):
    return {"ini": (r[p + "_INICIO_VIGENCIA"] or "")[:10], "fim": (r[p + "_FIM_VIGENCIA"] or "")[:10] if r[p + "_FIM_VIGENCIA"] else ""}


def extrair(con):
    v = con.execute("SELECT * FROM VERSAO_BASE_DADO ORDER BY VRBD_ID DESC LIMIT 1").fetchone()
    trib = {r["TBTO_ID"]: r["TBTO_SIGLA"] for r in con.execute("SELECT * FROM TRIBUTO")}
    cst_por_id = {}
    cst = []
    for r in con.execute("SELECT * FROM SITUACAO_TRIBUTARIA ORDER BY SITR_CD"):
        cst_por_id[r["SITR_ID"]] = r["SITR_CD"]
        cst.append({"cd": r["SITR_CD"], "descricao": r["SITR_DESCRICAO"], "grupo_valores": bool(r["SITR_IND_GIBSCBS"]),
                    "reducao": bool(r["SITR_IND_GRED"]), **vig(r, "SITR")})
    docs = {}
    for r in con.execute("SELECT * FROM TIPO_DFE_CLASSIFICACAO"):
        if r["TDCL_TPDF_ID"] in DOCS and not r["TDCL_FIM_VIGENCIA"]:
            docs.setdefault(r["TDCL_CLTR_ID"], set()).add(DOCS[r["TDCL_TPDF_ID"]])
    red = {}
    for r in con.execute("SELECT * FROM PERCENTUAL_REDUCAO WHERE PERE_FIM_VIGENCIA IS NULL"):
        red.setdefault(r["PERE_CLTR_ID"], {})[trib.get(r["PERE_TBTO_ID"], str(r["PERE_TBTO_ID"]))] = r["PERE_VALOR"]
    cct, cct_por_id = [], {}
    for r in con.execute("SELECT * FROM CLASSIFICACAO_TRIBUTARIA ORDER BY CLTR_CD"):
        cct_por_id[r["CLTR_ID"]] = r["CLTR_CD"]
        if r["CLTR_ID"] not in docs:
            continue
        cct.append({"cd": r["CLTR_CD"], "descricao": r["CLTR_DESCRICAO"], "cst": cst_por_id.get(r["CLTR_SITR_ID"], ""),
                    "tipo_aliquota": r["CLTR_TIPO_ALIQUOTA"] or "", "nomenclatura": r["CLTR_NOMENCLATURA"] or "",
                    "reducao": red.get(r["CLTR_ID"], {}), "docs": sorted(docs[r["CLTR_ID"]]), **vig(r, "CLTR")})
    aliq = [{"tributo": trib.get(r["ALRE_TBTO_ID"], ""), "valor": r["ALRE_VALOR"], **vig(r, "ALRE")}
            for r in con.execute("SELECT * FROM ALIQUOTA_REFERENCIA ORDER BY ALRE_TBTO_ID, ALRE_INICIO_VIGENCIA")]
    indop_por_id, indop = {}, []
    for r in con.execute("SELECT * FROM INDICADOR_OPERACAO_IBS_CBS ORDER BY IOIC_CD"):
        indop_por_id[r["IOIC_ID"]] = r["IOIC_CD"]
        indop.append({"cd": r["IOIC_CD"], "local": r["IOIC_LOCAL_INCIDENCIA"] or "", "tipo": r["IOIC_TIPO_OPERACAO"] or "", **vig(r, "IOIC")})
    nbs_lc, usados = [], set()
    for r in con.execute("SELECT * FROM CLASSIF_NBS_INDOP_LC ORDER BY CNIL_LSLC_CD, CNIL_NBS_CD"):
        nbs_lc.append({"lc": r["CNIL_LSLC_CD"], "nbs": r["CNIL_NBS_CD"], "indop": indop_por_id.get(r["CNIL_IOIC_ID"], ""),
                       "cclasstrib": cct_por_id.get(r["CNIL_CLTR_ID"], ""), "onerosa": bool(r["CNIL_IN_PS_ONEROSA"]),
                       "exterior": bool(r["CNIL_IN_ADQ_EXTERIOR"]), **vig(r, "CNIL")})
        usados.add(r["CNIL_NBS_CD"])
    nbs = {r["NBS_CD"]: r["NBS_DESCRICAO"] for r in con.execute("SELECT NBS_CD, NBS_DESCRICAO FROM NBS") if r["NBS_CD"] in usados}
    return {"versao": v["VRBD_VERSAO_BASE_DADO"].upper(), "versao_num": int(v["VRBD_ID"]), "data": (v["VRBD_DATA"] or "")[:10],
            "descricao": v["VRBD_DESCRICAO"], "gerado_em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "fonte": "Receita Federal do Brasil, calculadora-rtc-regime-regular (licença MIT)",
            "cst": cst, "cclasstrib": cct, "aliquotas": aliq, "indop": indop, "nbs_lc": nbs_lc, "nbs": nbs}


def conferir(t):
    """Barreira contra tabela quebrada: se algo parecer errado, não publica."""
    assert t["versao_num"] > 0 and len(t["cst"]) >= 10 and len(t["cclasstrib"]) >= 50, "tabelas incompletas"
    assert any(c["cd"] == "000001" for c in t["cclasstrib"]), "sem cClassTrib 000001"
    for a in t["aliquotas"]:
        assert 0 <= float(a["valor"]) <= 40, f"alíquota fora do esperado: {a}"
    assert {"CBS", "IBSUF", "IBSMun"} <= {a["tributo"] for a in t["aliquotas"]}, "faltam alíquotas de referência"


if __name__ == "__main__":
    repo, saida = sys.argv[1], sys.argv[2]
    t = extrair(montar(repo))
    conferir(t)
    with open(saida, "w", encoding="utf-8") as f:
        json.dump(t, f, ensure_ascii=False, separators=(",", ":"))
    print(t["versao"], t["data"], len(t["cclasstrib"]), "cClassTrib", len(t["nbs_lc"]), "NBS x LC", os.path.getsize(saida) // 1024, "KB")
