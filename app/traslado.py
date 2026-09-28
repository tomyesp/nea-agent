"""017 — ¿En qué carretón va la máquina que el lead quiere trasladar?

RPM traslada máquinas propias y del cliente con camión tractor y carretón
(decisión del dueño, 2026-09-28). Las fichas de los carretones dicen qué lleva
cada uno, pero Gemini no compara números: con una motoniveladora de 15 t leyó
"15 a 22 t" y contestó "por el peso no habría problema", cuando de 15 t para
arriba el mediano lo confirma el asesor. Es la misma lección que el ancho del
pasillo (app/acceso.py): el veredicto lo calcula el código y le llega al
modelo ya hecho.

Los límites NO viven acá: son datos de cada carretón en el CRM
(`specs.implementos` de los tractores, con `es_acoplado`):
- `carga_max_t`: lo que lleva según la placa o la ficha.
- `carga_sin_dudas_t` (opcional): hasta acá va seguro; entre esto y el
  máximo, lo confirma el asesor.
- `tipos_lleva` / `tipos_a_confirmar` / `tipos_no`: por tipo de máquina, para
  lo que el peso solo no dice (el largo de una retro en la plataforma de 4 m
  del chico, la hoja de 3,26 m de una topadora).
- `lleva_maquinas: false`: el semirremolque de carga general no tiene rampas.
"""

from __future__ import annotations

import re
from typing import Any

from app.catalog_guard import normalizar

#: "8 toneladas", "22 t", "15tn", "7,5 ton".
_PESO = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:toneladas?|tons?|tn|t)\b")
#: Hablar de mover una máquina, no de alquilarla para trabajar.
_TRASLADO = re.compile(
    r"\b(?:traslad\w*|llev\w*|mover|muevan|transport\w*|flete|carreton)\b"
)
#: Cómo le dice el lead a su máquina → el tipo que usan las fichas. El orden
#: importa: "minicargadora" antes que "cargadora", "retro" antes que
#: "excavadora" (igual "retroexcavadora" no tiene borde de palabra antes).
_TIPOS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("minicargadora", re.compile(r"\b(?:minicargadora|bobcat|minipala|skid)")),
    ("retroexcavadora", re.compile(r"\b(?:retroexcavadora|retropala|retro)\b")),
    ("excavadora", re.compile(r"\b(?:excavadora|giratoria)s?\b")),
    ("motoniveladora", re.compile(r"\b(?:motoniveladora|niveladora)s?\b")),
    ("cargadora", re.compile(r"\b(?:cargadora|pala cargadora|pala frontal|payloader)s?\b")),
    ("topadora", re.compile(r"\b(?:topadora|bulldozer|dozer)s?\b")),
    ("compactadora", re.compile(r"\b(?:compactadora|compactador|aplanadora|vibro)s?\b")),
    ("rodillo", re.compile(r"\brodillos?\b")),
    ("tractor agricola", re.compile(r"\btractor (?:agricola|de campo|chico)\b")),
)


def peso_del_lead(textos: list[str]) -> float | None:
    """El último peso en toneladas que dijo el lead."""
    peso = None
    for t in textos or []:
        for numero in _PESO.findall(normalizar(t or "")):
            valor = float(numero.replace(",", "."))
            if 0.3 <= valor <= 200:
                peso = valor
    return peso


def tipo_del_lead(textos: list[str]) -> str | None:
    """El último tipo de máquina que nombró el lead."""
    tipo, donde = None, (-1, -1)
    for i, t in enumerate(textos or []):
        plano = normalizar(t or "")
        for nombre, patron in _TIPOS:
            for m in patron.finditer(plano):
                if (i, m.start()) > donde:
                    tipo, donde = nombre, (i, m.start())
    return tipo


def _para(carreton: dict[str, Any], peso: float | None, tipo: str | None) -> str | None:
    """'si', 'confirmar', 'no', o None si no hay con qué decidir."""
    if carreton.get("lleva_maquinas") is False:
        return None
    maximo = carreton.get("carga_max_t")
    sin_dudas = carreton.get("carga_sin_dudas_t") or maximo
    if tipo and tipo in (carreton.get("tipos_no") or []):
        return "no"
    if peso is not None and maximo is not None:
        if peso > maximo:
            return "no"
        if tipo and tipo in (carreton.get("tipos_a_confirmar") or []):
            return "confirmar"
        return "confirmar" if peso > sin_dudas else "si"
    if tipo:
        if tipo in (carreton.get("tipos_a_confirmar") or []):
            return "confirmar"
        if tipo in (carreton.get("tipos_lleva") or []):
            return "si"
    return None


def _limite(carreton: dict[str, Any]) -> str:
    sin_dudas = carreton.get("carga_sin_dudas_t")
    maximo = carreton.get("carga_max_t")
    if sin_dudas:
        return f"hasta {sin_dudas:g} t sin dudas"
    return f"hasta {maximo:g} t" if maximo else ""


def veredicto_carreton(textos: list[str], carretones: list[dict[str, Any]]) -> str | None:
    """Qué decirle al lead sobre SU máquina y nuestros carretones, o None si
    no está hablando de trasladar nada o no dio con qué decidir."""
    plano = normalizar(" ".join(textos or []))
    if not _TRASLADO.search(plano):
        return None
    peso = peso_del_lead(textos)
    tipo = tipo_del_lead(textos)
    if peso is None and tipo is None:
        return None
    con_rampas = [c for c in carretones or [] if isinstance(c, dict) and c.get("lleva_maquinas") is not False]
    juicio = [(c, _para(c, peso, tipo)) for c in con_rampas]
    si = [c for c, j in juicio if j == "si"]
    confirmar = [c for c, j in juicio if j == "confirmar"]
    if not any(j is not None for _, j in juicio):
        return None
    que = " ".join(x for x in (f"una {tipo}" if tipo else "una máquina", f"de {peso:g} t".replace(".", ",") if peso is not None else "") if x)

    def lista(cs: list[dict[str, Any]]) -> str:
        return ", ".join(f"{c.get('nombre')} ({_limite(c)})" for c in cs)

    if si:
        texto = f"ENTRA: {que} va en {lista(si)}."
        if peso is None:
            texto += " Sin el peso no sé en cuál: pedíselo."
        if confirmar:
            texto += f" En {lista(confirmar)} lo confirma el asesor."
        return texto
    if confirmar:
        return (
            f"A CONFIRMAR: {que} podría ir en {lista(confirmar)}. NO digas que "
            "entra ni que no entra, ni que 'por el peso no habría problema': "
            "decí que el asesor lo confirma."
        )
    return (
        f"NO ENTRA: {que} no va en ninguno de nuestros carretones. Decíselo "
        "derecho y que lo vea un asesor."
    )


def carretones_de(specs: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Los acoplados (carretones y semirremolques) de la ficha de un tractor."""
    return [
        i
        for i in (specs or {}).get("implementos") or []
        if isinstance(i, dict) and i.get("es_acoplado")
    ]
