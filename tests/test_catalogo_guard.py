"""017 — No se nombra una máquina que el negocio no tiene.

De las pruebas del dueño (2026-09-16): "te recomiendo una minicargadora
Bobcat", "la Bobcat S450", "la motoniveladora New Holland RG140". Ninguna
existe en RPM.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.catalog_guard import (
    limpiar_cache,
    menciones_ajenas,
    tokens_del_catalogo,
)
from app.llm import LlmReply, ToolCall
from app.state import InboundMessage
from app.turn import run_turn
from tests.conftest import CRM_URL, IDENTITY, FakeLLM, make_ctx, mock_crm_basics

CATALOGO = {
    "modelos": [
        {
            "modeloId": "mmod_406",
            "nombre": "Retroexcavadora 406",
            "marca": "Randon",
            "categoria": "Retroexcavadoras",
            "descripcion": "Retro para zanjeo y carga",
            "specs": {"capacidad": "Balde 1 m3"},
            "tarifa": {"horaCents": 17641000, "minimoHoras": 1},
        },
        {
            "modeloId": "mmod_236",
            "nombre": "Minicargadora 236C",
            "marca": "Caterpillar",
            "categoria": "Minicargadoras",
            "descripcion": "",
            "specs": {},
            "tarifa": {"horaCents": 12000000, "minimoHoras": 1},
        },
        {
            "modeloId": "mmod_140",
            "nombre": "Motoniveladora 140H",
            "marca": "Caterpillar",
            "categoria": "Motoniveladoras",
            "descripcion": "",
            "specs": {},
            "tarifa": {"horaCents": 22747000, "minimoHoras": 1},
        },
    ]
}

PERMITIDO = tokens_del_catalogo(CATALOGO["modelos"])


@pytest.fixture(autouse=True)
def _sin_cache():
    limpiar_cache()
    yield
    limpiar_cache()


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("Te recomiendo una minicargadora Bobcat: entra en espacios angostos.", "bobcat"),
        ("La motoniveladora New Holland RG140 deja la superficie pareja.", "new holland"),
        ("Con la Bobcat S450 (1,26 m de ancho) lo hacés.", "bobcat"),
        ("Te sirve una Komatsu chica.", "komatsu"),
    ],
)
def test_detecta_marcas_que_el_negocio_no_tiene(texto, esperado):
    assert esperado in menciones_ajenas(texto, PERMITIDO)


def test_detecta_un_modelo_inventado_aunque_la_marca_exista():
    """"Caterpillar 315F" suena bien y no está en la flota."""
    assert "315f" in menciones_ajenas("Te paso la Caterpillar 315F.", PERMITIDO)


@pytest.mark.parametrize(
    "texto",
    [
        # Las del catálogo, con sus marcas y categorías.
        "Para esa zanja va la Retroexcavadora 406 (Randon), balde de 1 m3.",
        "Tenemos minicargadoras Caterpillar: la 236C entra en espacios angostos.",
        "La Motoniveladora 140H es la que deja el terreno parejo.",
        # Decir que NO tenemos una marca es exactamente lo que se le pide.
        "Bobcat no tenemos: lo nuestro son las minicargadoras Caterpillar.",
        "No trabajamos con New Holland, pero la Motoniveladora 140H hace ese trabajo.",
        # Medidas y precios no son modelos.
        "El terreno de 20x40 son 800m2, y la hora sale $176.410 más IVA.",
        "Son 100 m3 de tierra y unas 8 hs de máquina.",
        "",
    ],
)
def test_no_se_confunde_con_lo_que_si_existe(texto):
    assert menciones_ajenas(texto, PERMITIDO) == []


def test_lo_que_dice_la_ficha_tecnica_tambien_es_catalogo():
    """El motor y los implementos viven en specs, anidados. Citarlos es lo que
    pide el prompt: la guarda no puede frenarlo como un modelo inventado."""
    modelos = [
        {
            "nombre": "Minicargadora 252B",
            "marca": "Caterpillar",
            "categoria": "Minicargadoras",
            "specs": {
                "motor": "Cat 3044C DIT (3.3 L)",
                "implementos": [
                    {"nombre": "Martillo hidráulico chico", "modelo": "CAT H55D S"},
                    {"nombre": "Hoyadora hidráulica", "modelo": "CAT A19B", "unidades": 2},
                ],
            },
        }
    ]
    permitido = tokens_del_catalogo(modelos)
    texto = (
        "La Minicargadora 252B (motor Cat 3044C) va con el martillo CAT H55D S "
        "o con la hoyadora A19B."
    )
    assert menciones_ajenas(texto, permitido) == []
    # Lo que NO está en la ficha se sigue frenando.
    assert "h65d" in menciones_ajenas("Te llevo el martillo H65D.", permitido)


@pytest.mark.parametrize(
    "texto, esperado",
    [
        # De un lead real (2026-09-19): recomendó una JCB 8018 CTS de memoria,
        # con medidas y todo, sin haber mirado el catálogo. El tipo de máquina
        # no existe en la flota y el código va partido ("8018" + "CTS"), así
        # que ni las marcas ni `_sospechoso` lo veían.
        ("Para una vereda céntrica lo ideal es una miniexcavadora.", "miniexcavadora"),
        ("Te cuento más de la Miniexcavadora 8018 CTS: mide 0,96 m de ancho.", "8018 cts"),
        ("Te consigo un manipulador telescópico.", "manipulador telescopico"),
    ],
)
def test_un_tipo_de_maquina_que_el_negocio_no_tiene_tampoco_sale(texto, esperado):
    assert esperado in menciones_ajenas(texto, PERMITIDO)


@pytest.mark.parametrize(
    "texto",
    [
        # Números que NO son modelos: fechas, plata, medidas, cantidades.
        "Te la dejo tomada del lunes 21 al miércoles 23 de septiembre por $1.651.860 + IVA.",
        "Pesa 21.500 kg y excava hasta 6,65 metros.",
        "Son 2.000 kPa de contrapresión y 1.022 golpes por minuto.",
        "El rotocultivador tiene 1,85 m de ancho y 36 cuchillas.",
        # Decir que no tenemos ese tipo es exactamente lo que se le pide.
        "Miniexcavadora no tenemos: lo más chico es la Minicargadora 236C.",
    ],
)
def test_no_confunde_numeros_sueltos_con_modelos(texto):
    assert menciones_ajenas(texto, PERMITIDO) == []


def test_sin_catalogo_la_guarda_no_opina():
    """Si el CRM no contesta, no hay fuente de verdad: jamás frenar a ciegas."""
    assert menciones_ajenas("Te recomiendo una Bobcat S450.", set()) == []


# ------------------------------------------------------ por la puerta real ---


async def _turno(respx_mock, llm: FakeLLM, texto="necesito una maquina para un pozo"):
    ctx = make_ctx(llm=llm)
    ctx.inventory_enabled = True
    routes = mock_crm_basics(respx_mock)
    routes["catalogo"] = respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(200, json=CATALOGO)
    )
    result = await run_turn(
        ctx,
        IDENTITY,
        [InboundMessage(wa_message_id="wamid.1", identity=IDENTITY, type="text", text=texto)],
    )
    await ctx.crm.aclose()
    enviados = [json.loads(c.request.content)["text"] for c in routes["messages"].calls]
    return result, routes, enviados


BUSCAR = LlmReply(
    content=None,
    tool_calls=[ToolCall(id="c1", name="buscar_maquinas", arguments={"consulta": "pozo"})],
)


async def test_la_bobcat_no_le_llega_al_lead_y_el_modelo_corrige(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(content="Te recomiendo una minicargadora Bobcat: excava hasta 2,75 m."),
            BUSCAR,
            LlmReply(content="Para ese pozo va la Retroexcavadora 406 (Randon), balde de 1 m3."),
        ]
    )
    result, routes, enviados = await _turno(respx_mock, llm)

    assert enviados == ["Para ese pozo va la Retroexcavadora 406 (Randon), balde de 1 m3."]
    alerta = [
        m["content"]
        for m in llm.calls[-1]["messages"]
        if m["role"] == "system" and "NO se envió" in str(m["content"])
    ]
    assert len(alerta) == 1 and "bobcat" in alerta[0]


async def test_si_insiste_con_la_marca_ajena_sale_la_pregunta_segura(respx_mock):
    falsa = LlmReply(content="La Bobcat S450 es la que te sirve, 1,26 m de ancho.")
    llm = FakeLLM(replies=[falsa, falsa])
    result, routes, enviados = await _turno(respx_mock, llm)

    assert len(enviados) == 1
    assert "Bobcat" not in enviados[0] and "S450" not in enviados[0]
    assert "catálogo" in enviados[0]


async def test_una_respuesta_con_maquinas_del_catalogo_no_paga_vuelta_extra(respx_mock):
    llm = FakeLLM(
        replies=[LlmReply(content="Para ese pozo va la Retroexcavadora 406, balde de 1 m3.")]
    )
    result, routes, enviados = await _turno(respx_mock, llm)

    assert enviados == ["Para ese pozo va la Retroexcavadora 406, balde de 1 m3."]
    assert len(llm.calls) == 1
