"""¿Entra la máquina por donde hay que pasar? Lo calcula el código, no el modelo.

Pasó en vivo con Gemini (2026-09-19), con el ancho de la minicargadora ya en
la ficha: "la minicargadora mide 1,83 m, así que entra bien en el pasillo de
1,50 m que tenés", y a otro lead "la excavadora 320 debería entrar bien por el
pasillo" — una máquina de la que la ficha ni trae el ancho. Una regla en el
prompt no lo arregló: comparar dos medidas es justo lo que un modelo de
lenguaje hace mal, igual que multiplicar horas por tarifa. Por eso el precio
sale de cotizar y esto sale de acá.

El dato de la máquina es `specs.ancho_m` (número, en metros); un implemento
más ancho que la máquina trae su propio `ancho_m` (el cucharón, el
rotocultivador). El paso lo dice el lead ("un pasillo de 1,50 m", "el portón
tiene 90 cm") y se busca en SUS mensajes; si el modelo lo manda en
`ancho_paso_m`, gana el del modelo.
"""
from __future__ import annotations

import re
from typing import Any

from app.catalog_guard import normalizar

#: Menos margen que esto no se promete: que el asesor vea el acceso.
MARGEN_M = 0.10

_LUGAR = r"(?:pasillo|porton|portoncito|entrada|acceso|puerta|portada|callejon|tranquera)"
_NUMERO = r"(\d+(?:[.,]\d+)?)"
_UNIDAD = r"(metros?|mts?|m|cm|centimetros?)?"
#: "pasillo de 1,50 m", "el portón tiene 90 cm", "entrada de 2,20": el lugar
#: primero, la medida después, sin cortar la frase ni saltar otro número.
_PASO = re.compile(rf"\b{_LUGAR}\b[^.!?\n\d]{{0,40}}?{_NUMERO}\s*{_UNIDAD}(?![a-z])")


def _a_metros(numero: str, unidad: str | None) -> float | None:
    valor = float(numero.replace(",", "."))
    if unidad and unidad.startswith("c"):
        valor = valor / 100
    elif not unidad:
        # Sin unidad solo vale con decimales ("1,50"): un entero suelto puede
        # ser una altura de calle ("pasaje San Martín 450").
        if "," not in numero and "." not in numero:
            return None
    if 0.3 <= valor <= 20:
        return round(valor, 2)
    return None


def paso_del_lead(textos: list[str]) -> float | None:
    """El ancho del paso que dijo el lead, en metros. El último que dijo gana:
    "perdón, el pasillo mide 2 metros" corrige al de antes."""
    ultimo: float | None = None
    for texto in textos or []:
        for m in _PASO.finditer(normalizar(texto or "")):
            metros = _a_metros(m.group(1), m.group(2))
            if metros is not None:
                ultimo = metros
    return ultimo


def metros_de(valor: Any) -> float | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor) if valor > 0 else None
    if isinstance(valor, str):
        try:
            n = float(valor.replace(",", ".").strip())
        except ValueError:
            return None
        return n if n > 0 else None
    return None


def en_metros(valor: float) -> str:
    return f"{valor:.2f}".replace(".", ",") + " m"


def veredicto(paso: float, necesita: float) -> str:
    if paso < necesita:
        return "no"
    if paso - necesita < MARGEN_M:
        return "justo"
    return "si"


