"""017 — ¿Entra la máquina por donde hay que pasar? Lo decide el código.

En vivo con Gemini (2026-09-19): "la minicargadora mide 1,83 m, así que entra
bien en el pasillo de 1,50 m", y "la excavadora 320 debería entrar bien" sin
que la ficha trajera el ancho.
"""
from __future__ import annotations

import httpx
import pytest

from app.acceso import Paso, acceso_de, alto_del_lead, paso_del_lead, veredicto
from app.tools import ToolRuntime
from tests.conftest import CRM_URL, IDENTITY, make_ctx

MINI = {
    "ancho_m": 1.83,
    "alto_m": 2.06,
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
    a = acceso_de(MINI, Paso(ancho=1.5))
    assert a["entra"] == "no"
    assert "NO ENTRA" in a["detalle"] and "1,83 m" in a["detalle"]


def test_con_un_implemento_mas_ancho_manda_el_implemento():
    """Portón de 1,85 m: la mini pasa justo; con el cucharón (1,90 m), no."""
    a = acceso_de(MINI, Paso(ancho=1.85))
    assert a["entra"] == "justo"
    assert any("Cucharón" in x and "NO pasa" in x for x in a["implementos_que_no_pasan"])

    b = acceso_de(MINI, Paso(ancho=2.20))
    assert b["entra"] == "si"
    assert b["implementos_que_no_pasan"] == ["Rotocultivador / desbrozadora (2,27 m): NO pasa"]


def test_sin_ancho_en_la_ficha_no_se_promete_nada():
    a = acceso_de({"otras": "Profundidad 6,65 m"}, Paso(ancho=1.5))
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


# --------------------------------------------- la guarda sobre la respuesta ---

import json  # noqa: E402

from app.acceso import entidades_de_acceso, promesas_de_acceso  # noqa: E402
from app.llm import LlmReply, ToolCall  # noqa: E402
from app.state import InboundMessage  # noqa: E402
from app.turn import run_turn  # noqa: E402
from tests.conftest import FakeLLM, mock_crm_basics  # noqa: E402

FLOTA = {"Minicargadora 252B": MINI, "Excavadora 320 DL": {"otras": "6,65 m"}}


@pytest.mark.parametrize(
    "paso, texto",
    [
        # Las que salieron en vivo con Gemini (2026-09-19).
        (1.85, "Su ancho es de 1,83 m, lo que le permite pasar por tu portón de 1,85 m, "
               "aunque va a ir bien justo. Para sacar escombros, la equiparíamos con un Cucharón."),
        (1.5, "La minicargadora tiene 1,83 m, así que entra bien en el pasillo de 1,50 m."),
        (1.5, "La Excavadora 320 DL debería entrar bien por el pasillo."),
    ],
)
def test_frena_la_promesa_de_que_entra(paso, texto):
    assert promesas_de_acceso(texto, entidades_de_acceso(FLOTA, Paso(ancho=paso)))


@pytest.mark.parametrize(
    "paso, texto",
    [
        # Decir que NO entra, o que no se puede asegurar, es lo correcto.
        (1.5, "El pasillo mide 1,50 m, lo que significa que la Minicargadora NO entra."),
        (1.5, "La ficha no trae el ancho de la Excavadora 320 DL, no puedo asegurarte que entre."),
        (1.5, "No sé si la mini pasa por ahí: que lo vea un asesor."),
        # Lo que el sistema dijo que entra, entra.
        (2.2, "La Minicargadora 252B mide 1,83 m, así que entra por tu pasillo de 2,20 m."),
        (2.2, "La mini entra, pero el rotocultivador no pasa por ese pasillo."),
        # Sin promesa de acceso no hay nada que frenar.
        (1.5, "Para esa zanja va la Minicargadora 252B con la zanjeadora."),
    ],
)
def test_deja_pasar_lo_que_es_verdad(paso, texto):
    assert promesas_de_acceso(texto, entidades_de_acceso(FLOTA, Paso(ancho=paso))) == []


def test_con_el_rotocultivador_a_dos_veinte_no_se_promete():
    texto = "Con el rotocultivador puesto la mini entra por tu pasillo de 2,20 m."
    problemas = promesas_de_acceso(texto, entidades_de_acceso(FLOTA, Paso(ancho=2.2)))
    assert problemas and "Rotocultivador" in problemas[0]


CATALOGO_FLOTA = {
    "modelos": [
        {"modeloId": "m1", "nombre": "Minicargadora 252B", "marca": "Caterpillar",
         "categoria": "Minicargadoras", "specs": MINI},
    ]
}


async def _turno_con(respx_mock, llm, texto):
    ctx = make_ctx(llm=llm)
    ctx.inventory_enabled = True
    routes = mock_crm_basics(respx_mock)
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(200, json=CATALOGO_FLOTA)
    )
    await run_turn(
        ctx, IDENTITY,
        [InboundMessage(wa_message_id="wamid.1", identity=IDENTITY, type="text", text=texto)],
    )
    await ctx.crm.aclose()
    return [json.loads(c.request.content)["text"] for c in routes["messages"].calls]


