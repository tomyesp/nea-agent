"""Una máquina que el negocio NO tiene no se le nombra al lead.

Pasó en las pruebas del dueño (2026-09-16): pidiendo asesoramiento para un
pozo, el agente recomendó "una minicargadora Bobcat" que "excava hasta 2,75 m",
y más tarde una "Bobcat S450" y una "motoniveladora New Holland RG140" con sus
medidas. Ninguna de las tres existe: RPM tiene minicargadoras Cat y una
Motoniveladora 140H. El modelo no las buscó en el catálogo — las recordó de su
entrenamiento, que está lleno de máquinas de otras marcas.

Es la mentira más cara de este negocio después de una reserva falsa: el lead
llama al asesor pidiendo una máquina que nadie tiene. El prompt ya lo prohíbe;
esto no depende de que lo cumpla, igual que el Markdown (app/format.py), el
saludo repetido (app/greeting.py) o la reserva afirmada (app/booking_guard.py).

La fuente de verdad es el catálogo del CRM: todo lo que no salga de ahí y
tenga pinta de marca o de modelo, no sale.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any, Iterable

logger = logging.getLogger("nea.catalog_guard")

#: El catálogo cambia poco; preguntarlo en cada turno sería un viaje de red al
#: pedo. Diez minutos alcanza para que un alta nueva entre sola.
CACHE_TTL = 600.0
_cache: dict[str, Any] = {"permitido": None, "at": 0.0}

#: Marcas de maquinaria que el modelo conoce de memoria. Si alguna NO está en
#: el catálogo de este negocio y aparece en la respuesta, es invención.
MARCAS = (
    "bobcat", "new holland", "john deere", "deere", "jcb", "komatsu",
    "hitachi", "liebherr", "doosan", "kubota", "hyundai", "sany", "xcmg",
    "yanmar", "takeuchi", "bomag", "dynapac", "wacker", "neuson", "zoomlion",
    "lonking", "sdlg", "terex", "manitou", "jlg", "haulotte", "ammann",
    "wirtgen", "putzmeister", "case", "fiat allis", "massey ferguson",
    "zanello", "pauny", "deutz", "bell", "michigan",
)

_PALABRA = re.compile(r"[a-z0-9]+")
#: "800m3", "20hs", "3tn": medidas, no modelos.
_UNIDAD = re.compile(r"^\d+(m2|m3|mm|cm|km|kg|tn|ton|hs|hp|kw|kva|lts?|min|seg|t|h|m|l)$")
#: "20x40", "3x2": medidas de terreno.
_MEDIDA = re.compile(r"^\d+x\d+$")
#: Frases que NIEGAN tener la máquina. Nombrar una marca para decir que no la
#: tenemos es exactamente lo que el prompt pide hacer.
_NIEGA = re.compile(
    r"\bno\b[^.!?\n]{0,60}\b(tenemos|tengo|manejamos|trabajamos|alquilamos|"
    r"contamos|hay|existe|es nuestra|son nuestras|entra en la flota|"
    r"esta en la flota|tenemos en la flota)\b"
)
_FRASES = re.compile(r"[^.!?\n]+")


def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar como se comparan los nombres."""
    plano = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in plano if unicodedata.category(c) != "Mn")


def tokens_del_catalogo(modelos: Iterable[dict[str, Any]]) -> set[str]:
    """Todas las palabras que el negocio SÍ puede nombrar: nombres de modelo,
    marcas y categorías del catálogo."""
    permitido: set[str] = set()
    for m in modelos or []:
        for campo in ("nombre", "marca", "categoria", "descripcion"):
            valor = m.get(campo)
            if isinstance(valor, str):
                permitido.update(_PALABRA.findall(normalizar(valor)))
    return permitido


def _sospechoso(token: str) -> bool:
    """¿Parece un código de modelo (letras + números) y no una medida?"""
    if len(token) < 3 or _UNIDAD.match(token) or _MEDIDA.match(token):
        return False
    letras = sum(c.isalpha() for c in token)
    digitos = sum(c.isdigit() for c in token)
    return letras >= 1 and digitos >= 2


def menciones_ajenas(texto: str | None, permitido: set[str]) -> list[str]:
    """Marcas y modelos nombrados en la respuesta que NO están en el catálogo.

    No cuenta nombrar una marca para decir que no la tenemos: "Bobcat no
    tenemos, lo nuestro son las Cat" es la respuesta correcta.
    """
    if not texto or not permitido:
        return []
    ajenas: list[str] = []
    for frase in _FRASES.findall(texto):
        plana = normalizar(frase)
        if _NIEGA.search(plana):
            continue
        for marca in MARCAS:
            if marca in permitido or " " in marca and all(
                p in permitido for p in marca.split()
            ):
                continue
            if re.search(rf"(?<!\w){re.escape(marca)}(?!\w)", plana):
                ajenas.append(marca)
        for token in _PALABRA.findall(plana):
            if _sospechoso(token) and token not in permitido:
                ajenas.append(token)
    # Sin repetidos y en orden de aparición.
    vistas: dict[str, None] = {}
    for a in ajenas:
        vistas.setdefault(a, None)
    return list(vistas)


async def permitido_del_crm(ctx: Any) -> set[str]:
    """El catálogo completo, cacheado. Set vacío = no se pudo saber, y sin
    fuente de verdad la guarda NO opina (jamás frenar por no poder chequear)."""
    ahora = time.monotonic()
    if _cache["permitido"] and (ahora - _cache["at"]) < CACHE_TTL:
        return _cache["permitido"]
    try:
        data = await ctx.crm.get_catalogo(None)
    except Exception as exc:  # red, 404, inventario apagado: todo igual
        logger.warning("catálogo inaccesible para la guarda (%s) — no opino", exc)
        return set()
    permitido = tokens_del_catalogo((data or {}).get("modelos") or [])
    if permitido:
        _cache["permitido"] = permitido
        _cache["at"] = ahora
    return permitido


def limpiar_cache() -> None:
    _cache["permitido"] = None
    _cache["at"] = 0.0


_BORRADOR_MAX = 600


def alerta_maquinas_ajenas(ajenas: list[str], borrador: str) -> str:
    citado = borrador.strip()
    if len(citado) > _BORRADOR_MAX:
        citado = citado[:_BORRADOR_MAX] + "…"
    nombradas = ", ".join(ajenas)
    return (
        "ALERTA DEL SISTEMA — tu borrador de respuesta NO se envió. Decía: "
        f"«{citado}». Nombra máquinas, marcas o modelos que este negocio NO "
        f"tiene: {nombradas}. La flota es la que devuelve buscar_maquinas, y "
        "nada más.\n"
        "- Llamá buscar_maquinas AHORA y reescribí la respuesta recomendando "
        "SOLO lo que devuelva, con sus specs tal cual.\n"
        "- Si el lead nombró una marca que no tenemos, decíselo con todas las "
        "letras y ofrecele la máquina nuestra que hace ese trabajo.\n"
        "Recomendarle una máquina que no existe termina con el lead pidiéndole "
        "al asesor algo que nadie tiene."
    )


PREGUNTA_SEGURA = (
    "Dejame chequearlo bien en el catálogo antes de recomendarte, así no te "
    "paso una máquina que no tenemos. ¿Me contás de nuevo qué trabajo tenés "
    "que hacer y en qué espacio?"
)
