"""017 Fase 7 (bis) — Lo que el agente no decide, se escala SIEMPRE.

Lo encontró el Laboratorio con la persona `regateador`: el agente escribió "eso
lo ve un asesor y te contesta enseguida" DOS veces y siguió vendiendo, sin
llamar la herramienta ni una vez. El dueño nunca se enteró de que había alguien
pidiendo descuento y el lead quedó esperando una respuesta que no iba a llegar.

El detector mira el mensaje del LEAD, no la respuesta del agente. La tentación
era buscar la frase de escalada en lo que escribe el agente, pero "un asesor te
confirma la reserva a la brevedad" —la frase CORRECTA de cada venta exitosa— y
"eso lo ve un asesor" se parecen demasiado: confundirlas mandaría a un humano
cada reserva que sale bien.
"""
from __future__ import annotations

import httpx
import pytest

from app.escalation import needs_human
from app.llm import LlmReply
from app.main import create_app
from tests.conftest import CRM_CONV_ID, CRM_URL, FakeLLM, make_ctx, make_settings

LAB_IDENTITY = "5490000000002"
KEY = "test-key"


# --------------------------------------------------------------- el léxico ---


@pytest.mark.parametrize(
    "texto,motivo",
    [
        ("en Villa María me la dejan un 20% más barata, igualame el precio", "descuento"),
        ("dale, hacete el favor, somos clientes grandes, un 15% aunque sea", "descuento"),
        ("me hacen algún descuento por la semana completa?", "descuento"),
        ("buen día, consulta: facturan A?", "facturacion"),
        ("lo puedo pagar en cuotas?", "facturacion"),
        ("necesito saber si el seguro de la máquina lo cubren ustedes", "seguro"),
        ("quién se hace cargo si se rompe?", "seguro"),
        ("la última máquina que me mandaron era un desastre, perdí dos días de obra", "reclamo"),
        ("me cobraron igual los días que estuvo parada", "reclamo"),
    ],
)
def test_pedidos_que_decide_una_persona(texto: str, motivo: str):
    assert needs_human(texto) == motivo


@pytest.mark.parametrize(
    "texto",
    [
        # Pedir precio NO es negociar: es el 90% de los leads.
        "hola, cuánto sale una minicargadora por una semana?",
        "qué precio tiene la retro por 4 días?",
        "uh, está caro eso",
        # "seguro" como muletilla, no como cobertura.
        "seguro que sí, mandame los datos",
        "estoy seguro de que la necesito el lunes",
        # Porcentajes de obra, que en este rubro se hablan todo el tiempo.
        "el terreno tiene 20% de pendiente",
        "llevamos 30% de avance en la obra",
        # Conversación normal de venta.
        "dale, dejámela tomada esa",
        "necesito una retro para hacer zanjas en Alta Gracia",
        "perfecto, entonces quedamos con esas fechas nuevas",
        "perdí el teléfono, mandame de nuevo el precio",
    ],
)
def test_no_dispara_con_conversacion_normal(texto: str):
    """Un falso positivo acá manda a un humano una venta que iba bien. El
    léxico es estrecho a propósito: ante la duda no dispara, y el agente igual
    puede llamar handoff por su cuenta."""
    assert needs_human(texto) is None


# ------------------------------------------------------------- en el turno ---


def _context_payload() -> dict:
    return {
        "contact": {"id": "ct_1", "name": "[Prueba] Regateador", "ficha": {}},
        "conversation": {"id": CRM_CONV_ID, "aiEnabled": True, "windowOpen": True},
        "lead": None,
        "ad": None,
    }


async def _turno(llm: FakeLLM, texto: str, respx_mock):
    """Un turno por la puerta del Laboratorio, que es síncrona."""
    ctx = make_ctx(settings=make_settings(allowed_wa_ids=""), llm=llm)
    ctx.inventory_enabled = True
    app = create_app(ctx=ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload())
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
        return_value=httpx.Response(200, json={})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )
    handoff = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        resp = await c.post(
            "/lab/turn",
            json={
                "crm_conversation_id": CRM_CONV_ID,
                "identity": LAB_IDENTITY,
                "text": texto,
                "reset": True,
            },
            headers={"x-api-key": KEY},
        )
    await ctx.crm.aclose()
    return resp.json(), handoff, ctx


