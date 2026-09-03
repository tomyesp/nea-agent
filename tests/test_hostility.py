"""Hostilidad sostenida (AC-18): léxico + backstop determinista del turno."""
from __future__ import annotations

import asyncio

import httpx

from app.config import canonical_identity
from app.hostility import hostile_streak, is_hostile
from app.main import create_app
from tests.conftest import (
    CRM_CONV_ID,
    CRM_URL,
    IDENTITY,
    make_ctx,
    mock_crm_basics,
    wa_body,
)


def test_coloquial_mexicano_no_cuenta():
    for texto in (
        "no mames, qué chido quedó esto",
        "qué pedo, ¿cómo funciona?",
        "está bien vergas tu sistema",  # entusiasmo, no dirigido
        "me urge, ando hasta la madre de trabajo",
    ):
        assert not is_hostile(texto), texto


def test_agresion_dirigida_cuenta():
    for texto in (
        "vete mucho a la verga con tu asesoría",
        "pinche estafador",
        "esto es una estafa o qué pedo",
        "puro humo, pinches bots chafas",
        "chinga tu madre",
    ):
        assert is_hostile(texto), texto


def test_coloquial_argentino_no_cuenta():
    """017 — Un obrero puteando por el clima NO es un lead hostil: así se
    habla en obra, y un falso positivo pausa la IA de un cliente real."""
    for texto in (
        "che boludo, ¿tenés retro para el lunes?",
        "la puta madre, se largó a llover justo hoy",
        "de una, mandame el presupuesto",
        "está bárbaro, dale que va",
        "uh qué quilombo la obra hoy",
    ):
        assert not is_hostile(texto), texto


def test_agresion_dirigida_argentina_cuenta():
    for texto in (
        "andá a la concha de tu madre con ese precio",
        "sos un pelotudo",
        "son unos garcas, muertos de hambre",
        "esto es una estafa, chantas",
        "hijo de puta, no me contestás nunca",
    ):
        assert is_hostile(texto), texto


def test_identidad_argentina_NO_se_toca_para_hablar_con_el_crm():
    """017 — El CRM guarda el número tal cual lo manda Meta (solo normaliza
    México). Si Nea le sacara el 9, pediría el contexto con una identidad que
    el CRM no conoce y devolvería 404: el lead quedaría mudo. Se cazó en vivo
    en la primera prueba end-to-end."""
    assert canonical_identity("5493511234567") == "5493511234567"
    # El caso mexicano del upstream sigue funcionando.
    assert canonical_identity("5215512345678") == "525512345678"
    # Un BSUID pasa tal cual.
    assert canonical_identity("bsuid:abc123") == "bsuid:abc123"


def test_la_allowlist_si_tolera_el_9_de_movil():
    """La tolerancia vive donde es inofensiva: comparar contra listas locales."""
    from app.config import Settings

    s = Settings(allowed_wa_ids="543511234567")
    # El dueño la cargó sin el 9 y Meta manda con el 9: igual entra.
    assert canonical_identity("5493511234567") in s.allowed_identities
    assert canonical_identity("543511234567") in s.allowed_identities

    s2 = Settings(allowed_wa_ids="5493511234567")  # y al revés
    assert canonical_identity("543511234567") in s2.allowed_identities


def test_racha_se_corta_si_el_lead_se_calma():
    assert hostile_streak(["eres una estafa", "ok perdón, cuéntame más", "pinche bot"]) == 1


def test_racha_de_tres_al_final():
    assert (
        hostile_streak(["hola", "esto es estafa", "pinches bots chafas", "vete a la verga"])
        == 3
    )


async def test_tercer_strike_fuerza_handoff_aunque_el_llm_no_lo_llame(respx_mock):
    """El FakeLLM JAMÁS llama herramientas — el handoff sale del backstop."""
    ctx = make_ctx()
    routes = mock_crm_basics(respx_mock)

    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    hostiles = [
        "oye esto es una estafa o qué pedo",
        "no mames, puro humo, pinches bots chafas",
        "vete mucho a la verga con tu asesoría, pinche estafador",
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        for i, texto in enumerate(hostiles):
            await c.post("/webhook", content=wa_body(text=texto, wamid=f"wamid.host{i}"))
            await asyncio.sleep(0.3)
    await ctx.crm.aclose()

    assert routes["handoff"].call_count == 1
    import json

    body = json.loads(routes["handoff"].calls[0].request.content)
    assert body.get("reason") == "hostilidad"
    # La despedida del tercer turno se envió ANTES de la pausa (3 respuestas).
    assert routes["messages"].call_count == 3
    # El turno del strike recibió la alerta del sistema.
    ultimo = ctx.llm.calls[-1]["messages"]
    assert any(
        m.get("role") == "system" and "TERCER" in str(m.get("content"))
        for m in ultimo
    )


async def test_dos_strikes_no_disparan_nada(respx_mock):
    ctx = make_ctx()
    routes = mock_crm_basics(respx_mock)
    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        for i, texto in enumerate(["esto es una estafa", "pinches bots chafas"]):
            await c.post("/webhook", content=wa_body(text=texto, wamid=f"wamid.h2{i}"))
            await asyncio.sleep(0.3)
    await ctx.crm.aclose()
    assert routes["handoff"].call_count == 0