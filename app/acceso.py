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
