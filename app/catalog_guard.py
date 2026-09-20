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
_cache: dict[str, Any] = {"permitido": None, "at": 0.0, "modelos": None, "modelos_at": 0.0}

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

#: TIPOS de máquina del rubro que el modelo conoce de memoria. Mismo criterio
#: que MARCAS: si el tipo no está en el catálogo de este negocio y aparece en
#: la respuesta, es invención. Pasó (2026-09-19): "para una vereda céntrica lo
#: ideal es una miniexcavadora", y siguió con marca, modelo y medidas — RPM no
#: tiene miniexcavadoras.
TIPOS = (
    "miniexcavadora", "miniexcavadoras", "minipala", "minipalas",
    "manipulador telescopico", "plataforma elevadora", "tijera elevadora",
    "autoelevador", "autoelevadores", "montacargas", "grua torre",
    "hormigonera", "mixer", "motoniveladora de oruga", "zanjadora de cadena",
    "trencher", "bulldozer", "backhoe", "skid steer", "dumper", "dumpers",
)

_PALABRA = re.compile(r"[a-z0-9]+")
_DIGITOS = re.compile(r"\d+")
#: "8018 CTS", "3CX 4x4": número de modelo y sufijo separados por un espacio.
#: Con letras y números pegados los agarra `_sospechoso`; separados, no.
_CODIGO_PARTIDO = re.compile(r"\b(\d{3,6})\s+([a-z]{2,5})\b")
#: Lo que viene detrás de un número y NO es un modelo: unidades y palabras
#: corrientes (fechas, plata, medidas).
_NO_ES_MODELO = frozenset(
    """kpa psi bar rpm cv hp kw kva nm mm cm km kg tn ton hs hrs hr lts lt gal
    gpm mpa ksi db mts mt m2 m3 j l m t h de del a y o e por con en para mas
    sin iva dia dias mes meses ano anos hora horas ene feb mar abr may jun jul
    ago sep sept oct nov dic lun mie jue vie sab dom pesos mil""".split()
)
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
        for tipo in TIPOS:
            if all(p in permitido for p in tipo.split()):
                continue  # ese tipo SÍ está en el catálogo
            if re.search(rf"(?<!\w){re.escape(tipo)}(?!\w)", plana):
                ajenas.append(tipo)
        for numero, sufijo in _CODIGO_PARTIDO.findall(plana):
            if numero in permitido or sufijo in permitido:
                continue  # "320 DL", "1722 Grúa": del catálogo
            if sufijo in _NO_ES_MODELO or f"codigo:{numero}" in permitido:
                continue
            ajenas.append(f"{numero} {sufijo}")
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


#: "no lleva martillo", "no viene con hoyadora": decirlo está bien.
_NO_LLEVA = re.compile(r"\bno\b[^.!?\n]{0,30}\b(lleva|trae|viene|admite|se le pone|usa)\b")
_FRASE_CORTA = re.compile(r"[^.!?\n]+")


def implementos_mal_colgados(
    texto: str | None, modelos: dict[str, dict[str, Any]]
) -> list[str]:
    """Un implemento ofrecido en una máquina que no lo lleva.

    Pasó (2026-09-19): "para demoler te sirve una excavadora con martillo
    hidráulico" — los dos martillos son de la minicargadora. El lead llega a
    la obra esperando algo que esa máquina no puede montar.
    """
    if not texto or not modelos:
        return []
    duenos: dict[str, tuple[set[str], list[str]]] = {}
    maquinas: list[tuple[set[str], str, bool]] = []
    for nombre, specs in modelos.items():
        implementos = (specs or {}).get("implementos") or []
        maquinas.append((alias_de(nombre), nombre, bool(implementos)))
        for imp in implementos:
            if not isinstance(imp, dict) or not imp.get("nombre"):
                continue
            nombre_imp = str(imp["nombre"])
            alias, propietarios = duenos.setdefault(nombre_imp, (alias_de(nombre_imp), []))
            propietarios.append(nombre)
    if not duenos:
        return []
    problemas: dict[str, None] = {}
    for frase in _FRASE_CORTA.findall(normalizar(texto)):
        if _NIEGA.search(frase) or _NO_LLEVA.search(frase):
            continue
        palabras = set(_PALABRA.findall(frase))
        nombradas = [(nombre, tiene) for alias, nombre, tiene in maquinas if alias & palabras]
        if not nombradas:
            continue
        for nombre_imp, (alias_imp, propietarios) in duenos.items():
            if not (alias_imp & palabras):
                continue
            if any(nombre in propietarios for nombre, _ in nombradas):
                continue  # está nombrada la máquina que SÍ lo lleva
            ajenas = ", ".join(nombre for nombre, _ in nombradas)
            problemas.setdefault(
                f"{nombre_imp} es de {', '.join(propietarios)}; {ajenas} no lo lleva", None
            )
    return list(problemas)


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


async def modelos_del_crm(ctx: Any) -> dict[str, dict[str, Any]]:
    """{nombre: specs} de TODO el catálogo, cacheado.

    De quién es un implemento no depende de lo que el modelo haya buscado en
    este turno: buscando "grúa para demoler" el catálogo devuelve las grúas, y
    con eso no hay forma de saber que el martillo es de la minicargadora."""
    ahora = time.monotonic()
    if _cache["modelos"] is not None and (ahora - _cache["modelos_at"]) < CACHE_TTL:
        return _cache["modelos"]
    try:
        data = await ctx.crm.get_catalogo(None)
    except Exception as exc:
        logger.warning("catálogo inaccesible para la guarda (%s) — no opino", exc)
        return {}
    modelos = {
        str(m.get("nombre") or ""): (m.get("specs") or {})
        for m in (data or {}).get("modelos") or []
        if m.get("nombre")
    }
    if modelos:
        _cache["modelos"] = modelos
        _cache["modelos_at"] = ahora
    return modelos


def limpiar_cache() -> None:
    _cache["permitido"] = None
    _cache["at"] = 0.0
    _cache["modelos"] = None
    _cache["modelos_at"] = 0.0


_BORRADOR_MAX = 600


def alerta_implemento_ajeno(problemas: list[str], borrador: str) -> str:
    citado = borrador.strip()
    if len(citado) > _BORRADOR_MAX:
        citado = citado[:_BORRADOR_MAX] + "…"
    return (
        "ALERTA DEL SISTEMA — tu borrador de respuesta NO se envió. Decía: "
        f"«{citado}». Le cuelga un implemento a una máquina que no lo lleva:\n- "
        + "\n- ".join(problemas)
        + "\nCada implemento es de la máquina que lo tiene en `specs.implementos` "
        "y de ninguna otra. Reescribí: ofrecé la máquina que SÍ lo lleva, o esa "
        "máquina sin el implemento. Un lead que llega a la obra esperando un "
        "implemento que no existe es un alquiler que se cae.\n"
        "Y si para ESE trabajo no hay máquina en la flota que lo haga —demoler "
        "en altura, por ejemplo—, no busques la que más se le parece: decíselo "
        "derecho y llamá handoff en este turno."
    )


#: Cuando el modelo insiste con un implemento que esa máquina no lleva, el
#: turno termina con una persona: decisión del dueño (2026-09-19) para todo lo
#: que supere lo que pueden hacer las máquinas.
IMPLEMENTO_A_UN_ASESOR = (
    "Para ese trabajo no tengo la máquina con ese implemento. Te paso con un "
    "asesor, que ve con qué equipo se puede resolver. Te responde dentro del "
    "horario de atención 👍"
)


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