def acceso_de(specs: dict[str, Any] | None, paso: float) -> dict[str, Any]:
    """El veredicto para UNA máquina, con sus implementos más anchos que ella."""
    specs = specs or {}
    ancho = metros_de(specs.get("ancho_m"))
    if ancho is None:
        return {
            "paso_del_lead_m": paso,
            "entra": "sin_dato",
            "detalle": (
                "la ficha no trae el ancho de esta máquina: NO digas que entra "
                "ni que no entra; ofrecé que el asesor vea el acceso."
            ),
        }
    entra = veredicto(paso, ancho)
    if entra == "no":
        detalle = (
            f"NO ENTRA: la máquina mide {en_metros(ancho)} y el paso {en_metros(paso)}. "
            "No la recomiendes para ese acceso."
        )
    elif entra == "justo":
        detalle = (
            f"ENTRA MUY JUSTO: {en_metros(ancho)} en un paso de {en_metros(paso)}. No "
            "prometas que entra: que el asesor vea el acceso."
        )
    else:
        detalle = f"ENTRA: mide {en_metros(ancho)} y el paso {en_metros(paso)}."
    resultado: dict[str, Any] = {"paso_del_lead_m": paso, "entra": entra}
    if entra != "no":
        # Con un implemento más ancho que la máquina, manda el implemento.
        no_pasan: list[str] = []
        for imp in specs.get("implementos") or []:
            if not isinstance(imp, dict):
                continue
            ancho_imp = metros_de(imp.get("ancho_m"))
            if ancho_imp is None or ancho_imp <= ancho:
                continue
            v = veredicto(paso, ancho_imp)
            if v != "si":
                nombre = imp.get("nombre") or "implemento"
                estado = "NO pasa" if v == "no" else "pasa muy justo"
                no_pasan.append(f"{nombre} ({en_metros(ancho_imp)}): {estado}")
        if no_pasan:
            resultado["implementos_que_no_pasan"] = no_pasan
            detalle += (
                " Pero con estos implementos puestos: "
                + "; ".join(no_pasan)
                + ". No los ofrezcas para entrar por ese paso sin que el asesor vea el acceso."
            )
    resultado["detalle"] = detalle
    return resultado


def ficha_para_el_paso(maquina: dict[str, Any], specs: dict[str, Any] | None, paso: float) -> dict[str, Any]:
    """Lo que el modelo ve de una máquina cuando el lead dijo por dónde entra.

    Con el veredicto adentro, Gemini igual recomendó la mini para un pasillo
    de 1,50 m ("no puedo asegurarte que entre"). Lo que no entra le llega sin
    ficha: sin specs ni implementos no tiene con qué recomendarla. Y los
    implementos que no pasan salen de la lista."""
    acceso = acceso_de(specs, paso)
    if acceso["entra"] == "no":
        return {
            "modelo_id": maquina.get("modelo_id"),
            "nombre": maquina.get("nombre"),
            "categoria": maquina.get("categoria"),
            "acceso": acceso,
        }
    out = dict(maquina)
    out["acceso"] = acceso
    specs = specs or {}
    ancho = metros_de(specs.get("ancho_m"))
    implementos = specs.get("implementos")
    if ancho is not None and isinstance(implementos, list):
        pasan = [
            imp
            for imp in implementos
            if not isinstance(imp, dict)
            or (metros_de(imp.get("ancho_m")) or 0.0) <= ancho
            or veredicto(paso, metros_de(imp.get("ancho_m")) or 0.0) == "si"
        ]
        if len(pasan) != len(implementos):
            out["specs"] = {**specs, "implementos": pasan}
    return out


# ------------------------------------------------ la guarda de la respuesta ---
#
# Con el veredicto en la mano, Gemini igual escribió "entra por tu portón de
# 1,85 m, aunque va justita" con el cucharón de 1,90 m. Igual que la reserva
# afirmada (booking_guard) o la máquina ajena (catalog_guard): la respuesta que
# promete que entra lo que el sistema dijo que no, no sale.

_PALABRA = re.compile(r"[a-z0-9]+")
_AFIRMA = re.compile(
    r"\b(entra|entran|entraria|entrarian|pasa|pasan|pasaria|pasarian|cabe|caben|"
    r"le permite pasar|va a pasar|va a entrar|puede pasar|puede entrar|"
    r"podes pasar|podes entrar|deberia (?:entrar|pasar)|tendria que (?:entrar|pasar))\b"
)
#: "no entra", "ni pasa", "no puede pasar": hasta dos palabras entre la
#: negación y el verbo. Más lejos ya es otra cosa ("no hay drama, la mini
#: entra").
_NEGADA = re.compile(r"\b(no|ni|nunca|tampoco)\b(?:\s+\S+){0,2}\s*$")
#: La duda dicha con todas las letras tampoco es promesa: "no sé si la mini
#: pasa", "no te puedo asegurar que la máquina entra".
_DUDA = re.compile(
    r"\b(no se si|no sabria|no (?:te )?(?:puedo |podria )?(?:asegurar|prometer|garantizar)|"
    r"hay que ver si|habria que ver si|a confirmar)"
)
#: Cortar en frases sin partir "1.134 kg" ni "1,85 m".
_CLAUSULA = re.compile(r"(?<!\d)[.;!?\n]|[.;!?\n](?!\d)|\bpero\b")


