"""Tools de maquinaria (017): solo se reserva una oferta emitida por el CRM.

Lo que se fija acá es el blindaje antialucinación completo: el agente no puede
reservar un oferta_id que no le dieron, no puede quedarse sin salida cuando
pierde la carrera, y nunca ve un precio que no venga del servidor.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.state import RentalOffer
from app.tools import ToolRuntime, tool_schemas
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx

OFERTA_ID = "roff_abc123"
MODELO_ID = "mmod_retro3cx"


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
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID)
    yield runtime, ctx, conv
    await ctx.crm.aclose()


# ------------------------------------------------------------- catálogo ---


async def test_buscar_maquinas_devuelve_solo_lo_del_catalogo(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    route = respx_mock.get(f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(
            200,
            json={
                "categorias": [{"id": "c1", "nombre": "Retroexcavadoras"}],
                "modelos": [
                    {
                        "modeloId": MODELO_ID,
                        "nombre": "Retroexcavadora JCB 3CX",
                        "marca": "JCB",
                        "categoria": "Retroexcavadoras",
                        "specs": {"potencia_hp": 92},
                        "requiereOperario": True,
                        "unidades": 2,
                        "tarifa": {"horaCents": 3_200_000, "minimoHoras": 0},
                    }
                ],
            },
        )
    )
    result = await runtime.execute("buscar_maquinas", {"consulta": "retro"})
    assert result["ok"] is True
    assert [m["nombre"] for m in result["maquinas"]] == ["Retroexcavadora JCB 3CX"]
    # El precio de la hora llega formateado en pesos, listo para copiar.
    assert result["maquinas"][0]["precio_por_hora"] == "$32.000"
    assert result["maquinas"][0]["va_con_operario"] is True
    # Las dos condiciones que el agente tiene que repetir en cada precio.
    assert "operario y combustible" in result["instrucciones"]
    assert "SIN IVA" in result["instrucciones"]
    assert route.calls[0].request.url.params["q"] == "retro"


async def test_buscar_maquinas_sin_resultados_no_inventa(runtime_y_ctx, respx_mock):
    """Catálogo vacío para esa consulta: el agente lo dice, no improvisa."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.get(f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(200, json={"categorias": [], "modelos": []})
    )
    result = await runtime.execute("buscar_maquinas", {"consulta": "excavadora anfibia"})
    assert result["ok"] is True
    assert result["maquinas"] == []
    assert "no tiene nada que coincida" in result["instrucciones"]


# -------------------------------------------------------- disponibilidad ---


