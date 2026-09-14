"""La confirmación de una reserva nombra la máquina que de verdad quedó tomada.

Pasó en una prueba real: el lead eligió "la retro 1", la oferta de esa
máquina ya no estaba vigente, el agente terminó tomando la otra —una 416E— y
escribió "la retroexcavadora 406 quedó TOMADA". El resultado de la herramienta
traía la etiqueta correcta; el modelo la ignoró. Que el lead lea una máquina y
le llegue otra a la obra es de los peores errores posibles en este negocio, y
no es algo que se le pueda pedir al prompt que no vuelva a pasar.

Así que se verifica en código, como el Markdown (app/format.py) o el saludo
repetido (app/greeting.py): si en este turno se tomó o se movió una máquina y
la respuesta no la nombra —o no hay respuesta—, sale una confirmación armada
con los datos de la reserva. Se pierde la prosa del modelo; se gana que lo que
lee el lead es lo que va a llegar.

El caso inverso también pasó en vivo, con Qwen 3.8 Flash (2026-09-14): el lead
dijo "me quedo con la 406, tomala" y el modelo contestó "Listo, te la dejé
tomada" SIN llamar crear_reserva_tentativa. No quedó nada en el calendario y
el lead creía tener la máquina. Repitiendo ese momento reservó bien 8 de 8:
es raro, pero es la falla más cara. `afirma_reserva` detecta la afirmación y
turn.py no la deja salir si en el turno no se tomó nada.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Literal

logger = logging.getLogger("nea.booking_guard")


def _identidad(etiqueta: str) -> list[str]:
    """Las partes del nombre que distinguen UNA máquina de sus parecidas.

    La etiqueta arranca con el nombre del modelo ("Retroexcavadora 416E, sáb
    12 al…"). De ahí sirven los tokens con dígitos —416E, 1317, N35000,
    SK210LC—: "Retroexcavadora" no distingue una retro de otra. Alcanza con
    que la respuesta nombre UNO: nadie dice "Ford Cargo 1722 - Grúa N35000",
    dice "la grúa N35000", y eso ya la identifica.
    """
    nombre = etiqueta.split(", ", 1)[0]
    return [t for t in re.findall(r"\w+", nombre) if any(c.isdigit() for c in t)]


def _nombra(texto: str, token: str) -> bool:
    patron = rf"(?<!\w){re.escape(token)}(?!\w)"
    return re.search(patron, texto, flags=re.IGNORECASE) is not None


def confirmacion(booking: dict[str, Any]) -> str:
    """La confirmación armada con los datos de la reserva, sin prosa del modelo."""
    etiqueta = booking.get("etiqueta") or "la máquina"
    precio = booking.get("precio_total_sin_iva")
    precio_txt = (
        f", {precio} + IVA con operario y combustible incluidos"
        if precio and precio != "?"
        else ""
    )
    if booking.get("movida"):
        return (
            f"Listo, te la moví: {etiqueta}{precio_txt}. Sigue tomada hasta "
            "que un asesor te la confirme."
        )
    return (
        f"Listo, te la dejé tomada: {etiqueta}{precio_txt}. Un asesor te la "
        "confirma a la brevedad."
    )


def confirm_booking(reply_text: str, booking: dict[str, Any] | None) -> str:
    """La respuesta tal cual si nombra la máquina tomada; si no, la armada."""
    if not booking:
        return reply_text
    texto = (reply_text or "").strip()
    tokens = _identidad(str(booking.get("etiqueta") or ""))
    if texto and (not tokens or any(_nombra(texto, t) for t in tokens)):
        return reply_text
    logger.warning(
        "reserva: la respuesta no nombra la máquina tomada (%s) — sale la "
        "confirmación armada en código",
        booking.get("etiqueta"),
    )
    return confirmacion(booking)


# ------------------------------------------ reserva afirmada sin reservar ---

Afirmacion = Literal["reserva", "movida"]

_TOMADA = r"(?:tomad|reservad|apartad|bloquead|separad)[ao]s?"
_CLITICO = r"(?:la|lo|las|los)"

#: Frases que dan por HECHA una reserva. Solo tiempos pasados o de estado: el
#: prompt le enseña al modelo a OFRECER con "¿te la dejo tomada…?", y eso no
#: puede disparar la guarda. Se buscan sobre el texto sin tildes (el modelo a
#: veces las omite).
_AFIRMA_RESERVA = [
    re.compile(
        rf"\bte {_CLITICO} (?:deje|dejamos|tome|tomamos|reserve|reservamos|"
        r"aparte|apartamos|bloquee|bloqueamos|separe|separamos)\b"
    ),
    re.compile(rf"\b{_CLITICO} (?:deje|dejamos) {_TOMADA}"),
    re.compile(rf"\b(?:te )?(?:quedo|queda) {_TOMADA}"),
    re.compile(rf"\b{_CLITICO} tenes {_TOMADA}"),
    re.compile(rf"\besta {_TOMADA} (?:a tu nombre|para vos)"),
    re.compile(r"\btu reserva (?:ya )?(?:quedo|esta)\b"),
]
_AFIRMA_MOVIDA = [
    re.compile(
        rf"\bte {_CLITICO} (?:movi|movimos|corri|corrimos|pase|pasamos|"
        r"cambie|cambiamos)\b"
    ),
    re.compile(r"\b(?:quedo|queda) movid[ao]s?\b"),
    re.compile(rf"\bya {_CLITICO} (?:movi|corri)\b"),
]
#: En la frase ANTES de la afirmación: la vuelven condicional. Se buscan con
#: tildes, así "Sí, te la dejé tomada" no se lee como "si…".
_CONDICIONAL = re.compile(
    r"\b(?:si|cuando|apenas|en cuanto|una vez que|antes de|para que|"
    r"querés que|queres que|quieres que)\b"
)
#: En la cláusula (tras la última coma) antes de la afirmación: la niegan.
_NEGACION = re.compile(r"\b(?:no|nunca|todavía|todavia|aún|sin)\b")
#: En cualquier parte de la frase: la máquina la tiene OTRO, no el lead
#: ("la 416E quedó tomada por otro cliente" es disponibilidad, no una venta).
_DE_OTRO = re.compile(r"\b(?:otro cliente|otra persona|otro lead|alguien)\b")
#: Una frase termina en . ! ? o salto de línea, y una pregunta arranca en ¿.
_FRASES = re.compile(r"¿[^?\n]*\??|[^.!?¿\n]+[.!?]*")


def _sin_tildes(texto: str) -> str:
    """Quita tildes letra por letra: el largo no cambia, así las posiciones
    de un match sirven sobre el texto original."""
    return "".join(unicodedata.normalize("NFD", c)[0] for c in texto)


def _afirma_en(frase: str, patrones: list[re.Pattern[str]]) -> bool:
    plana = _sin_tildes(frase)
    for patron in patrones:
        for m in patron.finditer(plana):
            antes = frase[: m.start()]
            if _CONDICIONAL.search(antes):
                continue
            if _NEGACION.search(antes.rsplit(",", 1)[-1]):
                continue
            # "querés que te la deje tomada": subjuntivo, no pasado.
            if antes.rstrip().endswith("que"):
                continue
            return True
    return False


def afirma_reserva(texto: str | None) -> Afirmacion | None:
    """¿La respuesta da por hecha una reserva ("reserva") o un cambio de
    reserva ("movida")? None si no afirma nada: pregunta, ofrece, condiciona
    o habla de la máquina de otro."""
    if not texto:
        return None
    reserva = False
    for frase in _FRASES.findall(texto.lower()):
        frase = frase.strip()
        if not frase or frase.startswith("¿") or frase.endswith("?"):
            continue
        if _DE_OTRO.search(frase):
            continue
        if _afirma_en(frase, _AFIRMA_MOVIDA):
            return "movida"
        reserva = reserva or _afirma_en(frase, _AFIRMA_RESERVA)
    return "reserva" if reserva else None


_BORRADOR_MAX = 600


def alerta_reserva_falsa(afirmacion: Afirmacion, borrador: str) -> str:
    """El aviso al modelo. Cita el borrador para que se entienda aunque el
    proveedor junte todos los mensajes de sistema arriba."""
    citado = borrador.strip()
    if len(citado) > _BORRADOR_MAX:
        citado = citado[:_BORRADOR_MAX] + "…"
    if afirmacion == "movida":
        return (
            "ALERTA DEL SISTEMA — tu borrador de respuesta NO se envió. Decía: "
            f"«{citado}». Afirma que la reserva se movió o cambió, pero en este "
            "turno NO se movió nada: no llamaste cambiar_reserva_tentativa, o no "
            "devolvió ok.\n"
            "- Si el lead pidió sin dudas otras fechas u otra máquina y ya "
            "consultaste disponibilidad para eso (tenés el oferta_id nuevo), "
            "llamá cambiar_reserva_tentativa AHORA.\n"
            "- Si no, escribí la respuesta de nuevo SIN decir que la moviste: "
            "consultá disponibilidad o preguntale lo que falte."
        )
    return (
        "ALERTA DEL SISTEMA — tu borrador de respuesta NO se envió. Decía: "
        f"«{citado}». Afirma que la máquina quedó tomada, pero el lead NO tiene "
        "ninguna reserva: no llamaste crear_reserva_tentativa, o la herramienta "
        "no devolvió ok.\n"
        "- Si el lead ya aceptó sin dudas una máquina, fechas, horas por día y "
        "precio que vos le nombraste, y tenés el oferta_id de esa oferta, llamá "
        "crear_reserva_tentativa AHORA y confirmale con lo que devuelva.\n"
        "- Si falta algo o no está claro qué aceptó, escribí la respuesta de "
        "nuevo SIN decir que quedó tomada: preguntale lo que falte o pedile que "
        "confirme.\n"
        "Nunca digas que una máquina quedó tomada sin un ok de "
        "crear_reserva_tentativa."
    )


def _con_precio(etiqueta: str, precio: str | None) -> str:
    return f"{etiqueta}, {precio} + IVA" if precio and precio != "?" else etiqueta


def pregunta_segura(
    afirmacion: Afirmacion, ofertas: list[tuple[str, str | None]]
) -> str:
    """Lo que sale si el modelo insiste en afirmar lo que no pasó: en vez de
    la mentira, la pregunta de confirmación. `ofertas` son (etiqueta, precio)
    de las ofertas vigentes de la conversación."""
    if afirmacion == "movida":
        if len(ofertas) == 1:
            return (
                "Antes de moverte la reserva confirmame, así no la corro a algo "
                f"equivocado: ¿te la paso a {_con_precio(*ofertas[0])}?"
            )
        return (
            "Antes de moverte la reserva confirmame la máquina y las fechas "
            "nuevas, así no te la corro a días equivocados."
        )
    if len(ofertas) == 1:
        return (
            "Antes de dejártela tomada confirmame, así no bloqueo nada "
            f"equivocado: ¿te tomo {_con_precio(*ofertas[0])}, con operario y "
            "combustible incluidos?"
        )
    if 2 <= len(ofertas) <= 3:
        lineas = "\n".join(
            f"{i}. {_con_precio(etiqueta, precio)}"
            for i, (etiqueta, precio) in enumerate(ofertas, start=1)
        )
        return (
            "Antes de dejártela tomada confirmame cuál querés, así no bloqueo "
            f"la equivocada:\n{lineas}\n¿Con cuál avanzamos?"
        )
    return (
        "Antes de dejártela tomada confirmame la máquina, las fechas y las "
        "horas por día, así no te bloqueo nada equivocado."
    )
