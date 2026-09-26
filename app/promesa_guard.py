"""017 — Anunciar que va a buscar y cortar sin buscar.

Pasó en vivo (2026-09-26): "Ok. Con esa información te busco las mejores
opciones para el trabajo." y el turno terminó sin una sola llamada. El agente
no escribe primero (FOLLOWUP_HOURS=0), así que ese "te busco" deja al lead
esperando algo que nunca llega. Si la respuesta anuncia una búsqueda y el turno
no tocó el catálogo, el modelo tiene una vuelta más para buscar de verdad.
"""

from __future__ import annotations

import re

from app.catalog_guard import normalizar

_ANUNCIA = re.compile(
    r"\b(?:te|lo|la|los|las)\s+(?:busco|chequeo|reviso|averiguo|verifico)\b"
    r"|\b(?:voy a|dejame|ya)\s+(?:buscar|chequear|revisar|averiguar|verificar|fijarme)\b"
    r"|\bdame un (?:momento|segundo|minuto)\b"
)

AVISO = (
    "AVISO DEL SISTEMA — tu respuesta anuncia que vas a buscar o revisar algo, "
    "pero en este turno no llamaste ninguna herramienta. Todavía no salió. Vos "
    "no escribís hasta que el lead vuelva a escribir: si la mandás así, se "
    "queda esperando algo que nunca llega. Llamá AHORA la herramienta que "
    "haga falta (buscar_maquinas, consultar_disponibilidad…) y respondé con "
    "el resultado en este mismo mensaje. Si de verdad te falta un dato del "
    "lead para buscar, preguntáselo sin anunciar una búsqueda."
)


def anuncia_una_busqueda(texto: str | None) -> bool:
    return bool(texto) and bool(_ANUNCIA.search(normalizar(texto)))