async def test_disponibilidad_emite_ofertas_y_las_espeja(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    route = respx_mock.get(f"{CRM_URL}/api/bot/disponibilidad").mock(
        return_value=httpx.Response(
            200,
            json={
                "disponible": True,
                "ofertas": [
                    {
                        "ofertaId": "roff_nueva",
                        "modeloId": MODELO_ID,
                        "desde": "2026-11-03",
                        "hasta": "2026-11-08",
                        "montoCotizadoCents": 111_925_000,
                        "etiqueta": "Retroexcavadora JCB 3CX, 3 nov al 8 nov",
                    }
                ],
            },
        )
    )
    result = await runtime.execute(
        "consultar_disponibilidad",
        {"modelo_id": MODELO_ID, "desde": "2026-11-03", "hasta": "2026-11-08"},
    )
    assert result["ok"] is True and result["disponible"] is True
    assert result["ofertas"][0]["oferta_id"] == "roff_nueva"
    assert result["ofertas"][0]["precio_total_sin_iva"] == "$1.119.250"
    # La conversación viaja SIEMPRE: es contra ella que el CRM registra la oferta.
    assert route.calls[0].request.url.params["conversationId"] == CRM_CONV_ID
    # El espejo se reemplaza completo: la vigente es la última ronda.
    offers = await ctx.store.get_rental_offers(conv.id)
    assert [o.offer_id for o in offers] == ["roff_nueva"]


async def test_disponibilidad_sin_stock_trae_salida_no_un_no_seco(
    runtime_y_ctx, respx_mock
):
    """El punto comercial del sistema: un 'no hay' pelado pierde el lead."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.get(f"{CRM_URL}/api/bot/disponibilidad").mock(
        return_value=httpx.Response(
            200,
            json={
                "disponible": False,
                "motivo": "ocupado",
                "proximaFechaLibre": "2026-10-13",
                "alternativas": [
                    {
                        "ofertaId": "roff_alt",
                        "modeloId": "mmod_cat416",
                        "desde": "2026-10-05",
                        "hasta": "2026-10-12",
                        "montoCotizadoCents": 121_000_000,
                        "etiqueta": "Retroexcavadora CAT 416F2, 5 oct al 12 oct",
                    }
                ],
            },
        )
    )
    result = await runtime.execute(
        "consultar_disponibilidad",
        {"modelo_id": MODELO_ID, "desde": "2026-10-05", "hasta": "2026-10-12"},
    )
    assert result["disponible"] is False
    assert result["proxima_fecha_libre"] == "2026-10-13"
    assert result["alternativas"][0]["oferta_id"] == "roff_alt"
    # Y la alternativa queda RESERVABLE: el lead puede aceptarla en el acto.
    offers = await ctx.store.get_rental_offers(conv.id)
    assert [o.offer_id for o in offers] == ["roff_alt"]


async def test_disponibilidad_modelo_inventado_manda_al_catalogo(
    runtime_y_ctx, respx_mock
):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.get(f"{CRM_URL}/api/bot/disponibilidad").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "not_found", "message": "Modelo desconocido"}}
        )
    )
    result = await runtime.execute(
        "consultar_disponibilidad",
        {"modelo_id": "mmod_inventado", "desde": "2026-10-05", "hasta": "2026-10-12"},
    )
    assert result["ok"] is False
    assert result["error"] == "modelo_desconocido"
    assert "buscar_maquinas" in result["detalle"]


# -------------------------------------------------------------- cotizar ---


async def test_cotizar_devuelve_el_desglose_del_servidor(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    route = respx_mock.post(f"{CRM_URL}/api/bot/cotizar").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelo": "Retroexcavadora JCB 3CX",
                "dias": 7,
                "horasPorDia": 8,
                "horasPedidas": 56,
                "horasFacturadas": 56,
                "minimoHoras": 0,
                "tarifaHoraCents": 3_200_000,
                "desglose": {
                    "maquinaCents": 179_200_000,
                    "trasladoCents": 10_800_000,
                    "totalCents": 190_000_000,
                },
                "incluyeOperario": True,
                "incluyeCombustible": True,
                "incluyeIva": False,
                "requiereOperario": True,
            },
        )
    )
    result = await runtime.execute(
        "cotizar",
        {
            "modelo_id": MODELO_ID,
            "dias": 7,
            "horas_por_dia": 8,
            "con_traslado": True,
            "km": 40,
        },
    )
    assert result["ok"] is True
    assert result["horas_facturadas"] == 56
    assert result["precio_por_hora"] == "$32.000"
    # El nombre del campo es parte del contrato: el modelo copia "sin_iva".
    assert result["total_sin_iva"] == "$1.900.000"
    assert result["traslado"] == "$108.000"
    # Sin mínimo que aplicar, no se le cuenta al modelo un renglón que no pasó.
    assert result["minimo_aplicado"] is None
    assert "+ IVA" in result["instrucciones"]
    assert "operario y combustible" in result["instrucciones"]
    body = json.loads(route.calls[0].request.content)
    assert body["km"] == 40
    assert body["horasPorDia"] == 8


async def test_cotizar_sin_horas_pregunta_en_vez_de_suponer_una_jornada(
    runtime_y_ctx, respx_mock
):
    """El negocio cotiza la hora: suponer 8 sería cotizarle al lead una
    jornada que nunca pidió. Se devuelve la pregunta, no un número."""
    runtime, ctx, conv = runtime_y_ctx
    cotizar = respx_mock.post(f"{CRM_URL}/api/bot/cotizar").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await runtime.execute("cotizar", {"modelo_id": MODELO_ID, "dias": 3})
    assert result["ok"] is False
    assert result["error"] == "faltan_horas"
    assert "cuántas horas por día" in result["detalle"]
    # Y no se gastó un viaje al CRM para averiguar lo que ya se sabía.
    assert cotizar.call_count == 0


async def test_cotizar_avisa_cuando_se_factura_el_minimo(runtime_y_ctx, respx_mock):
    """Si el mínimo del tarifario pisa lo pedido, el agente tiene que poder
    explicar por qué el número no es horas × tarifa."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/cotizar").mock(
        return_value=httpx.Response(
            200,
            json={
                "modelo": "Retroexcavadora JCB 3CX",
                "dias": 1,
                "horasPorDia": 2,
                "horasPedidas": 2,
                "horasFacturadas": 4,
                "minimoHoras": 4,
                "tarifaHoraCents": 3_200_000,
                "desglose": {
                    "maquinaCents": 12_800_000,
                    "trasladoCents": 0,
                    "totalCents": 12_800_000,
                },
                "incluyeIva": False,
            },
        )
    )
    result = await runtime.execute(
        "cotizar", {"modelo_id": MODELO_ID, "dias": 1, "horas_por_dia": 2}
    )
    assert result["ok"] is True
    assert result["minimo_aplicado"] is not None
    assert "mínimo de 4 horas" in result["minimo_aplicado"]
    assert result["total_sin_iva"] == "$128.000"


