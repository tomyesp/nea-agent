"""017 — ¿Entra la máquina por donde hay que pasar? Lo decide el código.

En vivo con Gemini (2026-09-19): "la minicargadora mide 1,83 m, así que entra
bien en el pasillo de 1,50 m", y "la excavadora 320 debería entrar bien" sin
que la ficha trajera el ancho.
"""
from __future__ import annotations

import httpx
import pytest

from app.acceso import acceso_de, paso_del_lead, veredicto
from app.tools import ToolRuntime
from tests.conftest import CRM_URL, IDENTITY, make_ctx

MINI = {
    "ancho_m": 1.83,
    "implementos": [
        {"nombre": "Cucharón", "ancho_m": 1.90},
        {"nombre": "Zanjeadora"},
        {"nombre": "Rotocultivador / desbrozadora", "ancho_m": 2.27},
    ],
}


@pytest.mark.parametrize(
    "textos, esperado",
    [
        (["se entra por un pasillo de 1,50 m de ancho"], 1.5),
        (["el portón tiene 90 cm"], 0.9),
        (["la entrada es de 2,20"], 2.2),
        (["Portón de 3 metros, sin problema"], 3.0),
        # La última medida que dijo el lead corrige a las de antes.
        (["pasillo de 1,5 m", "perdón, el pasillo mide 2 metros"], 2.0),
        # Lo que NO es un paso.
        (["vivo en pasaje San Martín 450"], None),
        (["la zanja es de 60 cm de ancho y 1 m de hondo"], None),
        (["tengo que hacer 30 pozos"], None),
        ([], None),
    ],
)
def test_encuentra_el_paso_en_lo_que_dijo_el_lead(textos, esperado):
    assert paso_del_lead(textos) == esperado


def test_el_margen_chico_no_se_promete():
    assert veredicto(1.50, 1.83) == "no"
    assert veredicto(1.85, 1.83) == "justo"
    assert veredicto(2.20, 1.83) == "si"


def test_la_mini_no_entra_por_un_pasillo_de_un_metro_y_medio():
    a = acceso_de(MINI, 1.5)
    assert a["entra"] == "no"
    assert "NO ENTRA" in a["detalle"] and "1,83 m" in a["detalle"]


def test_con_un_implemento_mas_ancho_manda_el_implemento():
    """Portón de 1,85 m: la mini pasa justo; con el cucharón (1,90 m), no."""
    a = acceso_de(MINI, 1.85)
    assert a["entra"] == "justo"
    assert any("Cucharón" in x and "NO pasa" in x for x in a["implementos_que_no_pasan"])

    b = acceso_de(MINI, 2.20)
    assert b["entra"] == "si"
    assert b["implementos_que_no_pasan"] == ["Rotocultivador / desbrozadora (2,27 m): NO pasa"]


def test_sin_ancho_en_la_ficha_no_se_promete_nada():
    a = acceso_de({"otras": "Profundidad 6,65 m"}, 1.5)
    assert a["entra"] == "sin_dato"


async def test_buscar_maquinas_devuelve_el_veredicto(respx_mock):
    """El paso sale de los mensajes del lead aunque el modelo no lo mande."""
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(ctx, conv, "conv_crm_1", ancho_paso_m=1.5)
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelos": [
                    {"modeloId": "m1", "nombre": "Minicargadora 252B", "specs": MINI},
                    {"modeloId": "m2", "nombre": "Excavadora 320 DL", "specs": {}},
                ]
            },
        )
    )
    out = await runtime.execute("buscar_maquinas", {"consulta": "zanja"})
    await ctx.crm.aclose()
    por_nombre = {m["nombre"]: m["acceso"]["entra"] for m in out["maquinas"]}
    assert por_nombre == {"Minicargadora 252B": "no", "Excavadora 320 DL": "sin_dato"}
    assert "1,50 m" in out["instrucciones"]

    # Si el modelo manda la medida, gana la suya.
    ctx2 = make_ctx()
    conv2 = await ctx2.store.get_or_create_conversation(IDENTITY)
    runtime2 = ToolRuntime(ctx2, conv2, "conv_crm_1", ancho_paso_m=1.5)
    out2 = await runtime2.execute("buscar_maquinas", {"consulta": "zanja", "ancho_paso_m": 2.2})
    await ctx2.crm.aclose()
    assert out2["maquinas"][0]["acceso"]["entra"] == "si"


async def test_sin_paso_no_hay_veredicto(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(ctx, conv, "conv_crm_1")
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(
            200, json={"modelos": [{"modeloId": "m1", "nombre": "Minicargadora 252B", "specs": MINI}]}
        )
    )
    out = await runtime.execute("buscar_maquinas", {"consulta": "mini"})
    await ctx.crm.aclose()
    assert "acceso" not in out["maquinas"][0]
