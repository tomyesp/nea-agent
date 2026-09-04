"""017 Fase 7 — La puerta del Laboratorio (`POST /lab/turn`).

Lo que se fija acá son las tres propiedades que hacen que este endpoint no sea
un agujero: pide la API key, resuelve la conversación por ID (nunca por
identidad, que es el camino que el CRM le cierra a las conversaciones de
prueba) y no puede saltarse los gates de negocio. Más la razón de existir: que
devuelva la traza de herramientas, sin la cual el juez del Lab no puede
distinguir un precio cotizado de uno inventado.
"""
from __future__ import annotations

import httpx
import pytest

from app.llm import LlmReply, ToolCall
from app.main import create_app
from tests.conftest import CRM_CONV_ID, CRM_URL, FakeLLM, make_ctx, make_settings

LAB_IDENTITY = "5490000000001"
KEY = "test-key"


def _context_payload(ai_enabled: bool = True, window_open: bool = True) -> dict:
    return {
        "contact": {"id": "ct_lab", "name": "[Prueba] Fechas ocupadas", "ficha": {}},
        "conversation": {
            "id": CRM_CONV_ID,
            "aiEnabled": ai_enabled,
            "windowOpen": window_open,
        },
        "lead": None,
        "ad": None,
    }


@pytest.fixture
async def lab_client():
    # Allowlist NO vacía y sin el teléfono de la persona: el Lab tiene que
    # pasar igual (su autorización es la API key, no la allowlist).
    ctx = make_ctx(settings=make_settings(allowed_wa_ids="5493511111111"))
    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        yield c, ctx
    await ctx.crm.aclose()


def _body(text: str = "necesito una retro", reset: bool = True) -> dict:
    return {
        "crm_conversation_id": CRM_CONV_ID,
        "identity": LAB_IDENTITY,
        "text": text,
        "reset": reset,
    }


async def test_sin_api_key_no_hay_laboratorio(lab_client):
    client, _ = lab_client
    resp = await client.post("/lab/turn", json=_body())
    assert resp.status_code == 401


async def test_api_key_incorrecta_401(lab_client):
    client, _ = lab_client
    resp = await client.post(
        "/lab/turn", json=_body(), headers={"x-api-key": "otra-cosa"}
    )
    assert resp.status_code == 401


async def test_resuelve_la_conversacion_por_id_no_por_identidad(
    lab_client, respx_mock
):
    """El CRM se niega a resolver conversaciones de prueba por identidad. Si
    Nea preguntara por identidad, el Lab nunca obtendría contexto y todas las
    personas quedarían mudas."""
    client, _ = lab_client
    ruta = respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload())
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
        return_value=httpx.Response(200, json={})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )

    resp = await client.post("/lab/turn", json=_body(), headers={"x-api-key": KEY})
    assert resp.status_code == 200
    query = dict(ruta.calls[0].request.url.params)
    assert query == {"conversationId": CRM_CONV_ID}
    assert "waIdentity" not in query


async def test_la_allowlist_no_bloquea_al_laboratorio(lab_client, respx_mock):
    client, _ = lab_client
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload())
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
        return_value=httpx.Response(200, json={})
    )
    envio = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )

    resp = await client.post("/lab/turn", json=_body(), headers={"x-api-key": KEY})
    assert resp.status_code == 200
    assert resp.json()["silencio"] is None
    assert envio.called


async def test_devuelve_la_traza_de_herramientas(respx_mock):
    """La razón de ser del endpoint: el juez necesita saber de dónde salió cada
    dato, no solo qué dijo el agente."""
    llm = FakeLLM(
        replies=[
            LlmReply(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="tc_1",
                        name="buscar_maquinas",
                        arguments={"consulta": "retroexcavadora"},
                    )
                ],
            ),
            LlmReply(content="Tengo la JCB 3CX disponible."),
        ]
    )
    ctx = make_ctx(llm=llm)
    ctx.inventory_enabled = True
    app = create_app(ctx=ctx)
    transport = httpx.ASGITransport(app=app)

    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload())
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
        return_value=httpx.Response(200, json={})
    )
    respx_mock.get(f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelos": [
                    {"id": "mmod_1", "nombre": "Retroexcavadora JCB 3CX"}
                ]
            },
        )
    )
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        resp = await c.post("/lab/turn", json=_body(), headers={"x-api-key": KEY})
    await ctx.crm.aclose()

    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Tengo la JCB 3CX disponible."
    assert [t["herramienta"] for t in data["tools"]] == ["buscar_maquinas"]
    assert data["tools"][0]["argumentos"] == {"consulta": "retroexcavadora"}
    # El resultado viaja entero: comprimirlo es tarea del CRM, no de Nea.
    assert data["tools"][0]["resultado"]


async def test_un_handoff_activo_silencia_tambien_en_el_laboratorio(
    lab_client, respx_mock
):
    """El gate no se salta por ser una prueba: es JUSTO lo que el Lab evalúa
    cuando una persona pide un humano."""
    client, _ = lab_client
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload(ai_enabled=False))
    )
    envio = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "no-deberia"})
    )

    resp = await client.post("/lab/turn", json=_body(), headers={"x-api-key": KEY})
    assert resp.status_code == 200
    assert resp.json()["silencio"] == "ia_pausada"
    assert resp.json()["reply"] is None
    assert not envio.called


async def test_reset_borra_la_memoria_de_la_corrida_anterior(lab_client, respx_mock):
    """Sin esto, el agente arranca la persona de hoy 'recordando' la obra que
    el guion de ayer inventó, y el resultado deja de ser reproducible."""
    client, ctx = lab_client
    conv = await ctx.store.get_or_create_conversation(LAB_IDENTITY)
    await ctx.store.add_message(conv.id, "user", "obra vieja en Alta Gracia")

    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload())
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
        return_value=httpx.Response(200, json={})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )

    await client.post("/lab/turn", json=_body(), headers={"x-api-key": KEY})

    historial = [m.content for m in await ctx.store.recent_messages(conv.id, 20)]
    assert "obra vieja en Alta Gracia" not in historial