async def test_cotizar_sin_tarifa_no_deja_inventar_un_precio(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/cotizar").mock(
        return_value=httpx.Response(
            409, json={"error": {"code": "sin_tarifa", "message": "sin tarifa"}}
        )
    )
    result = await runtime.execute(
        "cotizar", {"modelo_id": MODELO_ID, "dias": 3, "horas_por_dia": 8}
    )
    assert result["ok"] is False
    assert result["error"] == "sin_tarifa"
    assert "no inventes" in result["detalle"]


# ------------------------------------------------------------- reservar ---


async def test_reserva_rechaza_oferta_no_emitida_sin_tocar_el_crm(
    runtime_y_ctx, respx_mock
):
    """El freno barato: la alucinación se corta ANTES del viaje de red."""
    runtime, ctx, conv = runtime_y_ctx
    reservas = respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(201, json={"reserva": {}})
    )
    result = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": "roff_inventada", "fechas_confirmadas": "dale, esa"},
    )
    assert result["ok"] is False
    assert result["error"] == "oferta_desconocida"
    assert reservas.call_count == 0
    assert runtime.booked is False
    # Se le devuelve lo que SÍ está vigente, para que re-ofrezca en vez de insistir.
    assert result["ofertas_vigentes"][0]["oferta_id"] == OFERTA_ID


async def test_reserva_por_numero_de_opcion(runtime_y_ctx, respx_mock):
    """Los modelos chicos no copian un nanoid opaco: mandan "1". Direccionar
    por número resuelve contra el MISMO espejo, así que no afloja la garantía:
    sigue siendo imposible reservar algo que el servidor no ofreció."""
    runtime, ctx, conv = runtime_y_ctx
    reservas = respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(201, json={"reserva": {"estado": "tentativa"}})
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}})
    )
    result = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": "1", "fechas_confirmadas": "dale esa"},
    )
    assert result["ok"] is True
    # Al CRM viaja el id REAL, no el número.
    assert json.loads(reservas.calls[0].request.content)["ofertaId"] == OFERTA_ID


