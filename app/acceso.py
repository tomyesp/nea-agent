"""¿Entra la máquina por donde hay que pasar? Lo calcula el código, no el modelo.

Pasó en vivo con Gemini (2026-09-19), con el ancho de la minicargadora ya en
la ficha: "la minicargadora mide 1,83 m, así que entra bien en el pasillo de
1,50 m que tenés", y a otro lead "la excavadora 320 debería entrar bien por el
pasillo" — una máquina de la que la ficha ni trae el ancho. Una regla en el
prompt no lo arregló: comparar dos medidas es justo lo que un modelo de
lenguaje hace mal, igual que multiplicar horas por tarifa. Por eso el precio
sale de cotizar y esto sale de acá.

Los datos de la máquina son `specs.ancho_m` y `specs.alto_m` (números, en
metros); un implemento más ancho que la máquina trae su propio `ancho_m` (el
cucharón, el rotocultivador). El paso lo dice el lead ("un pasillo de 1,50 m",
"el portón tiene 2 m de alto", "el techo está a 2,20") y se busca en SUS
mensajes; si el modelo lo manda en `ancho_paso_m`/`alto_paso_m`, gana el suyo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.catalog_guard import alias_de, normalizar

#: Menos margen que esto no se promete: que el asesor vea el acceso.
MARGEN_M = 0.10

_LUGAR = r"(?:pasillo|porton|portoncito|entrada|acceso|puerta|portada|callejon|tranquera)"
#: Lo que limita por ARRIBA: la altura solo cuenta si la frase habla de un
#: acceso o de algo de esto. "Los postes son de 2 m de alto" no es un paso.
_ARRIBA = r"(?:techo|dintel|alero|tinglado|cables?|galpon)"
_NUMERO = r"(\d+(?:[.,]\d+)?)"
_UNIDAD = r"(metros?|mts?|m|cm|centimetros?)?"
_ES_ALTO = r"\s*(?:de\s+)?(?:alto|alta|altura)\b"
#: "pasillo de 1,50 m", "el portón tiene 90 cm", "entrada de 2,20": el lugar
#: primero, la medida después, sin cortar la frase ni saltar otro número. Si
#: la medida dice "de alto", no es el ancho.
_ANCHO = re.compile(
    rf"\b{_LUGAR}\b[^.!?\n\d]{{0,40}}?{_NUMERO}\s*{_UNIDAD}(?![a-z])(?!{_ES_ALTO})"
)
#: "2 m de alto", "2,20 de altura" — en una frase que habla de un acceso.
_ALTO_DICHO = re.compile(rf"{_NUMERO}\s*{_UNIDAD}{_ES_ALTO}")
#: "el techo está a 2,20", "cables a 3 m".
_ALTO_ARRIBA = re.compile(rf"\b{_ARRIBA}\b[^.!?\n\d]{{0,30}}?{_NUMERO}\s*{_UNIDAD}(?![a-z])")
_FRASE = re.compile(r"[^.!?\n]+")
_HAY_ACCESO = re.compile(rf"\b(?:{_LUGAR}|{_ARRIBA})\b")


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


def _ultima(textos: list[str], buscar) -> float | None:
    """La última medida que dijo el lead: "perdón, el pasillo mide 2 metros"
    corrige a la de antes."""
    ultimo: float | None = None
    for texto in textos or []:
        for m in buscar(normalizar(texto or "")):
            metros = _a_metros(m.group(1), m.group(2))
            if metros is not None:
                ultimo = metros
    return ultimo


def paso_del_lead(textos: list[str]) -> float | None:
    """El ANCHO del paso que dijo el lead, en metros."""
    return _ultima(textos, _ANCHO.finditer)


def alto_del_lead(textos: list[str]) -> float | None:
    """El ALTO libre del paso que dijo el lead, en metros."""

    def buscar(plano: str):
        for frase in _FRASE.findall(plano):
            if _HAY_ACCESO.search(frase):
                yield from sorted(
                    [*_ALTO_DICHO.finditer(frase), *_ALTO_ARRIBA.finditer(frase)],
                    key=lambda m: m.start(),
                )

    return _ultima(textos, buscar)


def hablo_de_un_acceso(textos: list[str]) -> bool:
    """¿El lead nombró por dónde tiene que entrar la máquina?

    Sin esto, el modelo mandaba como paso lo primero que veía: a un lead con
    una "zanja de 60 cm de ancho" le contestó que no podía asegurarle que la
    excavadora pasara por 60 cm (2026-09-19). El ancho de la zanja no es una
    puerta."""
    return any(_HAY_ACCESO.search(normalizar(t or "")) for t in textos or [])


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


@dataclass(frozen=True)
class Paso:
    """Por dónde tiene que pasar la máquina. Cualquiera de las dos medidas
    puede faltar: el lead dice la que sabe."""

    ancho: float | None = None
    alto: float | None = None

    def __bool__(self) -> bool:
        return self.ancho is not None or self.alto is not None

    def describir(self) -> str:
        partes = []
        if self.ancho is not None:
            partes.append(f"{en_metros(self.ancho)} de ancho")
        if self.alto is not None:
            partes.append(f"{en_metros(self.alto)} de alto")
        return "un paso de " + " y ".join(partes)


def veredicto(paso: float, necesita: float) -> str:
    if paso < necesita:
        return "no"
    if paso - necesita < MARGEN_M:
        return "justo"
    return "si"


#: El peor veredicto manda: si no entra de alto, no importa que entre de ancho.
_GRAVEDAD = {"no": 0, "justo": 1, "sin_dato": 2, "si": 3}


def _implementos_que_no_pasan(specs: dict[str, Any], ancho_paso: float) -> list[tuple[str, float, str]]:
    """(nombre, ancho, veredicto) de los implementos más anchos que la
    máquina que no pasan holgados por el paso."""
    ancho = metros_de(specs.get("ancho_m"))
    if ancho is None:
        return []
    out = []
    for imp in specs.get("implementos") or []:
        if not isinstance(imp, dict):
            continue
        ancho_imp = metros_de(imp.get("ancho_m"))
        if ancho_imp is None or ancho_imp <= ancho:
            continue
        v = veredicto(ancho_paso, ancho_imp)
        if v != "si":
            out.append((str(imp.get("nombre") or "implemento"), ancho_imp, v))
    return out


def acceso_de(specs: dict[str, Any] | None, paso: Paso) -> dict[str, Any]:
    """El veredicto para UNA máquina, de ancho y de alto, con sus implementos
    más anchos que ella."""
    specs = specs or {}
    chequeos: list[tuple[str, str]] = []
    if paso.ancho is not None:
        ancho = metros_de(specs.get("ancho_m"))
        if ancho is None:
            chequeos.append(("sin_dato", "la ficha no trae el ancho de esta máquina"))
        else:
            chequeos.append(
                (
                    veredicto(paso.ancho, ancho),
                    f"mide {en_metros(ancho)} de ancho y el paso {en_metros(paso.ancho)}",
                )
            )
    if paso.alto is not None:
        alto = metros_de(specs.get("alto_m"))
        if alto is None:
            chequeos.append(("sin_dato", "la ficha no trae el alto de esta máquina"))
        else:
            chequeos.append(
                (
                    veredicto(paso.alto, alto),
                    f"mide {en_metros(alto)} de alto y el paso {en_metros(paso.alto)} de alto",
                )
            )
    entra = min((v for v, _ in chequeos), key=_GRAVEDAD.__getitem__, default="si")
    motivos = "; ".join(f for v, f in chequeos if v == entra)
    if entra == "no":
        detalle = f"NO ENTRA: {motivos}. No la recomiendes para ese acceso."
    elif entra == "justo":
        detalle = f"ENTRA MUY JUSTO: {motivos}. No prometas que entra: que el asesor vea el acceso."
    elif entra == "sin_dato":
        detalle = (
            f"{motivos}: NO digas que entra ni que no entra; ofrecé que el "
            "asesor vea el acceso."
        )
    else:
        detalle = f"ENTRA: {motivos}."
    resultado: dict[str, Any] = {"entra": entra}
    if paso.ancho is not None:
        resultado["ancho_del_paso_m"] = paso.ancho
    if paso.alto is not None:
        resultado["alto_del_paso_m"] = paso.alto
    if entra != "no" and paso.ancho is not None:
        # Con un implemento más ancho que la máquina, manda el implemento.
        no_pasan = [
            f"{nombre} ({en_metros(ancho_imp)}): {'NO pasa' if v == 'no' else 'pasa muy justo'}"
            for nombre, ancho_imp, v in _implementos_que_no_pasan(specs, paso.ancho)
        ]
        if no_pasan:
            resultado["implementos_que_no_pasan"] = no_pasan
            detalle += (
                " Pero con estos implementos puestos: "
                + "; ".join(no_pasan)
                + ". No los ofrezcas para entrar por ese paso sin que el asesor vea el acceso."
            )
    resultado["detalle"] = detalle
    return resultado


def ficha_para_el_paso(
    maquina: dict[str, Any], specs: dict[str, Any] | None, paso: Paso
) -> dict[str, Any]:
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
    if paso.ancho is not None and isinstance(specs.get("implementos"), list):
        fuera = {nombre for nombre, _, _ in _implementos_que_no_pasan(specs, paso.ancho)}
        if fuera:
            out["specs"] = {
                **specs,
                "implementos": [
                    imp
                    for imp in specs["implementos"]
                    if not (isinstance(imp, dict) and str(imp.get("nombre") or "implemento") in fuera)
                ],
            }
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


def entidades_de_acceso(
    modelos: dict[str, dict[str, Any]], paso: Paso
) -> list[tuple[set[str], bool, str]]:
    """(alias, ¿riesgosa?, detalle) de cada máquina y de cada implemento que
    no pasa. Riesgosa = no entra, entra justo o la ficha no trae la medida."""
    out: list[tuple[set[str], bool, str]] = []
    for nombre, specs in modelos.items():
        acceso = acceso_de(specs, paso)
        out.append((alias_de(nombre), acceso["entra"] != "si", f"{nombre}: {acceso['detalle']}"))
        if acceso["entra"] == "no" or paso.ancho is None:
            continue
        for nombre_imp, ancho_imp, _ in _implementos_que_no_pasan(specs or {}, paso.ancho):
            out.append(
                (
                    alias_de(nombre_imp),
                    True,
                    f"{nombre_imp}: mide {en_metros(ancho_imp)}, NO pasa por {paso.describir()}",
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


def alerta_acceso(problemas: list[str], borrador: str, paso: Paso) -> str:
    citado = borrador.strip()
    if len(citado) > _BORRADOR_MAX:
        citado = citado[:_BORRADOR_MAX] + "…"
    return (
        "ALERTA DEL SISTEMA — tu borrador de respuesta NO se envió. Decía: "
        f"«{citado}». Promete que entra por {paso.describir()} algo que el "
        "sistema calculó distinto:\n- "
        + "\n- ".join(problemas)
        + "\nReescribí sin comparar medidas vos: si no entra, decí que no "
        "entra; si entra justo o la ficha no trae la medida, decí que no se "
        "puede asegurar y que un asesor vea el acceso. Nunca 'entra' ni 'pasa' "
        "para eso."
    )


def pregunta_segura_acceso(paso: Paso) -> str:
    return (
        f"Con {paso.describir()} no te puedo asegurar que la máquina entre: "
        "eso lo tiene que ver un asesor en el lugar. ¿Hay otro acceso más "
        "grande, o querés que un asesor lo evalúe?"
    )
