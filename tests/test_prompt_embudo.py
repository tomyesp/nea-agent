"""017 — El embudo del prompt y el silencio del agente.

Cambios pedidos por el dueño el 2026-09-17, después de sus pruebas reales:
asesorar antes que vender, juntar datos de la obra, ofrecer el equipo que
completa el trabajo SIN precios, y no escribirle nunca primero al lead.
"""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx

from app.llm import LlmReply
from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation, InboundMessage, utcnow
from app.turn import run_turn
from tests.conftest import IDENTITY, FakeLLM, make_ctx, make_settings, mock_crm_basics

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def _prompt(**kwargs) -> str:
    return build_system_prompt(
        profile=BusinessProfile(agent_name="Pericles"),
        context={"contact": {"name": "Lead"}, "conversation": {}},
        conv=Conversation(id=1, wa_identity="5493511111111"),
        offered=[],
        inventory=True,
        tz=TZ,
        **kwargs,
    )


def test_el_prompt_va_en_bloques_etiquetados():
    """Etiquetas <xml> por tarea: cada regla se cita, se prueba y se cambia sola."""
    p = _prompt()
    for etiqueta in (
        "<rol>",
        "<voz>",
        "<embudo>",
        "<catalogo>",
        "<precios>",
        "<fechas>",
        "<reserva>",
        "<handoff>",
        "<blindaje>",
        "<herramientas>",
        "<nunca>",
        "<multimedia>",
        "<perfil_del_negocio>",
        "<contexto_actual>",
    ):
        assert etiqueta in p, etiqueta
        assert etiqueta.replace("<", "</") in p, etiqueta


def test_primero_asesora_y_despues_vende():
    p = _prompt()
    assert "ASESOR primero y un vendedor después" in p
    assert "no cuando lo apuraste" in p


def test_las_cinco_etapas_estan_y_no_se_saltean_hacia_adelante():
    p = _prompt()
    for etapa in ("entender la obra", "recomendar desde el catálogo",
                  "completar el equipo", "precio, fechas y disponibilidad",
                  "dejarla tomada"):
        assert etapa in p, etapa
    assert "Hacia adelante NO se saltean" in p


def test_pregunta_por_la_obra_antes_de_nombrar_maquinas():
    """El dueño lo pidió así: que sepa buscar la necesidad del cliente."""
    p = _prompt()
    assert "ANTES de nombrar ninguna máquina" in p
    for dato in ("TAMAÑO", "ACCESO y ESPACIO", "MATERIAL y SUELO", "SACAR material"):
        assert dato in p, dato


def test_el_equipo_complementario_se_ofrece_sin_precios():
    p = _prompt()
    assert "completar el equipo" in p
    assert "SIN PRECIOS" in p
    assert "camión volquete" in p and "rodillo" in p
    assert "ya sepa exactamente qué equipo necesita" in p


def test_no_insiste_ni_escribe_primero():
    p = _prompt()
    assert "<no_insistas>" in p
    assert "Nunca escribís primero" in p
    assert "¿seguís ahí?" in p
    assert "DEJÁS DE OFRECERLE TOMAR LA MÁQUINA" in p


def test_prohibe_marcas_de_memoria():
    """Pasó en vivo: ofreció "una minicargadora Bobcat" y una "New Holland
    RG140" — ninguna de las dos está en la flota."""
    p = _prompt()
    assert "JAMÁS nombres una marca o un modelo de memoria" in p
    assert "Bobcat" in p and "New Holland" in p
    assert "buscala en el catálogo ANTES de contestar qué es" in p


def test_recomienda_en_vez_de_seguir_preguntando():
    """En vivo el agente pidió tres veces el tipo de suelo y nunca recomendó
    nada; al "dale, esa me sirve" contestó "todavía no te recomendé"."""
    p = _prompt()
    assert "NUNCA preguntes dos veces lo mismo" in p
    assert "pasá YA a la etapa 2 con lo que tengas" in p
    assert "apenas el lead te pida una recomendación" in p