async def test_un_id_con_pinta_de_real_pero_ajeno_sigue_rechazado(
    runtime_y_ctx, respx_mock
):
    """El atajo por número no puede volverse una puerta: un roff_ inventado
    se rechaza igual que antes."""
    runtime, ctx, conv = runtime_y_ctx
    reservas = respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(201, json={"reserva": {}})
    )
    for fake in ("roff_otra_conversacion", "roff_9", "99"):
        result = await runtime.execute(
            "crear_reserva_tentativa",
            {"oferta_id": fake, "fechas_confirmadas": "dale"},
        )
        assert result["error"] == "oferta_desconocida", fake
    assert reservas.call_count == 0


async def test_reserva_con_oferta_emitida_queda_tentativa(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    reservas = respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            201,
            json={
                "reserva": {
                    "reservaId": "rent_1",
                    "estado": "tentativa",
                    "desde": "2026-10-05",
                    "hasta": "2026-10-12",
                    "montoCotizadoCents": 139_150_000,
                    "expiraEn": "2026-09-04T00:00:00Z",
                }
            },
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}})
    )
    result = await runtime.execute(
        "crear_reserva_tentativa",
        {
            "oferta_id": OFERTA_ID,
            "fechas_confirmadas": "sí, del 5 al 12 de octubre",
            "localidad_obra": "Alta Gracia",
        },
    )
    assert result["ok"] is True
    assert result["estado"] == "tentativa"
    assert result["precio_total_sin_iva"] == "$1.391.500"
    # Y la instrucción le prohíbe explícitamente decir "confirmada".
    assert "NUNCA digas 'confirmada'" in result["instrucciones"]
    assert runtime.booked is True
    body = json.loads(reservas.calls[0].request.content)
    assert body["ofertaId"] == OFERTA_ID
    assert body["localidadObra"] == "Alta Gracia"
    # Consumida la oferta, el espejo queda limpio.
    assert await ctx.store.get_rental_offers(conv.id) == []


async def test_reserva_perdida_por_carrera_reofrece_la_misma_maquina(
    runtime_y_ctx, respx_mock
):
    """recien_tomada con ofertas frescas: se re-ofrece en el MISMO turno."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {"code": "recien_tomada", "message": "otro lead ganó"},
                "ofertas": [
                    {
                        "ofertaId": "roff_fresca",
                        "modeloId": MODELO_ID,
                        "desde": "2026-10-05",
                        "hasta": "2026-10-12",
                        "montoCotizadoCents": 139_150_000,
                        "etiqueta": "Retroexcavadora JCB 3CX, 5 oct al 12 oct",
                    }
                ],
                "alternativas": [],
            },
        )
    )
    result = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "dale"},
    )
    assert result["ok"] is False
    assert result["error"] == "recien_tomada"
    assert result["ofertas"][0]["oferta_id"] == "roff_fresca"
    assert runtime.booked is False
    # La fresca ya es reservable: el lead acepta y se cierra sin otra consulta.
    offers = await ctx.store.get_rental_offers(conv.id)
    assert [o.offer_id for o in offers] == ["roff_fresca"]


async def test_reserva_perdida_sin_otra_igual_ofrece_alternativas(
    runtime_y_ctx, respx_mock
):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {"code": "recien_tomada", "message": "otro lead ganó"},
                "ofertas": [],
                "alternativas": [
                    {
                        "modeloId": "mmod_cat416",
                        "nombre": "Retroexcavadora CAT 416F2",
                        "tarifaHoraCents": 2_800_000,
                    }
                ],
            },
        )
    )
    result = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "dale"},
    )
    assert result["error"] == "recien_tomada"
    assert result["alternativas"][0]["nombre"] == "Retroexcavadora CAT 416F2"
    assert result["alternativas"][0]["precio_por_hora"] == "$28.000"


async def test_reserva_con_oferta_vencida_manda_a_reconsultar(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            409, json={"error": {"code": "oferta_vencida", "message": "venció"}}
        )
    )
    result = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "dale"},
    )
    assert result["error"] == "oferta_vencida"
    assert "consultar_disponibilidad" in result["detalle"]
    assert runtime.booked is False


async def test_reserva_con_humano_al_mando_se_frena(runtime_y_ctx, respx_mock):
    """Gate de handoff del CRM: si un asesor tomó la conversación, no se reserva."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.post(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            409, json={"error": {"code": "ai_paused", "message": "IA en pausa"}}
        )
    )
    result = await runtime.execute(
        "crear_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "dale"},
    )
    assert result["error"] == "ai_paused"
    assert runtime.booked is False