BUSCA = LlmReply(
    content=None,
    tool_calls=[ToolCall(id="c1", name="buscar_maquinas", arguments={"consulta": "escombros"})],
)


async def test_el_entra_falso_no_le_llega_al_lead_y_el_modelo_corrige(respx_mock):
    llm = FakeLLM(
        replies=[
            BUSCA,
            LlmReply(content="La Minicargadora 252B con cucharón entra por tu portón, va justita."),
            LlmReply(content="Con 1,85 m no te puedo asegurar que entre con el cucharón: que lo vea un asesor."),
        ]
    )
    enviados = await _turno_con(respx_mock, llm, "tengo que sacar escombros, el porton tiene 1,85 m")
    assert enviados == ["Con 1,85 m no te puedo asegurar que entre con el cucharón: que lo vea un asesor."]
    alerta = [m["content"] for m in llm.calls[-1]["messages"]
              if m["role"] == "system" and "NO se envió" in str(m["content"])]
    assert len(alerta) == 1 and "Cucharón" in alerta[0]


async def test_si_insiste_sale_la_pregunta_segura(respx_mock):
    falsa = LlmReply(content="Tranqui, la mini entra por tu pasillo de 1,50 m.")
    llm = FakeLLM(replies=[BUSCA, falsa, falsa])
    enviados = await _turno_con(respx_mock, llm, "se entra por un pasillo de 1,50 m, tengo que sacar escombros")
    assert len(enviados) == 1
    assert "no te puedo asegurar" in enviados[0] and "1,50 m" in enviados[0]


async def test_lo_que_no_entra_llega_sin_ficha_y_sin_implementos_que_no_pasan(respx_mock):
    """En vivo recomendó la mini para 1,50 m "sin poder asegurar": sin ficha
    no tiene con qué. Y con 1,85 m el cucharón (1,90) sale de la lista."""
    catalogo = {"modelos": [{"modeloId": "m1", "nombre": "Minicargadora 252B",
                             "categoria": "Minicargadoras", "specs": MINI}]}
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(200, json=catalogo)
    )
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)

    no_entra = await ToolRuntime(ctx, conv, "c", ancho_paso_m=1.5).execute(
        "buscar_maquinas", {"consulta": "mini"})
    mini = no_entra["maquinas"][0]
    assert mini["acceso"]["entra"] == "no"
    assert "specs" not in mini and "precio_por_hora" not in mini

    justo = await ToolRuntime(ctx, conv, "c", ancho_paso_m=1.85).execute(
        "buscar_maquinas", {"consulta": "mini"})
    await ctx.crm.aclose()
    nombres = [i["nombre"] for i in justo["maquinas"][0]["specs"]["implementos"]]
    assert nombres == ["Zanjeadora"]


# ------------------------------------------------------------- el alto ---


