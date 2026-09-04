"""Contrato con el motor de inventario de Vocero (017, fork RPM).

Reemplaza a test_agenda_vocero.py: este fork no agenda citas, alquila
máquinas. Lo que se fija acá es el acople con el CRM — que la conversación
viaje siempre (sin ella no hay nada reservable), que la sonda de capacidad
distinga "apagado" de "encendido", y que un CRM sin la bandera INVENTARIO
degrade a un agente honesto en vez de a uno que promete máquinas.
"""
from __future__ import annotations

import httpx
import pytest

from app.crm import CrmClient, InventoryUnavailable
from app.state import RentalOffer
from app.tools import INVENTORY_TOOLS, ToolRuntime, tool_schemas
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx

MODELO_ID = "mmod_retro3cx"
OFERTA_ID = "roff_abc123"


@pytest.fixture
async def runtime_y_ctx():
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.replace_rental_offers(
        conv.id,
        [
            RentalOffer(
                conversation_id=conv.id,
                offer_id=OFERTA_ID,
                model_id=MODELO_ID,
                label="Retroexcavadora JCB 3CX, 5 oct al 12 oct",
                desde="2026-10-05",
                hasta="2026-10-12",
                amount_cents=139_150_000,
            )
        ],
    )
    yield ToolRuntime(ctx, conv, CRM_CONV_ID), ctx, conv
    await ctx.crm.aclose()


async def test_disponibilidad_manda_la_conversacion(runtime_y_ctx, respx_mock):
    """Sin `conversationId` el CRM responde 422 y no emite ninguna oferta: sin
    eso, nada sería reservable después."""
    runtime, ctx, conv = runtime_y_ctx
    route = respx_mock.get(f"{CRM_URL}/api/bot/disponibilidad").mock(
        return_value=httpx.Response(200, json={"disponible": True, "ofertas": []})
    )
    await runtime.execute(
        "consultar_disponibilidad",
        {"modelo_id": MODELO_ID, "desde": "2026-10-05", "hasta": "2026-10-12"},
    )
    params = route.calls[0].request.url.params
    assert params["conversationId"] == CRM_CONV_ID
    assert params["modeloId"] == MODELO_ID
    assert params["desde"] == "2026-10-05"
    assert params["hasta"] == "2026-10-12"


async def test_sonda_de_capacidad_distingue_apagado_de_encendido(respx_mock):
    """404 = la instancia no tiene inventario; 422 = lo tiene y quiere params.

    Se pregunta SIN parámetros a propósito: así no se ensucia la oferta de
    ninguna conversación y no hace falta un endpoint nuevo del CRM.
    """
    crm = CrmClient(CRM_URL, "k")
    route = respx_mock.get(f"{CRM_URL}/api/bot/disponibilidad")

    route.mock(return_value=httpx.Response(404))
    assert await crm.inventory_available() is False

    route.mock(return_value=httpx.Response(422, json={"error": {"code": "invalid_body"}}))
    assert await crm.inventory_available() is True
    await crm.aclose()


async def test_sonda_ante_red_caida_asume_que_si_hay(respx_mock):
    """Fallar hacia "sí" cuesta un intento que ya degrada solo; fallar hacia
    "no" le apagaría el catálogo a una instancia que sí lo tiene."""
    crm = CrmClient(CRM_URL, "k")
    respx_mock.get(f"{CRM_URL}/api/bot/disponibilidad").mock(
        side_effect=httpx.ConnectError("sin red")
    )
    assert await crm.inventory_available() is True
    await crm.aclose()


async def test_catalogo_404_levanta_inventory_unavailable(respx_mock):
    crm = CrmClient(CRM_URL, "k")
    respx_mock.get(f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(InventoryUnavailable):
        await crm.get_catalogo()
    await crm.aclose()


async def test_sin_inventario_el_modelo_no_ve_las_tools_de_maquinaria():
    """Que la herramienta no exista es más claro que pedirle al prompt que se
    acuerde de no usarla: si se le enseñan, las llama y fallan todas."""
    con = {t["function"]["name"] for t in tool_schemas(True)}
    sin = {t["function"]["name"] for t in tool_schemas(False)}
    assert INVENTORY_TOOLS <= con
    assert not (INVENTORY_TOOLS & sin)
    # Lo que NO depende del inventario sigue disponible en ambos casos.
    assert {"update_ficha", "route_out", "handoff"} <= sin


async def test_reserva_404_apaga_el_inventario_en_caliente(runtime_y_ctx, respx_mock):
    """Si el CRM apagó la bandera después del arranque, Nea se entera y deja
    de prometer máquinas en vez de reintentar contra una puerta cerrada."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(404)
    )
    result = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "dale"},
    )
    assert result["error"] == "sin_inventario"
    assert ctx.inventory_enabled is False


async def test_segunda_reserva_manda_a_mover_no_a_ofrecer_alternativas(
    runtime_y_ctx, respx_mock
):
    """017 Fase 7 (bis) — El lead que corre las fechas de su obra.

    Lo encontró el Laboratorio (persona `cambia_de_idea`): el agente le tomaba
    la máquina, el lead movía la obra dos días, y el agente le contestaba que
    estaba OCUPADA. Lo estaba: por la reserva del propio lead. El CRM ahora
    rechaza la segunda con `ya_tiene_reserva` y adjunta la que ya existe, para
    que el agente sepa QUÉ mover en vez de salir a ofrecer alternativas que no
    hacen falta.
    """
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {
                    "code": "ya_tiene_reserva",
                    "message": "Esta conversación ya tiene una reserva tentativa",
                },
                "reservaExistente": {
                    "reservaId": "rent_1",
                    "estado": "tentativa",
                    "desde": "2026-10-05",
                    "hasta": "2026-10-12",
                    "montoCotizadoCents": 139_150_000,
                },
            },
        )
    )

    out = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "dale, del 7 al 14"},
    )

    assert out["ok"] is False
    assert out["error"] == "ya_tiene_reserva"
    # La reserva que ya tiene viaja entera: sin ella el agente sabe que falló
    # pero no qué mover.
    assert out["reserva_actual"]["reservaId"] == "rent_1"
    assert "cambiar_reserva_tentativa" in out["detalle"]
    # Y no quedó marcada como reservada: no se creó nada.
    assert runtime.booked is False


async def test_tras_reservar_un_cambio_de_fechas_no_es_handoff(
    runtime_y_ctx, respx_mock
):
    """La instrucción que devolvía la reserva mandaba a handoff ante cualquier
    cambio, contradiciendo a `cambiar_reserva_tentativa`, que existe justo para
    eso. Cancelar sí es de una persona; correr las fechas, no."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            201,
            json={
                "reserva": {
                    "reservaId": "rent_1",
                    "estado": "tentativa",
                    "desde": "2026-10-05",
                    "hasta": "2026-10-12",
                    "montoCotizadoCents": 139_150_000,
                }
            },
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={})
    )

    out = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "dale"},
    )

    instrucciones = out["instrucciones"]
    assert "cambiar_reserva_tentativa" in instrucciones
    assert "CANCELAR" in instrucciones
    # Y la promesa de entrega que el Lab cazó en la persona `apurado`.
    assert "no le prometas hora ni lugar de entrega" in instrucciones
