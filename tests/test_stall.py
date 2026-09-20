"""Candado de cierre: conversación que no va a ningún lado.

El agente se despide amable UNA vez y después calla. El conteo es determinista
(app/stall.py); aquí se fija tanto el detector como el silencio posterior.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app import turn
from app.stall import MAX_MENSAJES_SIN_AVANCE, es_relleno, racha_vacia, sin_rumbo
from app.state import utcnow
from tests.conftest import IDENTITY, mock_crm_basics, wa_body


# ------------------------------------------------------------- detector ---


@pytest.mark.parametrize(
    "texto",
    ["ok", "va", "ajá", "jaja", "jajaja", "jejeje", "👍", "🙏🙏", "  ", "Gracias", "ok 👍", "gracias!! 🙏"],
)
def test_relleno_no_aporta(texto):
    assert es_relleno(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "tengo una clínica dental",
        "no me interesa",  # un "no" es una respuesta clarísima, no relleno
        "ahorita no puedo, la otra semana",
        "somos 12",
        "ok pero cuánto cuesta",
        "hola",  # un saludo es jugada legítima, no vacío
        "buenas tardes",
    ],
)
def test_contenido_real_no_es_relleno(texto):
    assert es_relleno(texto) is False


def test_un_mensaje_con_contenido_corta_la_racha():
    assert racha_vacia(["ok", "va", "tengo una taquería", "ok"]) == 1
    assert racha_vacia(["tengo una taquería", "ok", "va", "ajá"]) == 3


def test_sin_rumbo_por_racha_de_vacios():
    assert sin_rumbo(["hola", "ok", "va", "ajá"], "descubrimiento") is True
    # Dos seguidos no bastan: el saludo NO cuenta como vacío.
    assert sin_rumbo(["hola", "ok", "va"], "descubrimiento") is False


def test_sin_rumbo_por_conversacion_larga_sin_avance():
    largos = [f"mensaje con contenido numero {i}" for i in range(MAX_MENSAJES_SIN_AVANCE)]
    assert sin_rumbo(largos, "descubrimiento") is True
    # 14 mensajes NO alcanzan: con el embudo de asesoramiento una charla que
    # va bien los pasa. Se cerró un lead que en el mensaje siguiente dijo "la
    # quiero el lunes a primera hora" (2026-09-19).
    assert sin_rumbo(largos[:14], "descubrimiento") is False


def test_agendando_nunca_se_cierra_por_el_candado():
    """Ya hay rumbo: el candado no se mete aunque el lead conteste corto."""
    assert sin_rumbo(["ok", "va", "ajá"], "agendando") is False
    assert sin_rumbo([f"m{i}" * 10 for i in range(20)], "cerrada") is False


# ----------------------------------------------------------- en el turno ---


async def _tres_vacios(ctx, client):
    for i, texto in enumerate(("ok", "va", "ajá")):
        await client.post(
            "/webhook", content=wa_body(text=texto, wamid=f"wamid.v{i}")
        )
        await asyncio.sleep(0.35)


async def test_cierra_una_vez_y_despues_calla(ctx, client, respx_mock):
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await _tres_vacios(ctx, client)

    # Se despidió en el tercero (uno por mensaje: 3 respuestas).
    assert routes["messages"].call_count == 3
    marcada = (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at
    assert marcada is not None

    # El alertazo de cierre viajó en el turno del cierre, no antes.
    sistemas = [
        m["content"]
        for m in ctx.llm.calls[-1]["messages"]
        if m["role"] == "system"
    ]
    assert any("UNA línea cálida de cierre" in s for s in sistemas)

    # Y a partir de aquí, silencio: ni LLM ni envío.
    llamadas_previas = len(ctx.llm.calls)
    await client.post("/webhook", content=wa_body(text="👍", wamid="wamid.v9"))
    await asyncio.sleep(0.35)
    assert routes["messages"].call_count == 3
    assert len(ctx.llm.calls) == llamadas_previas


async def test_el_lead_que_vuelve_tras_el_enfriamiento_reabre(
    ctx, client, respx_mock
):
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id="cv_test1",
        stalled_at=utcnow() - turn.STALL_COOLDOWN - timedelta(minutes=1),
    )

    await client.post(
        "/webhook", content=wa_body(text="oye, ya lo pensé mejor", wamid="wamid.r1")
    )
    await asyncio.sleep(0.35)

    assert routes["messages"].call_count == 1  # volvió a atenderlo
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is None


async def test_el_lead_que_vuelve_con_algo_concreto_reabre_en_el_acto(
    ctx, client, respx_mock
):
    """Pasó en vivo: se cerró la charla y 30 segundos después el lead escribió
    "la quiero el lunes a primera hora". Nadie le contestó."""
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv_test1", stalled_at=utcnow()
    )

    await client.post(
        "/webhook",
        content=wa_body(text="bien, la quiero el lunes a primera hora", wamid="wamid.r2"),
    )
    await asyncio.sleep(0.35)

    assert routes["messages"].call_count == 1
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is None


async def test_el_candado_no_pisa_el_handoff_por_hostilidad(
    ctx, client, respx_mock
):
    """Tres groserías seguidas son cortas y podrían leerse como "relleno": el
    handoff por hostilidad manda, porque el dueño tiene que ver esa conversación."""
    routes = mock_crm_basics(respx_mock)
    for i, texto in enumerate(("son unos rateros", "puro humo", "pinches estafadores")):
        await client.post(
            "/webhook", content=wa_body(text=texto, wamid=f"wamid.h{i}")
        )
        await asyncio.sleep(0.35)

    assert routes["handoff"].call_count == 1
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is None


async def test_no_manda_escribiendo_a_una_conversacion_cerrada(
    ctx, client, respx_mock
):
    """Un "escribiendo…" seguido de silencio es peor que el silencio solo.

    Con un mensaje de relleno: uno con contenido reabre la charla."""
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv_test1", stalled_at=utcnow()
    )
    previos = routes["typing"].call_count

    await client.post("/webhook", content=wa_body(text="ok", wamid="wamid.t1"))
    await asyncio.sleep(0.35)

    assert routes["typing"].call_count == previos
    assert routes["messages"].call_count == 0
