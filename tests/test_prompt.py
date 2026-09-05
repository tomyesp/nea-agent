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


def test_fija_las_tres_condiciones_comerciales():
    """RPM cotiza la HORA, con operario y combustible incluidos y sin IVA.
    Las tres van juntas en cada precio: las dos primeras venden, la tercera
    evita la discusión el día de la factura."""
    p = _prompt()
    assert "HORA DE MÁQUINA" in p
    assert "operario y combustible" in p
    assert "SIN IVA" in p
    assert "sin aclarar que NO incluye IVA" in p


def test_pide_las_horas_antes_de_cotizar():
    """Sin horas por día no hay precio: el agente tiene que preguntarlas en
    vez de suponer una jornada."""
    p = _prompt()
    assert "cuántas horas por día" in p.lower()
    assert "no supongas una jornada" in p.lower()


def _prompt_ya_saludado() -> str:
    return build_system_prompt(
        profile=BusinessProfile(agent_name="Nea"),
        context={"contact": {"name": "Lead"}, "conversation": {}},
        conv=Conversation(id=1, wa_identity="5493511111111", greeted=True),
        referral_headline=None,
        offered=[],
        inventory=True,
        tz=ZoneInfo("America/Argentina/Buenos_Aires"),
    )


def test_el_primer_contacto_pide_saludo():
    assert "Es el PRIMER contacto" in _prompt()
    assert "YA te presentaste" not in _prompt()


def test_despues_del_saludo_se_prohibe_volver_a_presentarse():
    """El chasis dice "Primer mensaje: saludo + gancho + pregunta" siempre. Sin
    esta linea, con un mensaje de sistema reencuadrando el turno el modelo
    escribia la despedida y arrancaba la conversacion de nuevo abajo."""
    p = _prompt_ya_saludado()
    assert "YA te presentaste" in p
    assert "Es el PRIMER contacto" not in p
    assert "al pasar a un humano" in p