def _alias(nombre: str) -> set[str]:
    """Cómo se nombra una máquina o un implemento en una respuesta: la primera
    palabra ("minicargadora", "cucharon"), los códigos ("252b", "320") y las
    palabras largas ("desbrozadora")."""
    ws = _PALABRA.findall(normalizar(nombre))
    out = {w for w in ws if any(c.isdigit() for c in w) or len(w) >= 8}
    if ws:
        out.add(ws[0])
        if ws[0].startswith("minicargadora"):
            out.add("mini")
        if ws[0].startswith("retroexcavadora"):
            out.add("retro")
    return out


def entidades_de_acceso(
    modelos: dict[str, dict[str, Any]], paso: float
) -> list[tuple[set[str], bool, str]]:
    """(alias, ¿riesgosa?, detalle) de cada máquina y de cada implemento que
    no pasa. Riesgosa = no entra, entra justo o la ficha no trae el ancho."""
    out: list[tuple[set[str], bool, str]] = []
    for nombre, specs in modelos.items():
        acceso = acceso_de(specs, paso)
        out.append((_alias(nombre), acceso["entra"] != "si", f"{nombre}: {acceso['detalle']}"))
        if acceso["entra"] == "no":
            continue
        ancho = metros_de((specs or {}).get("ancho_m")) or 0.0
        for imp in (specs or {}).get("implementos") or []:
            if not isinstance(imp, dict):
                continue
            ancho_imp = metros_de(imp.get("ancho_m"))
            if ancho_imp is None or ancho_imp <= ancho or veredicto(paso, ancho_imp) == "si":
                continue
            nombre_imp = str(imp.get("nombre") or "")
            out.append(
                (
                    _alias(nombre_imp),
                    True,
                    f"{nombre_imp}: mide {en_metros(ancho_imp)}, NO pasa por un paso de {en_metros(paso)}",
                )
            )
    return out


def _afirma(clausula: str) -> bool:
    for m in _AFIRMA.finditer(clausula):
        antes = clausula[: m.start()]
        if not _NEGADA.search(antes) and not _DUDA.search(antes):
            return True
    return False


def promesas_de_acceso(
    texto: str | None, entidades: list[tuple[set[str], bool, str]]
) -> list[str]:
    """Los detalles de lo que la respuesta promete que entra y el sistema dijo
    que no (o que entra justo, o que no se sabe). Vacío = nada que frenar."""
    if not texto or not entidades:
        return []
    plano = normalizar(texto)
    nombradas_en_todo = [e for e in entidades if e[0] & set(_PALABRA.findall(plano))]
    problemas: dict[str, None] = {}
    for clausula in _CLAUSULA.split(plano):
        if not clausula or not _afirma(clausula):
            continue
        palabras = set(_PALABRA.findall(clausula))
        aca = [e for e in entidades if e[0] & palabras]
        # "Lo que le permite pasar por tu portón" no nombra a nadie: habla de
        # lo que la respuesta venía recomendando.
        for alias, riesgosa, detalle in aca or nombradas_en_todo:
            if riesgosa:
                problemas.setdefault(detalle, None)
    return list(problemas)


_BORRADOR_MAX = 600


def alerta_acceso(problemas: list[str], borrador: str, paso: float) -> str:
    citado = borrador.strip()
    if len(citado) > _BORRADOR_MAX:
        citado = citado[:_BORRADOR_MAX] + "…"
    return (
        "ALERTA DEL SISTEMA — tu borrador de respuesta NO se envió. Decía: "
        f"«{citado}». Promete que entra por el paso de {en_metros(paso)} algo "
        "que el sistema calculó distinto:\n- "
        + "\n- ".join(problemas)
        + "\nReescribí sin comparar medidas vos: si no entra, decí que no "
        "entra; si entra justo o la ficha no trae el ancho, decí que no se "
        "puede asegurar y que un asesor vea el acceso. Nunca 'entra' ni 'pasa' "
        "para eso."
    )


def pregunta_segura_acceso(paso: float) -> str:
    return (
        f"Con un paso de {en_metros(paso)} no te puedo asegurar que la máquina "
        "entre: eso lo tiene que ver un asesor en el lugar. ¿Hay otro acceso "
        "más ancho, o querés que un asesor lo evalúe?"
    )
