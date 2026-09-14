"""017 — Una reserva que no se hizo no se le afirma al lead.

Pasó en vivo con Qwen 3.8 Flash (2026-09-14): el lead dijo "me quedo con la
406, tomala" y el modelo contestó "Listo, te la dejé tomada" sin llamar
crear_reserva_tentativa. En el calendario no quedó nada.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.booking_guard import afirma_reserva, confirmacion, pregunta_segura
from app.llm import LlmExhausted, LlmReply, ToolCall
from app.main import create_app
from app.state import RentalOffer
from tests.conftest import CRM_CONV_ID, CRM_URL, FakeLLM, make_ctx, make_settings

LAB_IDENTITY = "5490000000004"
KEY = "test-key"
ETIQUETA = "Retroexcavadora 406, sáb 19 al dom 20 sept (2 días), 8 hs/día"


# ------------------------------------------------------------- detector ---


@pytest.mark.parametrize(
    "texto",
    [
        "Listo, te la dejé tomada: Retroexcavadora 406, sáb 19 y dom 20.",
        "Listo, te la deje tomada para el finde 👍",
        "Quedó tomada a tu nombre, solo falta que un asesor la confirme.",
        "Ya la tenés tomada, no hay nada más que confirmar de mi lado.",
        "Te la reservé para el sábado y el domingo.",
        "Sí, te la dejé tomada. Un asesor te escribe.",
        "No hay drama, te la dejé tomada.",
        "Te la dejé tomada, ¿te sirve algo más?",
        "Tu reserva ya quedó registrada.",
    ],
)
def test_detecta_la_reserva_dada_por_hecha(texto):
    assert afirma_reserva(texto) == "reserva"


@pytest.mark.parametrize(
    "texto",
    [
        "Listo, te la moví al lunes 21 y martes 22.",
        "Quedó movida para el 21. Un asesor te confirma.",
    ],
)
def test_detecta_el_cambio_dado_por_hecho(texto):
    assert afirma_reserva(texto) == "movida"


@pytest.mark.parametrize(
    "texto",
    [
        # Ofrecer es la venta correcta: no puede disparar la guarda.
        "¿Te la dejo tomada del sáb 19 al dom 20 de septiembre, 8 hs por día?",
        "Está libre. ¿Te la dejo tomada por ese fin de semana?",
        "¿Querés que te la deje tomada?",
        "Querés que te la deje tomada?",
        "Si me confirmás, queda tomada hasta que un asesor la vea.",
        "Cuando me digas que sí, te la dejé tomada al toque.",
        "Todavía no te la dejé tomada: confirmame las fechas.",
        # La máquina de otro es disponibilidad, no una venta.
        "La 416E quedó tomada por otro cliente esos días, pero la 406 está libre.",
        "¿Para qué obra la necesitás?",
        "",
        None,
    ],
)
def test_no_confunde_ofrecer_ni_condicionar_con_afirmar(texto):
    assert afirma_reserva(texto) is None


def test_las_preguntas_armadas_no_se_disparan_a_si_mismas():
    una = [(ETIQUETA, "$2.822.560")]
    dos = una + [("Retroexcavadora 416E, sáb 19 al dom 20 sept (2 días), 8 hs/día", "$2.822.560")]
    for afirmacion in ("reserva", "movida"):
        for ofertas in ([], una, dos):
            assert afirma_reserva(pregunta_segura(afirmacion, ofertas)) is None


def test_la_confirmacion_real_si_se_lee_como_reserva():
    """Sin esto la guarda no protege nada: la confirmación armada afirma."""
    assert afirma_reserva(confirmacion({"etiqueta": ETIQUETA})) == "reserva"


def test_la_pregunta_nombra_la_oferta_vigente_con_su_precio():
    out = pregunta_segura("reserva", [(ETIQUETA, "$2.822.560")])
    assert ETIQUETA in out and "$2.822.560 + IVA" in out and out.rstrip().endswith("?")


# ------------------------------------------------------ por la puerta real ---


def _context(reserva_activa: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "contact": {"id": "ct_1", "name": "[Prueba] Retro", "ficha": {}},
        "conversation": {"id": CRM_CONV_ID, "aiEnabled": True, "windowOpen": True},
        "lead": None,
        "ad": None,
        "reservaActiva": reserva_activa,
    }


RESERVA_ACTIVA = {
    "reservaId": "rent_1",
    "estado": "tentativa",
    "modeloId": "mmod_406",
    "maquina": "Retroexcavadora 406",
    "etiqueta": ETIQUETA,
    "desde": "2026-09-19",
    "hasta": "2026-09-21",
    "horasPorDia": 8,
    "montoCotizadoCents": 282_256_000,
}


class _LlmQueSeCae(FakeLLM):
    """Contesta la cola y, cuando se vacía, se agota."""

    async def complete(self, messages, tools=None):
        if not self.replies:
            self.calls.append({"messages": messages, "tools": tools})
            raise LlmExhausted("sin respuesta")
        return await super().complete(messages, tools)


async def _turno(respx_mock, llm: FakeLLM, texto: str, reserva_activa=None):
    ctx = make_ctx(settings=make_settings(allowed_wa_ids=""), llm=llm)
    ctx.inventory_enabled = True
    conv = await ctx.store.get_or_create_conversation(LAB_IDENTITY)
    await ctx.store.replace_rental_offers(
        conv.id,
        [
            RentalOffer(
                conversation_id=conv.id,
                offer_id="roff_406",
                model_id="mmod_406",
                label=ETIQUETA,
                desde="2026-09-19",
                hasta="2026-09-21",
                amount_cents=282_256_000,
            )
        ],
    )
    app = create_app(ctx=ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context(reserva_activa))
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(return_value=httpx.Response(200, json={}))
    reservas = respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            201,
            json={
                "reserva": {
                    "reservaId": "rent_1",
                    "estado": "tentativa",
                    "desde": "2026-09-19",
                    "hasta": "2026-09-21",
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
    respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(return_value=httpx.Response(200, json={}))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        r = await c.post(
            "/lab/turn",
            json={
                "crm_conversation_id": CRM_CONV_ID,
                "identity": LAB_IDENTITY,
                "text": texto,
                "reset": False,
            },
            headers={"x-api-key": KEY},
        )
    await ctx.crm.aclose()
    textos = [json.loads(call.request.content)["text"] for call in enviados.calls]
    return r.json(), reservas, textos


def _alertas(llm: FakeLLM) -> list[str]:
    return [
        m["content"]
        for m in llm.calls[-1]["messages"]
        if m["role"] == "system" and "NO se envió" in str(m["content"])
    ]


RESERVAR = LlmReply(
    content=None,
    tool_calls=[
        ToolCall(
            id="call_1",
            name="crear_reserva_tentativa",
            arguments={"oferta_id": "roff_406", "fechas_confirmadas": "me quedo con la 406, tomala"},
        )
    ],
)


async def test_si_afirma_sin_reservar_se_le_avisa_y_reserva(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(content="Listo, te la dejé tomada: Retroexcavadora 406, sáb 19 y dom 20."),
            RESERVAR,
            LlmReply(content="Te dejé tomada la 406 el sáb 19 y dom 20. Un asesor te confirma."),
        ]
    )
    body, reservas, textos = await _turno(respx_mock, llm, "me quedo con la 406, tomala")

    assert reservas.call_count == 1
    assert textos == ["Te dejé tomada la 406 el sáb 19 y dom 20. Un asesor te confirma."]
    alerta = _alertas(llm)
    assert len(alerta) == 1 and "te la dejé tomada" in alerta[0]


async def test_si_insiste_sale_la_pregunta_y_no_la_mentira(respx_mock):
    falsa = LlmReply(content="Listo, te la dejé tomada 👍")
    llm = FakeLLM(replies=[falsa, falsa])
    body, reservas, textos = await _turno(respx_mock, llm, "me quedo con la 406, tomala")

    assert reservas.call_count == 0
    assert len(textos) == 1
    assert "dejé tomada" not in textos[0]
    assert ETIQUETA in textos[0] and textos[0].rstrip().endswith("?")
    assert len(llm.calls) == 2


async def test_si_se_corrige_sale_la_correccion(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(content="Listo, te la dejé tomada."),
            LlmReply(content="¿Te la dejo tomada del sáb 19 al dom 20, 8 hs por día, $2.822.560 + IVA?"),
        ]
    )
    body, reservas, textos = await _turno(respx_mock, llm, "me quedo con la 406")

    assert reservas.call_count == 0
    assert textos == ["¿Te la dejo tomada del sáb 19 al dom 20, 8 hs por día, $2.822.560 + IVA?"]


async def test_si_la_segunda_vuelta_se_cae_igual_sale_la_pregunta(respx_mock):
    llm = _LlmQueSeCae(replies=[LlmReply(content="Listo, te la dejé tomada.")])
    body, reservas, textos = await _turno(respx_mock, llm, "tomala")

    assert reservas.call_count == 0
    assert len(textos) == 1 and ETIQUETA in textos[0]
    assert body["handoff"] is None


async def test_si_ya_la_tenia_tomada_decirlo_es_verdad(respx_mock):
    llm = FakeLLM(replies=[LlmReply(content="Ya la tenés tomada, un asesor te escribe.")])
    body, reservas, textos = await _turno(
        respx_mock, llm, "sí, dale", reserva_activa=RESERVA_ACTIVA
    )

    assert textos == ["Ya la tenés tomada, un asesor te escribe."]
    assert len(llm.calls) == 1
    assert _alertas(llm) == []


async def test_un_cambio_que_no_se_hizo_no_se_afirma_aunque_tenga_reserva(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(content="Listo, te la moví al lunes 21 y martes 22."),
            LlmReply(content="Para moverla necesito ver si está libre: ¿lun 21 y mar 22, 8 hs por día?"),
        ]
    )
    body, reservas, textos = await _turno(
        respx_mock, llm, "pasala al lunes 21", reserva_activa=RESERVA_ACTIVA
    )

    assert textos == ["Para moverla necesito ver si está libre: ¿lun 21 y mar 22, 8 hs por día?"]
    alerta = _alertas(llm)
    assert len(alerta) == 1 and "cambiar_reserva_tentativa" in alerta[0]


async def test_una_respuesta_normal_no_paga_ninguna_vuelta_extra(respx_mock):
    llm = FakeLLM(replies=[LlmReply(content="Está libre. ¿Te la dejo tomada por ese finde?")])
    body, reservas, textos = await _turno(respx_mock, llm, "¿está libre la 406?")

    assert textos == ["Está libre. ¿Te la dejo tomada por ese finde?"]
    assert len(llm.calls) == 1
