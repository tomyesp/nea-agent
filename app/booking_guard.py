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
"""
from __future__ import annotations

import logging
import re
from typing import Any

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