@pytest.mark.parametrize(
    "texto, ancho, alto",
    [
        # "2 m de alto" es el alto del portón, NO el ancho.
        ("el portón tiene 2 m de alto", None, 2.0),
        ("la puerta tiene 3 m de ancho y 2,20 de alto", 3.0, 2.2),
        ("el techo del galpón está a 2,50 m", None, 2.5),
        ("hay cables a 3 m sobre la entrada", None, 3.0),
        # La altura de otra cosa no es un paso.
        ("los postes son de 2 m de alto", None, None),
        ("el muro tiene 3 m de altura", None, None),
    ],
)
def test_distingue_el_ancho_del_alto(texto, ancho, alto):
    assert paso_del_lead([texto]) == ancho
    assert alto_del_lead([texto]) == alto


def test_por_dos_metros_de_alto_la_mini_no_entra():
    """Mide 2,06 m: por un portón de 2 m de alto no pasa, aunque sobre ancho."""
    a = acceso_de(MINI, Paso(ancho=3.0, alto=2.0))
    assert a["entra"] == "no"
    assert "2,06 m de alto" in a["detalle"]
    assert acceso_de(MINI, Paso(ancho=2.2, alto=2.1))["entra"] == "justo"
    assert acceso_de(MINI, Paso(ancho=2.2, alto=2.5))["entra"] == "si"
    assert acceso_de({"ancho_m": 1.83}, Paso(alto=2.5))["entra"] == "sin_dato"


def test_frena_el_entra_por_un_porton_bajo():
    entidades = entidades_de_acceso(FLOTA, Paso(alto=2.0))
    assert promesas_de_acceso("La mini entra por tu portón sin problema.", entidades)
    assert promesas_de_acceso("Por 2 m de alto la mini no entra.", entidades) == []


# ------------------------------------- el ancho de la zanja NO es un paso ---


@pytest.mark.parametrize(
    "textos, esperado",
    [
        (["se entra por un pasillo de 1,50 m"], True),
        (["el portón es chico, no sé cuánto mide"], True),
        (["el techo del galpón es bajo"], True),
        # Medidas de la OBRA: no hay acceso del que hablar.
        (["zanja de 60 cm de ancho y 1,50 m de profundidad, sobre vereda"], False),
        # "cable subterráneo" NO es un cable aéreo que estorbe por arriba.
        (["tengo una obra de cable subterráneo de 2 km en zona céntrica"], False),
        (["necesito 30 pozos de 20 cm"], False),
        # Nombra el acceso para decir que NO limita (en vivo, 2026-09-26).
        (["es tierra normal, sin problemas de acceso, la zanja de unos 50 cm de ancho"], False),
        (["no hay limitaciones de acceso, zanja de 50 cm"], False),
        (["el acceso es libre, la zanja es de 40 cm"], False),
        (["el portón no es problema pero el pasillo tiene 1,20 m"], True),
    ],
)
def test_reconoce_si_el_lead_hablo_de_un_acceso(textos, esperado):
    from app.acceso import hablo_de_un_acceso

    assert hablo_de_un_acceso(textos) is esperado


async def test_sin_acceso_la_medida_del_modelo_se_ignora(respx_mock):
    """En vivo: el lead dijo "zanja de 60 cm de ancho" y el modelo mandó eso
    como ancho del paso; Nea contestó que no podía asegurar que la máquina
    pasara por 60 cm (2026-09-19)."""
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    respx_mock.get(url__startswith=f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(
            200, json={"modelos": [{"modeloId": "m1", "nombre": "Minicargadora 252B", "specs": MINI}]}
        )
    )
    sin_acceso = ToolRuntime(ctx, conv, "c", hablo_de_acceso=False)
    out = await sin_acceso.execute("buscar_maquinas", {"consulta": "zanja", "ancho_paso_m": 0.6})
    assert "acceso" not in out["maquinas"][0]

    con_acceso = ToolRuntime(ctx, conv, "c", hablo_de_acceso=True)
    out2 = await con_acceso.execute("buscar_maquinas", {"consulta": "zanja", "ancho_paso_m": 0.6})
    await ctx.crm.aclose()
    assert out2["maquinas"][0]["acceso"]["entra"] == "no"
