"""¿La máquina que el lead quiere trasladar va en el carretón? Lo calcula el
código: Gemini leyó "15 a 22 t" y contestó que 15 t "por el peso no habría
problema" (2026-09-28)."""

from __future__ import annotations

import httpx
import pytest

from app.tools import ToolRuntime
from app.traslado import peso_del_lead, veredicto_carreton
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx


@pytest.mark.parametrize(
    "textos, empieza",
    [
        (["necesito llevar mi retro de Perico a Humahuaca", "pesa unas 8 toneladas"], "ENTRA"),
        (["me llevan mi minicargadora de 3,5 t al centro?"], "ENTRA"),
        (["necesito mover una motoniveladora mía de 15 toneladas a Palpalá"], "A CONFIRMAR"),
        (["hay que trasladar una cargadora frontal"], "A CONFIRMAR"),
        (["tengo que llevar un tractor agrícola de 16 tn"], "A CONFIRMAR"),
        (["tengo que trasladar mi excavadora de 22 toneladas"], "NO ENTRA"),
        (["me llevan mi excavadora Hyundai R220?"], "NO ENTRA"),
        (["trasladar una grúa de 30 t"], "NO ENTRA"),
    ],
)
def test_veredicto_del_carreton(textos, empieza):
    assert veredicto_carreton(textos).startswith(empieza)


def test_una_retroexcavadora_no_es_una_excavadora():
    assert veredicto_carreton(["hay que llevar una retroexcavadora de 7 t"]).startswith("ENTRA")


def test_sin_traslado_no_opina():
    """Alquilar una excavadora de 21 t no es trasladar nada."""
    assert veredicto_carreton(["necesito una excavadora de 21 toneladas para un subsuelo"]) is None


def test_sin_peso_ni_tipo_no_opina():
    assert veredicto_carreton(["me llevan una máquina a Palpalá?"]) is None


def test_lee_el_ultimo_peso_que_dijo():
    assert peso_del_lead(["pesa 8 toneladas", "perdón, son 12,5 t"]) == 12.5


async def test_el_veredicto_viaja_en_los_tractores(respx_mock):
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    modelos = [
        {"modeloId": "t1", "nombre": "Ford Cargo 1832 - Tractor", "categoria": "Camiones tractor",
         "specs": {"implementos": [{"nombre": "Carretón mediano", "es_acoplado": True}]},
         "tarifa": {}},
        {"modeloId": "v1", "nombre": "Ford Cargo 1317 - Volquete", "categoria": "Camiones volquete",
         "specs": {}, "tarifa": {}},
    ]
    respx_mock.get(f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(200, json={"categorias": [], "modelos": modelos})
    )
    veredicto = veredicto_carreton(["necesito mover una motoniveladora de 15 toneladas"])
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID, carreton=veredicto)
    result = await runtime.execute("buscar_maquinas", {"consulta": "camion tractor"})
    await ctx.crm.aclose()

    tractor, volquete = result["maquinas"]
    assert tractor["carreton_para_lo_del_lead"].startswith("A CONFIRMAR")
    assert "carreton_para_lo_del_lead" not in volquete
    assert "MANDA sobre cualquier cuenta tuya" in result["instrucciones"]
