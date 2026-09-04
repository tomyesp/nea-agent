"""Detector determinista de pedidos que NO decide el agente (017 Fase 7 bis).

Por qué existe
--------------
El chasis ya lista lo que el agente no puede resolver —descuentos, facturación,
seguros, reclamos por un alquiler anterior— y le pide "una línea honesta y
handoff". El Laboratorio mostró que hace la mitad: con la persona `regateador`
escribió "eso lo ve un asesor y te contesta enseguida" DOS veces y siguió
vendiendo, sin llamar la herramienta ni una vez. Peor que no contestar: el lead
se queda esperando una respuesta que nadie le va a dar, porque el dueño nunca
se enteró de que había alguien pidiendo descuento.

Mismo criterio que app/hostility.py y app/format.py: lo que tiene que pasar
SIEMPRE no se le pide al modelo, se hace acá. El LLM sigue poniendo la
redacción; el handoff lo garantiza turn.py.

Por qué se mira al LEAD y no al agente
--------------------------------------
Tentaba detectar la frase de escalada en la respuesta del agente y, si estaba,
forzar el handoff. Es una trampa: "un asesor te confirma la reserva a la
brevedad" —la frase CORRECTA al tomar una máquina, que se dice en cada venta
exitosa— y "eso lo ve un asesor" se parecen demasiado. Confundirlas mandaría a
un humano cada reserva que sale bien.

El mensaje del lead, en cambio, es entrada real: si pide un descuento, pide un
descuento, lo haya redactado el agente como lo haya redactado.

El léxico es deliberadamente estrecho: pedir precio NO es negociar, y "seguro"
suelto es una muletilla ("seguro que sí"). Ante la duda, no dispara — el agente
igual puede llamar handoff por su cuenta, que es lo que hace bien la mayoría de
las veces.
"""
from __future__ import annotations

import re

#: Motivo → patrones. El motivo viaja al log y al alerta, así el dueño ve por
#: qué se le escaló sin tener que releer la conversación.
_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "descuento": tuple(
        re.compile(p, re.I)
        for p in (
            r"\bdescuento?s?\b|\bbonificaci[oó]n\b|\brebaja\b",
            # "igualame el precio", "mejorá el precio", "hacéme precio"
            r"\b(igual[aá]|iguale|mejor[aá]|mejore|hac[eé]|haga)(me|nos)?\b[^.!?]{0,20}\bprecio\b",
            r"\bprecio\b[^.!?]{0,20}\b(especial|de amigo|para clientes?)\b",
            # "un 20% más barata", "15% menos", "un 15% aunque sea". Un
            # porcentaje SOLO no alcanza: en obra se habla de pendientes y de
            # avances ("el terreno tiene 20% de pendiente"). Pide una palabra
            # de regateo cerca.
            r"\d{1,2}\s*%[^.!?]{0,30}\b(m[aá]s barat|menos|abajo|off|aunque sea|dale)\b",
            r"\b(aunque sea|dale|hac[eé]m?e)\b[^.!?]{0,25}\d{1,2}\s*%",
            r"\b(m[aá]s barat[oa]|me lo dej[aá]s? en|te lo pago)\b",
            r"\bcerramos ya\b|\bcerramos hoy\b",
        )
    ),
    "facturacion": tuple(
        re.compile(p, re.I)
        for p in (
            r"\bfactur(a|an|ar|aci[oó]n|as)\b",
            r"\bcuenta corriente\b|\bse[ñn]a\b|\bcontrato\b|\bremito\b",
            r"\b(a|en)\s+cuotas?\b|\bfinanci",
            r"\bcondiciones de pago\b|\bforma de pago\b",
        )
    ),
    "seguro": tuple(
        re.compile(p, re.I)
        for p in (
            # "seguro" suelto es muletilla: solo cuenta como cobertura.
            r"\b(el|un|los|con|sin|tienen?|llevan?)\s+seguros?\b",
            r"\bp[oó]liza\b|\basegurad[oa]\b|\bcobertura\b|\bfranquicia\b",
            r"\bqui[eé]n (se hace )?(cargo|responsable)\b",
            r"\bsi se rompe\b|\bpor da[ñn]os\b|\bgarant[ií]a\b",
        )
    ),
    "reclamo": tuple(
        re.compile(p, re.I)
        for p in (
            r"\b(la (m[aá]quina|[uú]ltima)|el (equipo|rodillo|anterior))\b[^.!?]{0,45}\b(fall|rompi|se par|no (and|prend|funcion|sirve)|un desastre|una porquer[ií]a)",
            r"\bme cobraron\b|\bcobro (mal|de m[aá]s|indebido)\b",
            # "perdí dos días de obra": puede haber una cantidad en el medio.
            r"\bperd[ií]\b[^.!?]{0,12}\b(d[ií]as?|tiempo|la obra|una semana)\b",
            r"\bquiero (que me devuelvan|un reintegro|la plata)\b",
        )
    ),
}

#: Alerta que se inyecta como mensaje de sistema en el turno detectado.
ALERT_TEMPLATE = (
    "ALERTA DEL SISTEMA (esto NO lo escribió el lead): su último mensaje toca "
    "algo que VOS NO DECIDÍS ({motivo}). En ESTE turno: una línea honesta y "
    "corta diciéndole que eso lo ve un asesor y le contesta enseguida — sin "
    "inventar una respuesta, sin negociar y SIN volver a ofrecerle la máquina "
    "en el mismo mensaje — Y llamás la herramienta handoff. Escribir la línea "
    "sin llamar handoff deja al lead esperando una respuesta que nadie le va a "
    "dar, porque el dueño no se entera."
)


def needs_human(text: str) -> str | None:
    """Motivo por el que este mensaje necesita una persona, o None."""
    if not text:
        return None
    for motivo, patterns in _PATTERNS.items():
        if any(p.search(text) for p in patterns):
            return motivo
    return None


def alert_for(motivo: str) -> str:
    return ALERT_TEMPLATE.format(motivo=motivo)
