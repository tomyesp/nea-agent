"""017 — Una falta de stock inventada no sale.

De la charla real con Pedrito (2026-09-19): el agente ofrecía una "Caterpillar
320L" (la máquina es la Excavadora 320 DL), consultó disponibilidad con el
modelo equivocado, el CRM le contestó con una Cargadora 938H LIBRE, y le
escribió al lead "la Caterpillar 320L no la tengo disponible para ese día" +
le cotizó la cargadora. Estaban todas libres.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.catalog_guard import menciones_ajenas, tokens_del_catalogo
from app.llm import LlmReply, ToolCall
from app.state import InboundMessage
from app.stock_guard import afirma_falta_de_stock, stock_inventado
from app.turn import run_turn
from tests.conftest import CRM_URL, IDENTITY, FakeLLM, make_ctx, mock_crm_basics

CATALOGO = {
    "modelos": [
        {
            "modeloId": "mmod_320",
            "nombre": "Excavadora 320 DL",
            "marca": "Caterpillar",
            "categoria": "Excavadoras",
            "descripcion": "Excavadora de oruga para excavación profunda.",
            "specs": {"capacidad": "Balde: 1 m³"},
            "tarifa": {"horaCents": 21539500, "minimoHoras": 1},
        },
        {
            "modeloId": "mmod_938",
            "nombre": "Cargadora 938H",
            "marca": "Caterpillar",
            "categoria": "Cargadoras",
            "descripcion": "Cargadora frontal.",
            "specs": {},
            "tarifa": {"horaCents": 19642000, "minimoHoras": 1},
        },
    ]
}
PERMITIDO = tokens_del_catalogo(CATALOGO["modelos"])


# --------------------------------------- el nombre torcido de una máquina ---


def test_la_320l_no_pasa_porque_la_maquina_es_la_320_dl():
    """"320l" se leía como "320 litros" y la guarda de catálogo la dejaba
    pasar. Los números son los de un modelo del catálogo: está mal escrita."""
    texto = "Tenemos la *Caterpillar 320L*, con un alcance de 9,5 metros."
    assert "320l" in menciones_ajenas(texto, PERMITIDO)


@pytest.mark.parametrize(
    "texto",
    [
        # El nombre bien escrito pasa.
        "La Excavadora 320 DL tiene balde de 1 m3.",
        "La Cargadora 938H es Caterpillar.",
        # Medidas de verdad: no son modelos.
        "El terreno de 20x40 son 800m2 y lleva unas 8hs de máquina.",
        "Son 100 m3 de tierra, 3tn por viaje.",
    ],
)
def test_no_confunde_medidas_con_modelos(texto):
    assert menciones_ajenas(texto, PERMITIDO) == []


# ------------------------------------------------ la falta de stock falsa ---


@pytest.mark.parametrize(
    "texto",
    [
        "La Caterpillar 320L no la tengo disponible para ese día.",
        "Esa máquina no está disponible el martes.",
        "La 320 DL está tomada para esas fechas.",
        "No hay disponibilidad para el martes.",
        "No me quedan libres para ese día.",
    ],
)
def test_reconoce_la_frase_de_falta_de_stock(texto):
    assert afirma_falta_de_stock(texto)


def test_no_confunde_otras_negaciones():
    assert not afirma_falta_de_stock("No incluye el traslado, se cotiza aparte.")
    assert not afirma_falta_de_stock("La excavadora está disponible el martes.")


def test_frena_la_falta_de_stock_de_lo_que_ni_consulto():
    """El caso de Pedrito, exacto."""
    consultadas = {"Cargadora 938H": True}
    texto = (
        "Ah, disculpá, la excavadora que te estoy cotizando es la Cargadora 938H. "
        "La Excavadora 320 DL no la tengo disponible para ese día."
    )
    problemas = stock_inventado(texto, consultadas)
    assert problemas and "no la consultaste en este turno" in problemas[0]


def test_frena_decir_que_esta_tomada_lo_que_el_sistema_dio_libre():
    problemas = stock_inventado(
        "La Excavadora 320 DL está tomada el martes.", {"Excavadora 320 DL": True}
    )
    assert problemas and "SÍ está libre" in problemas[0]


@pytest.mark.parametrize(
    "texto, consultadas",
    [
        # Lo que el sistema dijo que está ocupado, se puede decir.
        ("La Excavadora 320 DL está tomada el martes, pero tengo la 938H libre.",
         {"Excavadora 320 DL": False, "Cargadora 938H": True}),
        # Sin falta de stock no hay nada que frenar.
        ("Te queda tomada la Excavadora 320 DL para el martes.", {"Excavadora 320 DL": True}),
        # Sin consultas en el turno la guarda no opina (puede venir de antes).
        ("Esa no está disponible el martes.", {}),
    ],
)
def test_deja_pasar_lo_que_es_verdad(texto, consultadas):
    assert stock_inventado(texto, consultadas) == []


# ------------------------------------------------------ por la puerta real ---


DISPONIBLE = {
    "disponible": True,
    "horasPorDia": 8,
    "nota": "calculado sobre 8 horas por día",
    "ofertas": [
        {
            "ofertaId": "of_1",
            "modeloId": "mmod_938",
            "etiqueta": "Cargadora 938H, mar 23 sep (1 día)",
            "desde": "2026-09-23",
            "hasta": "2026-09-24",
            "precioTotalSinIvaCents": 157136000,
        }
    ],
}


async def _turno(respx_mock, llm: FakeLLM):
    ctx = make_ctx(llm=llm)
    ctx.inventory_enabled = True
    routes = mock_crm_basics(respx_mock)
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(200, json=CATALOGO)
    )
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/disponibilidad").mock(
        return_value=httpx.Response(200, json=DISPONIBLE)
    )
    await run_turn(
        ctx,
        IDENTITY,
        [InboundMessage(wa_message_id="wamid.1", identity=IDENTITY, type="text",
                        text="la necesito el martes que viene, un día")],
    )
    await ctx.crm.aclose()
    return [json.loads(c.request.content)["text"] for c in routes["messages"].calls]


CONSULTA = LlmReply(
    content=None,
    tool_calls=[
        ToolCall(
            id="c1",
            name="consultar_disponibilidad",
            arguments={"modelo_id": "mmod_938", "desde": "2026-09-23",
                       "ultimo_dia": "2026-09-23", "dias": 1, "horas_por_dia": 8},
        )
    ],
)


async def test_el_no_hay_falso_no_le_llega_al_lead_y_el_modelo_corrige(respx_mock):
    llm = FakeLLM(
        replies=[
            CONSULTA,
            LlmReply(content="La Excavadora 320 DL no la tengo disponible; te cotizo la Cargadora 938H."),
            LlmReply(content="Me confundí de máquina al chequear, ya lo verifico bien."),
        ]
    )
    enviados = await _turno(respx_mock, llm)
    assert enviados == ["Me confundí de máquina al chequear, ya lo verifico bien."]
    alerta = [
        m["content"]
        for m in llm.calls[-1]["messages"]
        if m["role"] == "system" and "NO se envió" in str(m["content"])
    ]
    assert len(alerta) == 1 and "Cargadora 938H (libre)" in alerta[0]


async def test_si_insiste_sale_la_disculpa_armada(respx_mock):
    falsa = LlmReply(content="Esa no está disponible para el martes, te paso otra.")
    llm = FakeLLM(replies=[CONSULTA, falsa, falsa])
    enviados = await _turno(respx_mock, llm)
    assert len(enviados) == 1 and "Dejame verificar bien la disponibilidad" in enviados[0]


async def test_la_maquina_sin_implementos_lo_dice(respx_mock):
    """Con el martillo de la mini en el catálogo, el modelo se lo colgó a una
    excavadora. La ficha de cada máquina lo dice con todas las letras."""
    from app.tools import ToolRuntime

    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelos": [
                    CATALOGO["modelos"][0],
                    {"modeloId": "mmod_mini", "nombre": "Minicargadora 252B",
                     "specs": {"implementos": [{"nombre": "Martillo hidráulico chico"}]}},
                ]
            },
        )
    )
    out = await ToolRuntime(ctx, conv, "c").execute("buscar_maquinas", {"consulta": "martillo"})
    await ctx.crm.aclose()
    por_nombre = {m["nombre"]: m.get("implementos") for m in out["maquinas"]}
    assert por_nombre["Excavadora 320 DL"].startswith("NINGUNO")
    assert por_nombre["Minicargadora 252B"] is None  # los suyos van en specs
    assert "de ninguna otra" in out["instrucciones"]


# ------------------------------------------- el implemento de otra máquina ---

FLOTA_CON_IMPLEMENTOS = {
    "Excavadora 320 DL": {"capacidad": "Balde: 1 m³"},
    "Minicargadora 252B": {
        "implementos": [
            {"nombre": "Martillo hidráulico grande"},
            {"nombre": "Hoyadora hidráulica"},
        ]
    },
}


@pytest.mark.parametrize(
    "texto",
    [
        "Para demoler te sirve una excavadora con martillo hidráulico.",
        "La Excavadora 320 DL con el martillo rompe el ladrillo.",
        "Te mando la Excavadora 320 DL con una hoyadora para los pozos.",
    ],
)
def test_frena_el_implemento_colgado_a_otra_maquina(texto):
    from app.catalog_guard import implementos_mal_colgados

    problemas = implementos_mal_colgados(texto, FLOTA_CON_IMPLEMENTOS)
    assert problemas and "Minicargadora 252B" in problemas[0]


@pytest.mark.parametrize(
    "texto",
    [
        # El implemento con SU máquina.
        "La Minicargadora 252B con el martillo hidráulico grande rompe el contrapiso.",
        # Las dos nombradas: el martillo es de la mini y se entiende.
        "Te dejo la Minicargadora 252B con martillo y la Excavadora 320 DL para el volumen.",
        # Decir que NO lo lleva está bien.
        "La Excavadora 320 DL no lleva martillo hidráulico: el martillo va en la minicargadora.",
        # Sin implementos nombrados no hay nada que frenar.
        "La Excavadora 320 DL excava hasta 6,65 m.",
    ],
)
def test_deja_pasar_el_implemento_bien_puesto(texto):
    from app.catalog_guard import implementos_mal_colgados

    assert implementos_mal_colgados(texto, FLOTA_CON_IMPLEMENTOS) == []


async def test_la_guarda_de_implementos_mira_el_catalogo_completo(respx_mock):
    """Buscando "grúa para demoler" volvían solo las grúas, y con eso nadie
    sabía que el martillo es de la minicargadora: pasó en vivo."""
    from app.catalog_guard import limpiar_cache

    limpiar_cache()
    ctx = make_ctx(
        llm=FakeLLM(
            replies=[
                LlmReply(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="buscar_maquinas",
                                         arguments={"consulta": "grua para demoler"})],
                ),
                LlmReply(content="Te recomiendo la Excavadora 320 DL con martillo hidráulico."),
                LlmReply(content="El martillo va en la Minicargadora 252B; la excavadora demuele con el balde."),
            ]
        )
    )
    ctx.inventory_enabled = True
    routes = mock_crm_basics(respx_mock)
    completo = {
        "modelos": [
            CATALOGO["modelos"][0],
            {"modeloId": "mmod_mini", "nombre": "Minicargadora 252B",
             "specs": {"implementos": [{"nombre": "Martillo hidráulico grande"}]}},
        ]
    }

    def catalogo(request):
        q = request.url.params.get("q") or ""
        if "grua" in q:  # la búsqueda del modelo NO trae la mini
            return httpx.Response(200, json={"modelos": []})
        return httpx.Response(200, json=completo)

    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(side_effect=catalogo)
    await run_turn(
        ctx, IDENTITY,
        [InboundMessage(wa_message_id="w1", identity=IDENTITY, type="text", text="demoler paredes")],
    )
    await ctx.crm.aclose()
    limpiar_cache()
    enviados = [json.loads(c.request.content)["text"] for c in routes["messages"].calls]
    assert enviados == ["El martillo va en la Minicargadora 252B; la excavadora demuele con el balde."]


async def test_si_insiste_con_el_implemento_ajeno_lo_toma_un_asesor(respx_mock):
    """Decisión del dueño: lo que supera a la flota (demoler en altura) no se
    resuelve con la máquina más parecida — lo ve una persona."""
    from app.catalog_guard import limpiar_cache

    limpiar_cache()
    falsa = LlmReply(content="La Excavadora 320 DL con martillo hidráulico te demuele eso.")
    # Tres: la primera vuelta la pide la ficha del catálogo (no miró), las
    # otras dos son las de la guarda del implemento.
    ctx = make_ctx(llm=FakeLLM(replies=[falsa, falsa, falsa]))
    ctx.inventory_enabled = True
    routes = mock_crm_basics(respx_mock)
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelos": [
                    CATALOGO["modelos"][0],
                    {"modeloId": "mmod_mini", "nombre": "Minicargadora 252B",
                     "specs": {"implementos": [{"nombre": "Martillo hidráulico grande"}]}},
                ]
            },
        )
    )
    await run_turn(
        ctx, IDENTITY,
        [InboundMessage(wa_message_id="w1", identity=IDENTITY, type="text",
                        text="demoler una pared de tres pisos")],
    )
    await ctx.crm.aclose()
    limpiar_cache()
    enviados = [json.loads(c.request.content)["text"] for c in routes["messages"].calls]
    assert len(enviados) == 1 and "Te paso con un asesor" in enviados[0]
    assert routes["handoff"].called