async def test_el_handoff_sucede_aunque_el_modelo_no_lo_llame(respx_mock):
    """El caso exacto del Laboratorio: el modelo escribe la línea correcta y no
    llama la herramienta. Antes eso dejaba la conversación viva y al dueño sin
    enterarse."""
    llm = FakeLLM(replies=[LlmReply(content="Eso lo ve un asesor y te contesta enseguida.")])
    data, handoff, _ = await _turno(
        llm, "igualame el precio, un 20% más barata y cerramos ya", respx_mock
    )

    assert data["handoff"] == "cliente"
    assert handoff.called
    # El motivo que viaja al CRM tiene que estar en su catálogo cerrado.
    import json as _json

    assert _json.loads(handoff.calls[0].request.content)["reason"] == "cliente"


async def test_al_modelo_se_le_avisa_en_el_mismo_turno(respx_mock):
    """No alcanza con forzar el handoff por detrás: si al modelo no se le dice,
    escribe un pitch de venta y el handoff queda pegado a un mensaje que no
    corresponde."""
    llm = FakeLLM(replies=[LlmReply(content="Eso lo ve un asesor.")])
    await _turno(llm, "me hacen descuento por 3 semanas?", respx_mock)

    sistema = [
        m["content"]
        for m in llm.calls[0]["messages"]
        if m["role"] == "system" and "ALERTA DEL SISTEMA" in str(m["content"])
    ]
    assert len(sistema) == 1
    assert "descuento" in sistema[0]
    assert "handoff" in sistema[0]


async def test_una_venta_normal_no_escala(respx_mock):
    """El contrapeso: pedir precio y aceptar no puede terminar en handoff, o el
    agente deja de servir para vender."""
    llm = FakeLLM(replies=[LlmReply(content="La Bobcat S570 sale $786.500 la semana.")])
    data, handoff, _ = await _turno(
        llm, "hola, cuánto sale una minicargadora por una semana?", respx_mock
    )

    assert data["handoff"] is None
    assert not handoff.called


async def test_el_motivo_es_siempre_el_mismo_lo_llame_el_modelo_o_no(respx_mock):
    """El dueño mira la bandeja y ve un motivo por conversación. Si la misma
    situación aparece como `cliente` cuando la fuerza el backstop y como
    `modelo` cuando la llamó el LLM, el filtro por motivo deja de servir."""
    from app.llm import ToolCall

    llm = FakeLLM(
        replies=[
            LlmReply(
                content=None,
                tool_calls=[
                    ToolCall(id="t1", name="handoff", arguments={"reason": "duda de precio"})
                ],
            ),
            LlmReply(content="Eso lo ve un asesor y te contesta enseguida."),
        ]
    )
    data, handoff, _ = await _turno(llm, "me hacés un descuento del 20%?", respx_mock)

    # El modelo SÍ llamó la herramienta (con su propio motivo libre), y aun así
    # al CRM llega el motivo canónico de esta situación.
    assert data["handoff"] == "cliente"
    import json as _json

    assert _json.loads(handoff.calls[0].request.content)["reason"] == "cliente"


async def test_la_hostilidad_le_gana_al_pedido_de_descuento(respx_mock):
    """Un lead que insulta Y regatea: al dueño le sirve más ver 'hostilidad',
    que es la señal urgente."""
    llm = FakeLLM(replies=[LlmReply(content="Cierro acá.")])
    ctx = make_ctx(settings=make_settings(allowed_wa_ids=""), llm=llm)
    conv = await ctx.store.get_or_create_conversation(LAB_IDENTITY)
    # Dos strikes previos: el mensaje del turno es el tercero.
    for texto in ("son unos chantas", "ustedes son unos ladrones"):
        await ctx.store.add_message(conv.id, "user", texto)

    app = create_app(ctx=ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload())
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
        return_value=httpx.Response(200, json={})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m"})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        resp = await c.post(
            "/lab/turn",
            json={
                "crm_conversation_id": CRM_CONV_ID,
                "identity": LAB_IDENTITY,
                "text": "sos un garca, igualame el precio un 20% o me voy",
                "reset": False,
            },
            headers={"x-api-key": KEY},
        )
    await ctx.crm.aclose()
    assert resp.json()["handoff"] == "hostilidad"
