"""La confirmación nombra la máquina que de verdad quedó tomada.

El caso que lo motivó: el agente tomó una 416E y le escribió al lead que había
quedado tomada la 406.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.booking_guard import confirm_booking
from app.llm import LlmReply, ToolCall
from app.main import create_app
from app.state import RentalOffer
from tests.conftest import CRM_CONV_ID, CRM_URL, FakeLLM, make_ctx, make_settings

LAB_IDENTITY = "5490000000003"
KEY = "test-key"

RETRO_416 = {
    "etiqueta": "Retroexcavadora 416E, sáb 12 al dom 13 sept (2 días), 8 hs/día",
    "precio_total_sin_iva": "$2.822.560",
}


def test_si_nombra_la_maquina_tomada_pasa_intacta():
    texto = "Listo, te dejé tomada la 416E el sábado y el domingo. Un asesor te la confirma."
    assert confirm_booking(texto, RETRO_416) == texto


def test_si_nombra_otra_maquina_sale_la_confirmacion_armada():
    out = confirm_booking("La retroexcavadora 406 quedó TOMADA para el 12 y 13.", RETRO_416)
    assert "416E" in out
    assert "406" not in out
    assert "$2.822.560 + IVA" in out
    assert "confirmada" not in out.lower()


def test_si_no_nombra_ninguna_maquina_tambien_se_arma():
    out = confirm_booking("Listo, te la dejé tomada para el finde.", RETRO_416)
    assert out.startswith("Listo, te la dejé tomada: Retroexcavadora 416E")


def test_sin_texto_igual_le_llega_la_confirmacion():
    assert "416E" in confirm_booking("", RETRO_416)


def test_una_grua_alcanza_con_nombrar_su_modelo():
    """Nadie dice "Ford Cargo 1722": dice "la grúa N35000", y eso ya la identifica."""
    grua = {
        "etiqueta": "Ford Cargo 1722 - Grúa N35000, lun 5 al mar 6 oct (2 días), 8 hs/día",
        "precio_total_sin_iva": "$4.344.096",
    }
    texto = "Te dejé tomada la grúa N35000 el lunes y el martes."
    assert confirm_booking(texto, grua) == texto


def test_si_la_movio_lo_dice():
    assert confirm_booking("", {**RETRO_416, "movida": True}).startswith("Listo, te la moví")


def test_sin_reserva_en_el_turno_no_toca_nada():
    texto = "¿Para qué obra la necesitás?"
    assert confirm_booking(texto, None) == texto


# --------------------------------------------------- por la puerta real ---
# Los tests de arriba fijan la función; este fija que ESTÉ CONECTADA en turn.py.


def _context_payload() -> dict:
    return {
        "contact": {"id": "ct_1", "name": "[Prueba] Retro", "ficha": {}},
        "conversation": {"id": CRM_CONV_ID, "aiEnabled": True, "windowOpen": True},
        "lead": None,
        "ad": None,
    }


@pytest.mark.anyio
async def test_al_lead_le_llega_la_maquina_que_se_tomo(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="crear_reserva_tentativa",
                        arguments={"oferta_id": "roff_416", "fechas_confirmadas": "dale, la 1"},
                    )
                ],
            ),
            LlmReply(content="Listo, la retroexcavadora 406 quedó TOMADA. Un asesor te confirma."),
        ]
    )
    ctx = make_ctx(settings=make_settings(allowed_wa_ids=""), llm=llm)
    ctx.inventory_enabled = True
    conv = await ctx.store.get_or_create_conversation(LAB_IDENTITY)
    await ctx.store.replace_rental_offers(
        conv.id,
        [
            RentalOffer(
                conversation_id=conv.id,
                offer_id="roff_416",
                model_id="mmod_416",
                label=RETRO_416["etiqueta"],
                desde="2026-09-12",
                hasta="2026-09-14",
                amount_cents=282_256_000,
            )
        ],
    )
    app = create_app(ctx=ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload())
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
        return_value=httpx.Response(200, json={})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            201,
            json={
                "reserva": {
                    "reservaId": "rent_1",
                    "estado": "tentativa",
                    "desde": "2026-09-12",
                    "hasta": "2026-09-14",
                    "horasPorDia": 8,
                    "montoCotizadoCents": 282_256_000,
                }
            },
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}})
    )
    enviados = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        r = await c.post(
            "/lab/turn",
            json={
                "crm_conversation_id": CRM_CONV_ID,
                "identity": LAB_IDENTITY,
                "text": "dale, la 1",
                "reset": False,
            },
            headers={"x-api-key": KEY},
        )
    await ctx.crm.aclose()

    reply = r.json()["reply"]
    assert "416E" in reply and "406" not in reply
    texto = json.loads(enviados.calls[-1].request.content)["text"]
    assert "416E" in texto and "406" not in texto
