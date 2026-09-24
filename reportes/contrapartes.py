"""
reportes.contrapartes
=====================

Identidad legal de cada contraparte, sin agrupar filiales bajo su matriz.

El criterio
-----------
La unidad es el IDENTIFICADOR LEGAL tal como lo informa la aseguradora:

  RUT   contraparte chilena. Un RUT es una persona juridica.
  LEI   contraparte extranjera. Un LEI (ISO 17442) es una persona juridica.

Dos identificadores distintos son dos entidades distintas, aunque el catalogo
del dashboard (config/entities.yaml) las junte. El nombre sale de la fuente
mas autoritativa disponible:

  LEI valido  -> nombre legal registrado en GLEIF (config/gleif_lei.csv)
  RUT         -> nombre del catalogo si el catalogo lo resolvio POR RUT, que
                 es exacto; si no, el nombre que informo la aseguradora
  sin RUT/LEI -> el nombre que informo la aseguradora (pasa en todos los
                 pactos: el B.7 no les pide identificador)

Alertas
-------
Cada alerta es un HECHO verificable, no una sospecha:

  - el LEI no cumple el digito verificador (ISO 7064 MOD 97-10);
  - GLEIF marca el LEI como no vigente (LAPSED, RETIRED);
  - el LEI esta registrado a un fondo o fideicomiso, no a un banco;
  - el nombre legal del LEI no comparte ninguna palabra significativa con
    el nombre que escribio la aseguradora;
  - el mismo nombre informado aparece con mas de un LEI en la base;
  - el catalogo del dashboard agrupa este identificador con otros;
  - no hay RUT ni LEI: la entidad descansa solo en el nombre escrito.

Ninguna alerta cambia a que entidad se le atribuye el monto: el monto va al
identificador informado, que es lo que la CMF recibio.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import pandas as pd

from normalize.entities import validate_rut
from utils.fetch_gleif import leer as leer_gleif, lei_valido

#: Palabras que no identifican a una entidad: forma juridica, rubro generico,
#: articulos. Se descartan antes de comparar nombres.
_VACIAS = {
    "BANCO", "BANK", "BANKING", "THE", "DE", "DEL", "LA", "EL", "OF", "AND", "Y",
    "SA", "S", "A", "NA", "N", "AG", "PLC", "LLC", "INC", "LTD", "LIMITED", "CORP",
    "CORPORATION", "COMPANY", "CO", "SAS", "NATIONAL", "ASSOCIATION", "INTERNATIONAL",
    "CHILE", "LONDON", "NEW", "YORK", "BRANCH", "SUCURSAL", "AGENCIA", "EN",
    "CAPITAL", "MARKETS", "GROUP", "HOLDINGS", "AKTIENGESELLSCHAFT", "SOCIEDAD",
    "ANONIMA", "LIMITADA",
}

#: Palabras que delatan que el LEI es de un vehiculo, no de un banco operador.
_VEHICULO = {"TRUST", "FUND", "PLAN", "BENEFIT", "EMPLOYEE", "COLLATERAL"}


def _tokens(nombre: str | None) -> set[str]:
    t = re.sub(r"[^A-Z0-9 ]", " ", str(nombre or "").upper())
    return {w for w in t.split() if len(w) > 2 and w not in _VACIAS}


def _sigla(nombre: str | None) -> str:
    """Iniciales de las palabras con contenido: BANCO BILBAO VIZCAYA ARGENTARIA -> BBVA.

    Sin esto, "BBVA ESPANA" contra "Banco Bilbao Vizcaya Argentaria" parece un
    LEI ajeno, cuando es la misma entidad escrita por su sigla.
    """
    ruido = {"DE", "DEL", "LA", "EL", "OF", "THE", "AND", "Y", "SA", "S", "A",
             "SOCIEDAD", "ANONIMA", "PLC", "LLC", "INC", "LTD", "AG", "NA", "N"}
    t = re.sub(r"[^A-Z0-9 ]", " ", str(nombre or "").upper()).split()
    return "".join(w[0] for w in t if w not in ruido)


def _coinciden(a: str | None, b: str | None) -> bool:
    """Dos nombres refieren a lo mismo si comparten una palabra con contenido,
    si una palabra de uno esta contenida en el otro (JPMORGAN / MORGAN), o si
    uno es la sigla del otro (BBVA / Banco Bilbao Vizcaya Argentaria)."""
    ta, tb = _tokens(a), _tokens(b)
    if ta & tb:
        return True
    pa, pb = "".join(sorted(ta)), "".join(sorted(tb))
    if any(len(w) >= 4 and w in pb for w in ta) or any(len(w) >= 4 and w in pa for w in tb):
        return True
    sa, sb = _sigla(a), _sigla(b)
    return (len(sa) >= 3 and sa in tb) or (len(sb) >= 3 and sb in ta)


def _norm(nombre: str | None) -> str:
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", str(nombre or "").upper()).split())


def identificar(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega la identidad legal a un DataFrame de operaciones del B.7/B.14.

    Espera las columnas contraparte_rut, contraparte_lei,
    contraparte_nombre_informado, contraparte_key, contraparte_nombre y
    resolucion_metodo. Devuelve una copia con:

        entidad_id, entidad_tipo_id, entidad_nombre, entidad_pais,
        entidad_fuente_nombre, entidad_alerta
    """
    out = df.copy()
    gleif = leer_gleif()

    rut = pd.to_numeric(out["contraparte_rut"], errors="coerce").fillna(0).astype("int64")
    dv = (out["contraparte_dv"] if "contraparte_dv" in out else pd.Series("", index=out.index)
          ).fillna("").astype(str).str.strip().str.upper()
    lei = out["contraparte_lei"].fillna("").astype(str).str.strip().str.upper()
    lei = lei.where(lei.ne("0"), "")
    informado = out["contraparte_nombre_informado"].fillna("").astype(str).str.strip()

    # --- identificador --------------------------------------------------------
    tipo, ident = [], []
    for r, d, l, n in zip(rut, dv, lei, informado):
        if r > 0 and validate_rut(r, d):
            tipo.append("RUT"); ident.append(f"RUT {r}-{d}")
        elif r > 0:
            # Un RUT que no cuadra modulo 11 no identifica a nadie: se muestra
            # aparte, con el nombre informado, en vez de atribuirlo a un banco.
            tipo.append("RUT invalido"); ident.append(f"RUT invalido {r}-{d or '?'}")
        elif l:
            if lei_valido(l):
                tipo.append("LEI"); ident.append(f"LEI {l}")
            else:
                tipo.append("LEI invalido"); ident.append(f"LEI invalido {l}")
        else:
            tipo.append("Sin RUT ni LEI"); ident.append(f"NOMBRE {_norm(n) or '(vacio)'}")
    out["entidad_tipo_id"] = tipo
    out["entidad_id"] = ident

    # Nombre informado mas frecuente por identificador: la ortografia que mas
    # aseguradoras usan, para no mostrar una variante suelta con typo.
    frec = (pd.DataFrame({"id": ident, "n": informado})
            .groupby(["id", "n"]).size().reset_index(name="k")
            .sort_values(["id", "k"], ascending=[True, False])
            .drop_duplicates("id").set_index("id")["n"])

    # --- nombre y pais ----------------------------------------------------------
    nombres, paises, fuentes = [], [], []
    for t, i, l, key, cat, met in zip(tipo, ident, lei, out["contraparte_key"],
                                      out["contraparte_nombre"], out["resolucion_metodo"]):
        if t == "LEI" and gleif.get(l, {}).get("nombre_legal"):
            g = gleif[l]
            nombres.append(g["nombre_legal"]); paises.append(g["pais"]); fuentes.append("GLEIF")
        elif t == "RUT" and met == "rut" and cat:
            nombres.append(cat); paises.append("CL"); fuentes.append("Catalogo (resuelto por RUT)")
        else:
            nombres.append(frec.get(i) or cat or i)
            paises.append("CL" if t in ("RUT", "RUT invalido") else "")
            fuentes.append("Nombre informado por la aseguradora")
    out["entidad_nombre"] = nombres
    out["entidad_pais"] = paises
    out["entidad_fuente_nombre"] = fuentes

    # --- alertas ----------------------------------------------------------------
    # Nombres informados que aparecen con mas de un LEI valido en la base.
    leis_por_nombre: dict[str, set[str]] = defaultdict(set)
    for t, l, n in zip(tipo, lei, informado):
        if t in ("LEI", "LEI invalido"):
            leis_por_nombre[_norm(n)].add(l)
    # Identificadores que el catalogo del dashboard junta bajo una misma clave.
    # Solo cuentan identificadores LEGALES: que el catalogo junte un RUT con las
    # filas de pactos que traen solo el nombre no es fusionar dos personas
    # juridicas, es que a los pactos no se les pide identificador.
    ids_por_clave: dict[str, set[str]] = defaultdict(set)
    for t, i, key in zip(tipo, ident, out["contraparte_key"]):
        if t != "Sin RUT ni LEI":
            ids_por_clave[str(key)].add(i)

    alertas = []
    for t, i, l, n, key in zip(tipo, ident, lei, informado, out["contraparte_key"]):
        a = []
        if t == "LEI invalido":
            a.append("LEI con digito verificador invalido (ISO 17442)")
        if t == "RUT invalido":
            a.append("RUT que no cuadra con su digito verificador (modulo 11)")
        if t == "LEI":
            g = gleif.get(l, {})
            estado = g.get("estado_lei", "")
            if estado and estado != "ISSUED":
                a.append(f"LEI no vigente en GLEIF ({estado})")
            nombre_leg = g.get("nombre_legal", "")
            if g.get("categoria") == "FUND" or (_tokens(nombre_leg) & _VEHICULO):
                a.append("El LEI esta registrado a un fondo o fideicomiso, no a un banco")
            if nombre_leg and n and not _coinciden(nombre_leg, n):
                a.append(f"Informado como '{n}', pero el LEI es de {nombre_leg}")
        if t in ("LEI", "LEI invalido") and len(leis_por_nombre.get(_norm(n), ())) > 1:
            a.append(f"El nombre '{n}' se informa con "
                     f"{len(leis_por_nombre[_norm(n)])} LEI distintos en la base")
        if t != "Sin RUT ni LEI" and len(ids_por_clave.get(str(key), ())) > 1:
            a.append(f"El catalogo del dashboard agrupa este identificador con "
                     f"{len(ids_por_clave[str(key)]) - 1} otro(s) bajo {key}")
        if t == "Sin RUT ni LEI":
            a.append("Sin RUT ni LEI: la entidad descansa solo en el nombre informado")
        alertas.append("; ".join(dict.fromkeys(a)))
    out["entidad_alerta"] = alertas
    # Version para las hojas de operaciones: solo lo que dice algo de la
    # operacion. Que el catalogo del dashboard agrupe identificadores es un
    # hecho sobre el dashboard, no sobre el contrato, y repetido en cada una de
    # las 773 operaciones con Santander Chile solo tapa las alertas que importan.
    out["entidad_alerta_op"] = ["; ".join(x for x in al.split("; ")
                                          if x and not x.startswith("El catalogo del dashboard"))
                                for al in alertas]
    return out


def resumen_calidad(df_ident: pd.DataFrame) -> pd.DataFrame:
    """Una fila por identificador con alertas, para la hoja de calidad."""
    con_alerta = df_ident[df_ident["entidad_alerta"] != ""]
    if con_alerta.empty:
        return pd.DataFrame(columns=["entidad_id", "entidad_tipo_id", "entidad_nombre",
                                     "nombres_informados", "catalogo_dashboard",
                                     "operaciones", "aseguradoras", "entidad_alerta"])
    g = con_alerta.groupby("entidad_id")
    return pd.DataFrame({
        "entidad_tipo_id": g["entidad_tipo_id"].first(),
        "entidad_nombre": g["entidad_nombre"].first(),
        "nombres_informados": g["contraparte_nombre_informado"].agg(
            lambda s: " | ".join(k for k, _ in Counter(s.dropna()).most_common(4))),
        "catalogo_dashboard": g["contraparte_nombre"].first(),
        "operaciones": g.size(),
        "aseguradoras": g["rut_compania"].nunique(),
        "entidad_alerta": g["entidad_alerta"].first(),
    }).reset_index().sort_values("operaciones", ascending=False)
