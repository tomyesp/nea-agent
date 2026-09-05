"""El agente no se presenta dos veces (app/greeting.py).

El caso que lo motivó salió de una corrida real contra el agente: pidió
descuento, el agente escaló bien… y pegó el saludo del chasis atrás de la
despedida. Lo que se fija acá es el corte, y sobre todo lo que NO se corta.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.greeting import strip_restart
from app.llm import LlmReply
from app.main import create_app
from tests.conftest import CRM_CONV_ID, CRM_URL, FakeLLM, make_ctx, make_settings

NOMBRE = "Nea"
LAB_IDENTITY = "5490000000002"
KEY = "test-key"


def test_corta_el_saludo_pegado_atras_de_la_despedida():
    """El caso real: cierre correcto + conversación empezada de nuevo."""
    texto = (
        "Lamento no poder igualar precios, eso lo ve un asesor y te contesta "
        "enseguida. Voy a pasar la conversación a un humano.\n"
        "¡Hola! Soy Nea, de RPM Construcciones 👷. ¿Para qué trabajo necesitás "
        "la máquina?"
    )
    out = strip_restart(texto, NOMBRE, already_greeted=True)
    assert out == (
        "Lamento no poder igualar precios, eso lo ve un asesor y te contesta "
        "enseguida. Voy a pasar la conversación a un humano."
    )


def test_corta_aunque_el_reinicio_venga_sin_hola():
    """Renglón nuevo + presentación es reinicio igual, sin interjección."""
    texto = "Listo, te paso con un asesor.\nSoy Nea y te ayudo con el alquiler."
    assert strip_restart(texto, NOMBRE, already_greeted=True) == (
        "Listo, te paso con un asesor."
    )


def test_corta_en_la_misma_linea_si_hay_saludo():
    texto = "Te la dejo tomada del 5 al 12. Hola, soy Nea, ¿en qué te ayudo?"
    assert strip_restart(texto, NOMBRE, already_greeted=True) == (
        "Te la dejo tomada del 5 al 12."
    )


def test_el_primer_contacto_no_se_toca():
    """Sin `already_greeted` el saludo es exactamente lo que corresponde."""
    texto = "¡Hola! Soy Nea, de RPM Construcciones 👷 ¿Qué obra tenés?"
    assert strip_restart(texto, NOMBRE, already_greeted=False) == texto


def test_contestar_quien_sos_no_es_un_reinicio():
    """Si el lead pregunta si es un bot, el agente lo confirma — y eso no
    lleva saludo ni abre renglón, así que no se toca."""
    texto = "Sí, te atiendo yo. Soy Nea, la asistente de RPM Construcciones."
    assert strip_restart(texto, NOMBRE, already_greeted=True) == texto


def test_un_saludo_al_principio_se_deja_entero():
    """Redundante, no roto: recortarlo se llevaría el contenido de atrás, que
    es el mensaje real."""
    texto = "¡Hola! Soy Nea. Te confirmo que la retro está libre esa semana."
    assert strip_restart(texto, NOMBRE, already_greeted=True) == texto


def test_un_mensaje_normal_pasa_intacto():
    texto = "Dale, te la dejo tomada del 5 al 12 de octubre, $1.792.000 + IVA."
    assert strip_restart(texto, NOMBRE, already_greeted=True) == texto


def test_el_nombre_del_agente_sale_del_perfil():
    """El negocio puede llamar a su agente de otra forma; el corte lo sigue."""
    texto = "Te paso con un asesor.\n¡Hola! Soy Vera, de Otra Empresa. ¿Qué necesitás?"
    assert strip_restart(texto, "Vera", already_greeted=True) == (
        "Te paso con un asesor."
    )
    # Y con el nombre equivocado no corta nada: no es SU presentación.
    assert strip_restart(texto, "Nea", already_greeted=True) == texto


def test_no_devuelve_vacio_ni_con_entradas_raras():
    assert strip_restart("", NOMBRE, already_greeted=True) == ""
    # Sin nombre configurado no hay presentación que reconocer.
    assert strip_restart("Hola, soy Nea. Algo", "", already_greeted=True) == (
        "Hola, soy Nea. Algo"
    )


def test_un_nombre_con_caracteres_de_regex_no_rompe():
    texto = "Listo.\n¡Hola! Soy A.J. y te ayudo."
    # El punto de "A.J." es literal, no un comodín: no puede hacer match con
    # cualquier cosa ni reventar la compilación.
    assert strip_restart(texto, "A.J.", already_greeted=True) == "Listo."


# --------------------------------------------------- por la puerta real ---
# Los tests de arriba fijan la función; este fija que ESTÉ CONECTADA. Sin él,
# borrar la llamada en turn.py deja el defecto suelto y la suite en verde.

#: Lo que el modelo escribió de verdad en una corrida contra el agente: la
#: línea de escalada correcta y, pegado abajo, el arranque del chasis.
REINICIO = (
    "Eso lo ve un asesor y te contesta enseguida.\n"
    "¡Hola! Soy Nea, de RPM Construcciones 👷. ¿Para qué trabajo necesitás la "
    "máquina?"
)


def _context_payload() -> dict:
    return {
        "contact": {"id": "ct_1", "name": "[Prueba] Regateador", "ficha": {}},
        "conversation": {"id": CRM_CONV_ID, "aiEnabled": True, "windowOpen": True},
        "lead": None,
        "ad": None,
    }


@pytest.mark.anyio
async def test_el_reinicio_no_le_llega_al_lead(respx_mock):
    """Dos turnos: en el primero saluda (correcto) y en el segundo el modelo
    vuelve a empezar. Lo que sale al CRM tiene que ser solo la despedida."""
    llm = FakeLLM(
        replies=[
            LlmReply(content="¡Hola! Soy Nea, de RPM Construcciones. ¿Qué obra tenés?"),
            LlmReply(content=REINICIO),
        ]
    )
    ctx = make_ctx(settings=make_settings(allowed_wa_ids=""), llm=llm)
    ctx.inventory_enabled = True
    app = create_app(ctx=ctx)
    respx_mock.get(f"{CRM_URL}/api/bot/context").mock(
        return_value=httpx.Response(200, json=_context_payload())
    )
    respx_mock.post(f"{CRM_URL}/api/bot/typing").mock(
        return_value=httpx.Response(200, json={})
    )
    enviados = respx_mock.post(f"{CRM_URL}/api/bot/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "msg_1"})
    )
    respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bot.test") as c:
        primera = await c.post(
            "/lab/turn",
            json={
                "crm_conversation_id": CRM_CONV_ID,
                "identity": LAB_IDENTITY,
                "text": "hola, necesito una minicargadora",
                "reset": True,
            },
            headers={"x-api-key": KEY},
        )
        segunda = await c.post(
            "/lab/turn",
            json={
                "crm_conversation_id": CRM_CONV_ID,
                "identity": LAB_IDENTITY,
                "text": "igualame el precio, un 20% más barata y cerramos ya",
                "reset": False,
            },
            headers={"x-api-key": KEY},
        )
    await ctx.crm.aclose()

    # El saludo del PRIMER contacto sale entero: ahí es lo correcto.
    assert "Soy Nea" in primera.json()["reply"]

    reply = segunda.json()["reply"]
    assert reply == "Eso lo ve un asesor y te contesta enseguida."
    # Y lo que de verdad viajó al CRM tampoco lo trae.
    texto = json.loads(enviados.calls[-1].request.content)["text"]
    assert "Soy Nea" not in texto
    assert "¿Para qué trabajo" not in texto
