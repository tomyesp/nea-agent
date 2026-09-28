"""¿En qué carretón va la máquina que el lead quiere trasladar? Lo calcula
el código: Gemini leyó "15 a 22 t" y contestó que 15 t "por el peso no habría
problema" (2026-09-28). Los límites son datos de cada carretón."""

from __future__ import annotations

import httpx
import pytest

from app.tools import ToolRuntime
from app.traslado import peso_del_lead, tipo_del_lead, veredicto_carreton
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx

TODAS = ["minicargadora", "rodillo", "compactadora", "retroexcavadora", "tractor agricola",
         "excavadora", "motoniveladora", "cargadora", "topadora"]
CHICO = {"nombre": "Carretón chico", "es_acoplado": True, "carga_max_t": 8,
         "tipos_lleva": ["minicargadora", "rodillo"],
         "tipos_a_confirmar": ["tractor agricola", "compactadora"],
         "tipos_no": ["retroexcavadora", "excavadora", "motoniveladora", "cargadora", "topadora"]}
MEDIANO = {"nombre": "Carretón mediano", "es_acoplado": True, "carga_max_t": 22,
           "carga_sin_dudas_t": 15,
           "tipos_lleva": ["minicargadora", "rodillo", "compactadora", "retroexcavadora", "tractor agricola"],
           "tipos_a_confirmar": ["motoniveladora", "cargadora", "topadora"],
           "tipos_no": ["excavadora"]}
GRANDE = {"nombre": "Carretón grande Randon", "es_acoplado": True, "carga_max_t": 45,
          "tipos_lleva": [t for t in TODAS if t != "topadora"], "tipos_a_confirmar": ["topadora"]}
SEMI = {"nombre": "Semirremolque de carga general", "es_acoplado": True, "lleva_maquinas": False}
FLOTA = [CHICO, MEDIANO, GRANDE, SEMI]


def _v(*textos):
    return veredicto_carreton(list(textos), FLOTA)


def test_una_minicargadora_va_en_todos_los_de_rampa():
    v = _v("me llevan mi bobcat S650 de 3,8 t a Los Perales?")
    assert v.startswith("ENTRA")
    assert "Carretón chico" in v and "Carretón mediano" in v and "Carretón grande Randon" in v
    assert "Semirremolque" not in v


def test_una_retro_de_8_t_no_va_en_el_chico():
    """Pesa lo que carga el chico, pero no entra en su plataforma de 4 m."""
    v = _v("necesito llevar mi retroexcavadora", "pesa unas 8 toneladas")
    assert v.startswith("ENTRA") and "Carretón chico" not in v
    assert "Carretón mediano" in v


def test_una_excavadora_de_22_t_va_en_el_grande():
    v = _v("tengo que trasladar mi excavadora de 22 toneladas")
    assert v.startswith("ENTRA") and "Carretón grande Randon" in v
    assert "mediano" not in v


def test_una_motoniveladora_de_15_t_va_en_el_grande_y_en_el_mediano_se_confirma():
    v = _v("necesito mover una motoniveladora mía de 15 toneladas a Palpalá")
    assert v.startswith("ENTRA") and "Carretón grande Randon" in v
    assert "Carretón mediano" in v.split("lo confirma el asesor")[0].split("En ")[-1]


def test_la_topadora_la_confirma_el_asesor():
    v = _v("hay que trasladar una topadora")
    assert v.startswith("A CONFIRMAR") and "NO digas que entra" in v


def test_mas_de_45_t_no_entra_en_ninguno():
    assert _v("trasladar una grúa de 60 t").startswith("NO ENTRA")


def test_sin_peso_pide_el_peso():
    v = _v("me llevan mi minicargadora?")
    assert v.startswith("ENTRA") and "pedíselo" in v


def test_una_retroexcavadora_no_es_una_excavadora():
    assert tipo_del_lead(["hay que llevar una retroexcavadora de 7 t"]) == "retroexcavadora"
    assert tipo_del_lead(["mi retro", "no, perdón, es una excavadora"]) == "excavadora"


def test_sin_traslado_no_opina():
    """Alquilar una excavadora de 21 t no es trasladar nada."""
    assert _v("necesito una excavadora de 21 toneladas para un subsuelo") is None


def test_sin_peso_ni_tipo_no_opina():
    assert _v("me llevan una máquina a Palpalá?") is None


def test_lee_el_ultimo_peso_que_dijo():
    assert peso_del_lead(["pesa 8 toneladas", "perdón, son 12,5 t"]) == 12.5


async def test_el_veredicto_viaja_en_los_tractores(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    modelos = [
        {"modeloId": "t1", "nombre": "Ford Cargo 1832 - Tractor", "categoria": "Camiones tractor",
         "specs": {"implementos": FLOTA}, "tarifa": {}},
        {"modeloId": "v1", "nombre": "Ford Cargo 1317 - Volquete", "categoria": "Camiones volquete",
         "specs": {}, "tarifa": {}},
    ]
    respx_mock.get(f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(200, json={"categorias": [], "modelos": modelos})
    )
    del_lead = ["necesito mover una excavadora de 22 toneladas"]
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID, del_lead=del_lead)
    result = await runtime.execute("buscar_maquinas", {"consulta": "camion tractor"})
    await ctx.crm.aclose()

    tractor, volquete = result["maquinas"]
    assert tractor["carreton_para_lo_del_lead"].startswith("ENTRA")
    assert "Carretón grande Randon" in tractor["carreton_para_lo_del_lead"]
    assert "carreton_para_lo_del_lead" not in volquete
    assert "MANDA sobre cualquier cuenta tuya" in result["instrucciones"]


# ------------------------- precio que lo pasa un asesor ---

TRACTOR = {
    "modeloId": "t1",
    "nombre": "Ford Cargo 1832 - Tractor",
    "categoria": "Camiones tractor",
    "specs": {"precio_con_asesor": True},
    "tarifa": {"horaCents": 44_930_500},
}


async def test_el_precio_en_revision_no_le_llega_al_modelo(respx_mock):
    """El perfil dice que el precio de los tractores está en revisión y lo pasa
    un asesor; con el número a la vista, Gemini lo dijo igual (2026-09-28)."""
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    respx_mock.get(f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(200, json={"categorias": [], "modelos": [TRACTOR]})
    )
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID)
    result = await runtime.execute("buscar_maquinas", {"consulta": "tractor"})
    cotizado = await runtime.execute(
        "cotizar", {"modelo_id": "t1", "dias": 1, "horas_por_dia": 8}
    )
    await ctx.crm.aclose()

    assert result["maquinas"][0]["precio_por_hora"] is None
    assert "Lo pasa un asesor" in result["maquinas"][0]["precio"]
    assert "449" not in str(result)
    assert cotizado["error"] == "precio_con_asesor"


def test_la_ficha_que_se_le_pasa_tampoco_trae_el_precio():
    from app.catalog_guard import aviso_sin_mirar_el_catalogo

    assert "449" not in aviso_sin_mirar_el_catalogo([TRACTOR])
