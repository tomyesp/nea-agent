"""017 Fase 7 (bis) — Reglas duras del chasis que el Laboratorio encontró faltando.

Un prompt no se testea por su redacción, pero sí se puede fijar que una regla
que costó una corrida entera descubrir no se caiga en el próximo retoque.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation


def _prompt() -> str:
    return build_system_prompt(
        profile=BusinessProfile(agent_name="Nea"),
        context={"contact": {"name": "Lead"}, "conversation": {}},
        conv=Conversation(id=1, wa_identity="5493511111111"),
        referral_headline=None,
        offered=[],
        inventory=True,
        tz=ZoneInfo("America/Argentina/Buenos_Aires"),
    )


def test_prohibe_prometer_hora_de_entrega():
    """Persona `apurado`: prometió "mañana a las 7 la tenés lista en Alta
    Gracia". Ninguna herramienta devuelve horarios de entrega — el traslado lo
    coordina el asesor, y una obra parada esperando un camión que nadie mandó
    es el costo real de esa frase."""
    p = _prompt()
    assert "hora ni logística de entrega" in p
    assert "coordina el asesor" in p


def test_prohibe_generalizar_la_disponibilidad():
    """Persona `fechas_ocupadas`: ofreció la JCB para un rango y dos mensajes
    después dijo que "ambas" retros estaban ocupadas, habiendo consultado una
    sola."""
    p = _prompt()
    assert "UNO POR UNO" in p
    assert "NUNCA te desdigas" in p


def test_un_cambio_de_fechas_se_mueve_no_se_acumula():
    """Persona `cambia_de_idea`. La regla ya estaba; lo que faltaba era que el
    servidor la hiciera posible (ver test_inventario_vocero)."""
    p = _prompt()
    assert "cambiar_reserva_tentativa" in p
    assert "NO crees una segunda reserva" in p
