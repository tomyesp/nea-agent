""""Esa máquina no está disponible" se dice SOLO si el sistema lo dijo.

Pasó con un lead real (2026-09-19, charla de Pedrito): el agente le venía
ofreciendo una "Caterpillar 320L" —nombre torcido de la Excavadora 320 DL—,
pidió disponibilidad con el modelo equivocado, el CRM le contestó con una
Cargadora 938H libre, y en vez de darse cuenta del cambiazo le escribió al
lead "la Caterpillar 320L no la tengo disponible para ese día" y le cotizó la
cargadora. Estaban TODAS libres.

Una falta de stock inventada es cara de las dos maneras: el lead se va
creyendo que no tenemos la máquina, o acepta otra que no le sirve para su
obra (una cargadora no demuele).

La guarda solo opina cuando en ESTE turno hubo al menos una consulta de
disponibilidad: si el agente contesta de memoria de turnos anteriores, no hay
con qué compararlo y no se frena nada.
"""
from __future__ import annotations

import re
from typing import Any

from app.catalog_guard import alias_de, normalizar

_PALABRA = re.compile(r"[a-z0-9]+")
#: Cortar en frases sin partir "1.134 kg".
_CLAUSULA = re.compile(r"(?<!\d)[.;!?\n]|[.;!?\n](?!\d)|\bpero\b")
#: "no está disponible", "no la tengo libre", "está tomada", "no hay
#: disponibilidad", "no me queda ninguna".
_FALTA = re.compile(
    r"\b(?:"
    r"no\b(?:\s+\S+){0,3}\s+(?:disponible|disponibles|libre|libres|queda|quedan)|"
    r"(?:esta|estan|quedo|quedaron)\s+(?:tomada|tomadas|tomado|ocupada|ocupadas|"
    r"reservada|reservadas|alquilada|alquiladas|comprometida)|"
    r"sin\s+disponibilidad|no\s+hay\s+disponibilidad|no\s+tenemos\s+disponibilidad"
    r")\b"
)


def afirma_falta_de_stock(texto: str | None) -> bool:
    return bool(texto) and bool(_FALTA.search(normalizar(texto)))


def stock_inventado(
    texto: str | None, consultadas: dict[str, bool]
) -> list[str]:
    """Las máquinas a las que la respuesta les inventa una falta de stock.

    `consultadas` es {nombre del catálogo: ¿está libre?} de lo que se consultó
    en este turno. Se frena lo que el sistema dijo que SÍ está libre, y lo que
    nunca se consultó.
    """
    if not texto or not consultadas:
        return []
    plano = normalizar(texto)
    entidades = [(alias_de(nombre), nombre) for nombre in consultadas]
    problemas: dict[str, None] = {}
    for clausula in _CLAUSULA.split(plano):
        if not clausula or not _FALTA.search(clausula):
            continue
        palabras = set(_PALABRA.findall(clausula))
        nombradas = [nombre for alias, nombre in entidades if alias & palabras]
        if nombradas:
            # Nombra una que SÍ consultaste: solo vale si dio ocupada.
            for nombre in nombradas:
                if consultadas.get(nombre):
                    problemas.setdefault(
                        f"{nombre}: el sistema dijo que SÍ está libre en esas fechas", None
                    )
        else:
            # Habla de otra máquina (o de ninguna): no hay consulta que lo
            # respalde. Es el caso de la charla de Pedrito.
            problemas.setdefault(
                "esa máquina no la consultaste en este turno: lo único que "
                "consultaste fue "
                + ", ".join(
                    f"{n} ({'libre' if libre else 'ocupada'})" for n, libre in consultadas.items()
                ),
                None,
            )
    return list(problemas)


_BORRADOR_MAX = 600


def alerta_stock(problemas: list[str], borrador: str) -> str:
    citado = borrador.strip()
    if len(citado) > _BORRADOR_MAX:
        citado = citado[:_BORRADOR_MAX] + "…"
    return (
        "ALERTA DEL SISTEMA — tu borrador de respuesta NO se envió. Decía: "
        f"«{citado}». Dice que una máquina no está disponible, y eso no es lo "
        "que contestó el sistema:\n- "
        + "\n- ".join(problemas)
        + "\nLa disponibilidad la sabe SOLO consultar_disponibilidad, y para "
        "la máquina que el lead quiere: si te devolvió otra, es que pediste el "
        "modelo_id equivocado — buscalo en el catálogo y volvé a consultar en "
        "este mismo turno. Nunca le digas a un lead que no tenemos una máquina "
        "libre sin haberlo chequeado: se va creyendo que no la tenemos."
    )


PREGUNTA_SEGURA = (
    "Perdoname, me confundí de máquina al chequear. Dejame verificar bien la "
    "disponibilidad y te confirmo. ¿Me repetís para qué fechas la necesitás?"
)