async def test_cambiar_reserva_mueve_en_vez_de_duplicar(runtime_y_ctx, respx_mock):
    """Sin esta tool, un cambio de fechas dejaba DOS máquinas bloqueadas."""
    runtime, ctx, conv = runtime_y_ctx
    patch = respx_mock.patch(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            200,
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
    result = await runtime.execute(
        "cambiar_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "mejor del 5 al 12"},
    )
    assert result["ok"] is True
    assert result["estado"] == "tentativa"
    assert json.loads(patch.calls[0].request.content)["ofertaId"] == OFERTA_ID
    assert await ctx.store.get_rental_offers(conv.id) == []


async def test_cambiar_sin_reserva_previa_manda_a_crear(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.patch(f"{CRM_URL}/api/bot/reservas").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "reserva_inexistente", "message": "no hay"}}
        )
    )
    result = await runtime.execute(
        "cambiar_reserva_tentativa",
        {"oferta_id": OFERTA_ID, "fechas_confirmadas": "movela"},
    )
    assert result["error"] == "sin_reserva"
    assert "crear_reserva_tentativa" in result["detalle"]


# ------------------------------------------------------- ficha y varios ---


async def test_update_ficha_manda_lo_que_haya(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    ficha_route = respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}, "stageMoved": False})
    )
    result = await runtime.execute(
        "update_ficha",
        {
            "tipo_obra": "zanjeo para cloacas",
            "localidad_obra": "Malagueño",
            "campo_raro": "x",
        },
    )
    assert result["ok"] is True
    body = json.loads(ficha_route.calls[0].request.content)
    # drift tolerado: se manda tal cual, el CRM normaliza flojo
    assert body["ficha"]["tipo_obra"] == "zanjeo para cloacas"
    assert body["ficha"]["campo_raro"] == "x"


async def test_handoff_se_difiere_al_final_del_turno(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    handoff_route = respx_mock.post(f"{CRM_URL}/api/bot/handoff").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await runtime.execute("handoff", {"reason": "pide descuento"})
    assert result["ok"] is True
    assert runtime.handoff_reason == "pide descuento"
    # la tool NO llama al CRM: turn.py lo hace después de la despedida
    assert handoff_route.call_count == 0


async def test_crm_caido_en_tool_no_tumba_el_turno(runtime_y_ctx, respx_mock):
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(return_value=httpx.Response(500))
    result = await runtime.execute("update_ficha", {"tipo_obra": "playón"})
    assert result["ok"] is False
    assert result["error"] == "crm_error"


async def test_sin_inventario_se_apagan_las_tools_de_maquinaria(
    runtime_y_ctx, respx_mock
):
    """Un CRM sin la bandera INVENTARIO: mejor que la herramienta no exista a
    que el modelo la llame, falle y el lead reciba evasivas."""
    runtime, ctx, conv = runtime_y_ctx
    respx_mock.get(f"{CRM_URL}/api/bot/catalogo").mock(
        return_value=httpx.Response(404)
    )
    result = await runtime.execute("buscar_maquinas", {})
    assert result["ok"] is False
    assert result["error"] == "sin_inventario"
    # Y queda apagado para el resto de la vida del proceso.
    assert ctx.inventory_enabled is False
    nombres = {t["function"]["name"] for t in tool_schemas(False)}
    assert "buscar_maquinas" not in nombres
    assert "crear_reserva_tentativa" not in nombres
    assert {"update_ficha", "handoff", "route_out"} <= nombres