def test_no_calcula_cuanto_tarda_la_obra():
    p = _prompt()
    assert "<tiempos_de_obra>" in p
    assert "NO lo sabés vos" in p
    assert "Las horas las decide el lead o el asesor" in p


def test_el_minimo_de_horas_lo_pone_el_negocio_no_el_chasis():
    """RPM pasó de 6 horas a 1: el número vive en el perfil del CRM, editable
    por el dueño, no clavado en el código."""
    p = _prompt()
    assert "El mínimo de horas lo fija el negocio" in p
    assert "6 horas" not in p


def test_lo_que_la_flota_no_puede_hacer_lo_ve_un_asesor():
    """Decisión del dueño (2026-09-19): demoler en altura, o cualquier obra
    que supere lo que dan las máquinas, se escala — no se ofrece una máquina
    que no hace el trabajo (pasó: mini con martillo para 3 pisos)."""
    p = _prompt()
    assert "El trabajo SUPERA lo que puede hacer la flota" in p
    assert "para eso no tengo la máquina" in p


def test_anunciar_el_pase_a_humano_obliga_a_llamarlo():
    """Pasó en vivo: escribió "te lo paso a un asesor" y nadie se enteró."""
    assert "LLAMÁ la herramienta en ese mismo turno" in _prompt()


def test_puede_escribir_y_llamar_herramientas_en_el_mismo_turno():
    assert "llamar herramientas en el mismo turno" in _prompt()


def test_recomienda_maquina_con_su_implemento():
    """RPM alquila la minicargadora con el implemento del trabajo (martillo,
    hoyadora, zanjeadora…), incluido en la hora. El asesor necesita saber cuál
    preparar, y un implemento no es otra máquina (2026-09-19)."""
    p = _prompt()
    assert "lo que recomendás es máquina + implemento" in p
    assert "guardalo con update_ficha en `implementos`" in p
    assert "copialas, no las supongas" in p
    assert "Un implemento NO es otra máquina" in p
    assert "la máquina (con su implemento, si lleva)" in p
    # En vivo (Gemini) aceptaba "dale, esa me sirve" y la ficha quedaba vacía.
    assert "en ESE turno llamá update_ficha con `maquina_interes`" in p


def test_compara_el_paso_con_el_ancho_antes_de_recomendar():
    """En vivo (Gemini, 2026-09-19): "la minicargadora mide 1,83 m, así que
    entra bien en el pasillo de 1,50 m que tenés"."""
    p = _prompt()
    assert "¿ENTRA?" in p
    assert "NO compares medidas vos" in p
    assert "'no' = esa máquina NO entra" in p
    assert "Si ninguna del catálogo entra" in p


def test_la_ficha_tiene_lugar_para_los_implementos():
    """maquina_interes se pisa con la etiqueta de la oferta al reservar: el
    implemento necesita su propio campo."""
    from app.tools import TOOL_SCHEMAS

    ficha = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == "update_ficha")
    assert "implementos" in ficha["function"]["parameters"]["properties"]


# ------------------------------------------- el agente no escribe primero ---


async def _turno(respx_mock, horas: float):
    ctx = make_ctx(
        settings=make_settings(followup_hours=horas),
        llm=FakeLLM(replies=[LlmReply(content="Contame qué obra tenés.")]),
    )
    mock_crm_basics(respx_mock)
    await run_turn(
        ctx,
        IDENTITY,
        [InboundMessage(wa_message_id="wamid.1", identity=IDENTITY, type="text", text="hola")],
    )
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.crm.aclose()
    return conv


async def test_con_el_seguimiento_apagado_no_se_agenda_ningun_empujon(respx_mock):
    conv = await _turno(respx_mock, 0)
    assert conv.followup_due_at is None


async def test_si_el_negocio_lo_enciende_vuelve_a_agendarse(respx_mock):
    conv = await _turno(respx_mock, 4)
    assert conv.followup_due_at is not None
    assert conv.followup_due_at > utcnow() + timedelta(hours=3)


def test_el_default_es_no_escribir_primero():
    from app.config import Settings

    assert Settings.model_fields["followup_hours"].default == 0.0
