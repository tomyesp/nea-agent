"""017 — El texto que el modelo escribe junto a una herramienta también sale.

Pasó en vivo (2026-09-14): el modelo escribió el saludo y en la misma vuelta
guardó la ficha. El saludo se descartaba y al lead le llegaba "Quedo a la
espera de tu respuesta." — o, con Haiku, nada: tres respuestas vacías y
handoff por `error`.
"""
from __future__ import annotations

import json

from app.llm import LlmExhausted, LlmReply, ToolCall
from app.state import InboundMessage
from app.turn import run_turn
from tests.conftest import IDENTITY, FakeLLM, make_ctx, mock_crm_basics

SALUDO = "¡Hola! Soy Pericles, de RPM Construcciones. Contame: ¿qué trabajo tenés que hacer?"


class _LlmQueSeCae(FakeLLM):
    """Contesta la cola y, cuando se vacía, se agota."""

    async def complete(self, messages, tools=None):
        if not self.replies:
            self.calls.append({"messages": messages, "tools": tools})
            raise LlmExhausted("respuesta vacía del LLM (sin content ni tools)")
        return await super().complete(messages, tools)


async def _turno(respx_mock, llm: FakeLLM, texto: str = "buenas me gustaria alquilar una maquina"):
    ctx = make_ctx(llm=llm)
    routes = mock_crm_basics(respx_mock)
    result = await run_turn(
        ctx,
        IDENTITY,
        [InboundMessage(wa_message_id="wamid.1", identity=IDENTITY, type="text", text=texto)],
    )
    await ctx.crm.aclose()
    enviados = [json.loads(c.request.content)["text"] for c in routes["messages"].calls]
    return result, routes, enviados


async def test_el_saludo_escrito_junto_a_update_ficha_es_el_que_llega(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(
                content=SALUDO,
                tool_calls=[ToolCall(id="c1", name="update_ficha", arguments={"notas": "quiere alquilar"})],
            ),
            LlmReply(content="Quedo a la espera de tu respuesta."),
        ]
    )
    result, routes, enviados = await _turno(respx_mock, llm)

    assert enviados == [SALUDO]
    assert routes["ficha"].called
    # Sin vuelta extra: la ficha no le da nada nuevo que decir.
    assert len(llm.calls) == 1
    assert result.handoff is None


async def test_la_despedida_escrita_junto_al_handoff_sale_y_despues_se_pasa(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(
                content="Eso lo ve un asesor, te paso con el equipo.",
                tool_calls=[ToolCall(id="c1", name="handoff", arguments={"reason": "cliente"})],
            ),
        ]
    )
    result, routes, enviados = await _turno(respx_mock, llm, "quiero hablar con una persona")

    assert enviados == ["Eso lo ve un asesor, te paso con el equipo."]
    assert routes["handoff"].called
    assert len(llm.calls) == 1


async def test_si_la_herramienta_informa_llega_la_respuesta_escrita_con_el_resultado(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(
                content="Dejame ver qué te puedo pasar.",
                tool_calls=[ToolCall(id="c1", name="route_out", arguments={})],
            ),
            LlmReply(content="Por ahora no es algo que hagamos, pero cualquier cosa escribime."),
        ]
    )
    result, routes, enviados = await _turno(respx_mock, llm, "busco trabajo de operario")

    assert enviados == ["Por ahora no es algo que hagamos, pero cualquier cosa escribime."]
    assert len(llm.calls) == 2


async def test_si_despues_de_informar_el_modelo_no_contesta_sale_lo_que_ya_escribio(respx_mock):
    llm = _LlmQueSeCae(
        replies=[
            LlmReply(
                content="Por ahora no es algo que hagamos, pero cualquier cosa escribime.",
                tool_calls=[ToolCall(id="c1", name="route_out", arguments={})],
            ),
        ]
    )
    result, routes, enviados = await _turno(respx_mock, llm, "busco trabajo de operario")

    assert enviados == ["Por ahora no es algo que hagamos, pero cualquier cosa escribime."]
    # No es un fallo del modelo: ya había contestado. Nada de handoff `error`.
    assert not routes["handoff"].called
    assert result.handoff is None


async def test_sin_nada_escrito_un_modelo_que_no_contesta_sigue_siendo_error(respx_mock):
    llm = _LlmQueSeCae(
        replies=[
            LlmReply(
                content=None,
                tool_calls=[ToolCall(id="c1", name="update_ficha", arguments={"notas": "x"})],
            ),
        ]
    )
    result, routes, enviados = await _turno(respx_mock, llm)

    assert enviados == []
    assert result.handoff == "error"
