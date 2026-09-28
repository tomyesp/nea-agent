"""017 — ¿La máquina que el lead quiere trasladar va en el carretón?

RPM traslada máquinas propias y del cliente con camión tractor y carretón
(decisión del dueño, 2026-09-28). La ficha del carretón dice qué lleva, qué
confirma el asesor y qué no entra, pero Gemini no compara números: con una
motoniveladora de 15 t leyó "15 a 22 t" y contestó "por el peso no habría
problema", cuando de 15 t para arriba lo confirma el asesor. Es la misma
lección que el ancho del pasillo (app/acceso.py): el veredicto lo calcula el
código y le llega al modelo ya hecho.

Los límites son del carretón mediano Marcellini, el único que hay: menos de
15 t va; de 15 a 22 t, o motoniveladoras, cargadoras y topadoras (largo y
ancho), lo confirma el asesor; excavadoras de 20 t o más y cualquier cosa de
más de 22 t no entra.
"""

from __future__ import annotations

import re

from app.catalog_guard import normalizar

SIN_DUDAS_T = 15.0
MAXIMO_T = 22.0
EXCAVADORA_GRANDE_T = 20.0

#: "8 toneladas", "22 t", "15tn", "7,5 ton".
_PESO = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:toneladas?|tons?|tn|t)\b")
#: Hablar de mover una máquina, no de alquilarla para trabajar.
_TRASLADO = re.compile(
    r"\b(?:traslad\w*|llev\w*|mover|muevan|transport\w*|flete|carreton)\b"
)
#: Largas o anchas para la plataforma de 6,98 m × 2,53 m: el asesor mira.
_LARGAS = re.compile(r"\b(?:motoniveladora|cargadora|pala cargadora|topadora|bulldozer|dozer)s?\b")
#: "excavadora" sola: "retroexcavadora" no tiene borde de palabra antes.
_EXCAVADORA = re.compile(r"\bexcavadoras?\b")


def peso_del_lead(textos: list[str]) -> float | None:
    """El último peso en toneladas que dijo el lead."""
    peso = None
    for t in textos or []:
        for numero in _PESO.findall(normalizar(t or "")):
            valor = float(numero.replace(",", "."))
            if 0.3 <= valor <= 200:
                peso = valor
    return peso


def veredicto_carreton(textos: list[str]) -> str | None:
    """Qué decirle al lead sobre SU máquina y el carretón, o None si no está
    hablando de trasladar nada o no dio con qué decidir."""
    plano = normalizar(" ".join(textos or []))
    if not _TRASLADO.search(plano):
        return None
    peso = peso_del_lead(textos)
    de = f" de {peso:g} t" if peso is not None else ""
    if _EXCAVADORA.search(plano) and (peso is None or peso >= EXCAVADORA_GRANDE_T):
        return (
            f"NO ENTRA: una excavadora{de} supera al carretón (las de 20 t o más "
            "no van). Decíselo derecho y que lo vea un asesor."
        )
    if peso is not None and peso > MAXIMO_T:
        return (
            f"NO ENTRA: una máquina{de} supera al carretón (máximo estimado 22 t). "
            "Decíselo derecho y que lo vea un asesor."
        )
    largas = _LARGAS.search(plano)
    if largas or (peso is not None and peso >= SIN_DUDAS_T):
        motivo = (
            f"una {largas.group(0)} es larga o ancha para la plataforma"
            if largas
            else f"con {peso:g} t pasa las 15 t que el carretón lleva sin dudas"
        )
        return (
            f"A CONFIRMAR: {motivo}. NO digas que entra ni que no entra, ni que "
            "'por el peso no habría problema': decí que el asesor lo confirma."
        )
    if peso is not None:
        return f"ENTRA: una máquina{de} va en el carretón sin problema (menos de 15 t)."
    return None
