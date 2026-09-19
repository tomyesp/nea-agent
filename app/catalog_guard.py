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
_DIGITOS = re.compile(r"\d+")
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


def _textos(valor: Any) -> Iterable[str]:
    """Los textos de un valor de specs, a cualquier profundidad: los
    implementos viajan como una lista de fichas adentro de la ficha."""
    if isinstance(valor, str):
        yield valor
    elif isinstance(valor, dict):
        for v in valor.values():
            yield from _textos(v)
    elif isinstance(valor, list):
        for v in valor:
            yield from _textos(v)


def alias_de(nombre: str) -> set[str]:
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


def tokens_del_catalogo(modelos: Iterable[dict[str, Any]]) -> set[str]:
    """Todas las palabras que el negocio SÍ puede nombrar: nombres de modelo,
    marcas, categorías, descripciones y specs del catálogo.

    Las specs cuentan: ahí viven el motor ("Cat 3044C") y los implementos
    ("martillo CAT H55D S"). Sin ellas, citar la ficha tal cual —que es lo que
    pide el prompt— se frenaba como si fuera un modelo inventado."""
    permitido: set[str] = set()
    for m in modelos or []:
        for campo in ("nombre", "marca", "categoria", "descripcion", "specs"):
            for texto in _textos(m.get(campo)):
                permitido.update(_PALABRA.findall(normalizar(texto)))
        # Los NÚMEROS de los nombres, aparte: sirven para detectar un modelo
        # mal escrito ("320L" cuando la máquina es la "320 DL"). Ver
        # `_codigo_mutado`.
        nombre = m.get("nombre")
        if isinstance(nombre, str):
            for digitos in _DIGITOS.findall(normalizar(nombre)):
                permitido.add(f"codigo:{digitos}")
    return permitido


def _sospechoso(token: str) -> bool:
    """¿Parece un código de modelo (letras + números) y no una medida?"""
    if len(token) < 3 or _UNIDAD.match(token) or _MEDIDA.match(token):
        return False
    letras = sum(c.isalpha() for c in token)
    digitos = sum(c.isdigit() for c in token)
    return letras >= 1 and digitos >= 2


def _codigo_mutado(token: str, permitido: set[str]) -> bool:
    """Un modelo del catálogo escrito mal, disfrazado de medida.

    Pasó con un lead real (2026-09-19): el agente ofreció una "Caterpillar
    320L" —la máquina es la "Excavadora 320 DL"—, y `_sospechoso` la dejó
    pasar porque "320l" se lee como "320 litros". Con ese nombre torcido pidió
    disponibilidad de otra máquina y terminó diciéndole al lead que la 320L no
    estaba libre. Si los números del token son los de un modelo del catálogo y
    las letras no coinciden, es ESE modelo mal escrito."""
    if not any(c.isalpha() for c in token) or not any(c.isdigit() for c in token):
        return False
    digitos = _DIGITOS.search(token)
    return digitos is not None and f"codigo:{digitos.group()}" in permitido


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
            if token in permitido:
                continue
            if _sospechoso(token) or _codigo_mutado(token, permitido):
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
