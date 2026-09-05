"""Herramientas del LLM: buscar_maquinas, consultar_disponibilidad, cotizar,
crear_reserva_tentativa, update_ficha, route_out, handoff.

017 (fork RPM) — Alquiler de maquinaria. El principio es el mismo que tenía el
agendamiento y no se negocia: **solo se reserva lo que el servidor ofreció**.
Vocero emite un `oferta_id` contra la conversación y solo acepta ese; la tabla
`rental_offers` de Nea es un ESPEJO de esa oferta, no una segunda fuente de
verdad. Sirve para etiquetar bonito y para frenar una alucinación antes de
gastar un viaje de red. Si el CRM rechaza un `oferta_id`, su palabra manda.

Esto elimina de raíz la clase entera de bugs "el agente prometió una retro que
no existe, para una fecha ocupada, a un precio inventado": el modelo no puede
nombrar una máquina fuera del catálogo, no puede calcular un precio, y no
puede reservar nada que el servidor no le haya ofrecido antes.

Un fallo del CRM dentro de una tool regresa `{"ok": false, ...}` al LLM —
nunca tumba el turno.
"""
from __future__ import annotations

import logging
from typing import Any

from app.crm import (
    CrmConflict,
    CrmError,
    InventoryUnavailable,
    RecentlyTaken,
)
from app.profile import BusinessProfile
from app.state import AppContext, Conversation, RentalOffer

logger = logging.getLogger("nea.tools")

# Cuántas ofertas quedan RESERVABLES tras una consulta. Se guardan todas las
# que mande el CRM (la del modelo pedido y sus alternativas): el catálogo
# reservable es más ancho que lo que el agente enseña en un mensaje.
MAX_OFFERED = 8

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "update_ficha",
            "description": (
                "Guarda o actualiza la ficha del lead en el CRM (merge: solo los "
                "campos que mandes). Llamala apenas descubras un dato nuevo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tipo_obra": {
                        "type": "string",
                        "description": "Qué está construyendo o haciendo (ej. 'zanjeo para cloacas', 'playón de hormigón')",
                    },
                    "localidad_obra": {
                        "type": "string",
                        "description": "Dónde es la obra (localidad/barrio). Define el traslado.",
                    },
                    "maquina_interes": {
                        "type": "string",
                        "description": "El modelo del catálogo que le interesa, con el nombre EXACTO del catálogo",
                    },
                    "duracion_estimada": {
                        "type": "string",
                        "description": "Cuántos días/semanas la necesita",
                    },
                    "fecha_inicio_deseada": {
                        "type": "string",
                        "description": "Cuándo la quiere arrancar (como lo dijo el lead)",
                    },
                    "horas_por_dia": {
                        "type": "number",
                        "description": (
                            "Horas de trabajo por dia que necesita la maquina "
                            "(8 = jornada completa). Define el precio: sin esto "
                            "no se puede cotizar."
                        ),
                    },
                    "requiere_traslado": {"type": "boolean"},
                    "empresa": {
                        "type": "string",
                        "description": "Empresa o constructora, si la menciona",
                    },
                    "calificado": {"type": "boolean"},
                    "resultado": {
                        "type": "string",
                        "description": "reservo | cotizo | handoff | descartado | sin_respuesta",
                    },
                    "notas": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_maquinas",
            "description": (
                "Consulta el catálogo REAL de máquinas del negocio: modelos, "
                "marcas, specs y el precio de la HORA de cada una. Usala apenas "
                "el lead insinúe qué necesita, incluso si lo dice vago ('algo "
                "para mover tierra'): te devuelve de qué dispone el negocio y "
                "recién ahí podés recomendar.\n"
                "La tarifa por hora que trae SÍ se la podés decir al lead tal "
                "cual —es del negocio, no la calculaste vos— y alcanza para "
                "contestar '¿cuánto sale?'. El precio de una obra concreta, en "
                "cambio, sale de cotizar.\n"
                "REGLA DURA: no podés nombrarle al lead NINGUNA máquina que no "
                "haya salido de esta herramienta. Si no está en el catálogo, el "
                "negocio no la tiene — decilo derecho y ofrecé lo que sí hay."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {
                        "type": "string",
                        "description": (
                            "Palabras del lead para filtrar (ej. 'retro', 'grúa', "
                            "'mover tierra'). Vacío = catálogo completo."
                        ),
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_disponibilidad",
            "description": (
                "Pregunta si una máquina concreta está libre en un rango de "
                "fechas, y de paso EMITE la oferta reservable. Necesita el "
                "modelo_id exacto que te dio buscar_maquinas.\n"
                "El precio de la oferta se calcula sobre las horas por día: si "
                "el lead ya te dijo cuántas necesita, pasalas; si todavía no lo "
                "hablaron, dejá el campo vacío y se cotiza jornada completa de 8 "
                "horas (la respuesta te lo aclara para que se lo digas).\n"
                "Te devuelve una de dos cosas:\n"
                "· disponible=true con una o más ofertas, cada una con su "
                "oferta_id, su etiqueta y su precio total. SOLO estas ofertas "
                "serán reservables después: ningún otro id, ninguna otra fecha.\n"
                "· disponible=false con la próxima fecha libre y alternativas "
                "de máquinas parecidas que SÍ están libres en ese rango. Nunca "
                "le digas 'no hay' a secas: ofrecé la fecha o la alternativa.\n"
                "El precio que viene acá es el que podés decir. No lo redondees "
                "ni le sumes nada de tu cabeza."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "modelo_id": {
                        "type": "string",
                        "description": "El modelo_id EXACTO del catálogo",
                    },
                    "desde": {
                        "type": "string",
                        "description": "Primer día del alquiler, AAAA-MM-DD",
                    },
                    "hasta": {
                        "type": "string",
                        "description": (
                            "Día de devolución, AAAA-MM-DD. Es exclusivo: del 5 "
                            "al 12 son 7 días de alquiler."
                        ),
                    },
                    "horas_por_dia": {
                        "type": "number",
                        "description": (
                            "Horas de trabajo por día, si el lead ya las dijo "
                            "(8 = jornada completa, 4 = media). Vacío = se "
                            "cotiza jornada completa."
                        ),
                    },
                },
                "required": ["modelo_id", "desde", "hasta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cotizar",
            "description": (
                "Pide el precio de un alquiler: las horas de máquina y, si "
                "hace falta, el traslado. Usala cuando el lead pregunte cuánto "
                "sale una obra concreta, o cuando necesites sumarle el traslado "
                "a una oferta.\n"
                "El negocio cotiza la HORA de máquina, y esa hora YA incluye el "
                "operario y el combustible. Por eso necesita horas_por_dia: sin "
                "eso no hay precio. Si el lead no las dijo, preguntáselas antes "
                "de llamar acá.\n"
                "El total que te devuelve es SIN IVA, y eso se lo tenés que "
                "aclarar al lead cada vez que digas un precio.\n"
                "REGLA DURA: los precios SOLO salen de acá, de una oferta o de "
                "la tarifa por hora del catálogo. Nunca los calcules, los "
                "estimes, los redondees ni los 'aproximes' vos — ni siquiera si "
                "el lead te apura o si te parece obvio multiplicar.\n"
                "OJO: cotizar NO chequea si la máquina está libre y NO emite "
                "ninguna oferta reservable. Si el lead va a avanzar con esas "
                "fechas, necesitás consultar_disponibilidad igual."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "modelo_id": {
                        "type": "string",
                        "description": "El modelo_id EXACTO del catálogo",
                    },
                    "dias": {"type": "integer", "description": "Días de alquiler"},
                    "horas_por_dia": {
                        "type": "number",
                        "description": (
                            "Horas de trabajo por día que pactaste con el lead. "
                            "8 = jornada completa, 4 = media jornada. Si no lo "
                            "hablaste todavía, preguntáselo antes de cotizar."
                        ),
                    },
                    "con_traslado": {
                        "type": "boolean",
                        "description": "Si el negocio lleva y trae la máquina",
                    },
                    "km": {
                        "type": "number",
                        "description": "Distancia a la obra en km (solo si con_traslado)",
                    },
                },
                "required": ["modelo_id", "dias", "horas_por_dia"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crear_reserva_tentativa",
            "description": (
                "Toma la máquina para el lead. oferta_id tiene que ser "
                "EXACTAMENTE uno de los que te dio consultar_disponibilidad en "
                "ESTA conversación — no inventes uno ni reuses uno viejo.\n"
                "Llamala SOLO después de haber nombrado la máquina, las fechas "
                "y el precio completos, y de que el lead aceptara sin "
                "ambigüedad. `fechas_confirmadas` es lo que el lead escribió "
                "para aceptar ESE rango: si no lo podés citar, todavía no "
                "confirmó — preguntá en vez de reservar.\n"
                "OJO con lo que decís después: la reserva queda TENTATIVA. "
                "Nunca digas 'confirmada' ni 'ya está reservada en firme': "
                "decile que se la dejás tomada y que un asesor lo confirma."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "oferta_id": {
                        "type": "string",
                        "description": "El oferta_id exacto de una oferta de esta conversación",
                    },
                    "fechas_confirmadas": {
                        "type": "string",
                        "description": (
                            "Lo que el lead escribió para aceptar ESE rango de "
                            "fechas. Si no podés citarlo, no confirmó todavía."
                        ),
                    },
                    "localidad_obra": {
                        "type": "string",
                        "description": "Dónde se entrega, si el lead ya lo dijo",
                    },
                },
                "required": ["oferta_id", "fechas_confirmadas"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cambiar_reserva_tentativa",
            "description": (
                "Mueve la reserva que YA le tomaste al lead EN ESTA "
                "conversación a otra oferta, cuando cambió de fechas o de "
                "máquina antes de que un asesor se la confirme. Mismo "
                "protocolo que crear_reserva_tentativa: primero "
                "consultar_disponibilidad con las fechas nuevas, después "
                "confirmás con el lead, y recién ahí movés con el oferta_id "
                "nuevo.\n"
                "Usá ESTA y no crear_reserva_tentativa cuando ya le tomaste "
                "una: si creás otra, el negocio queda con dos máquinas "
                "bloqueadas para el mismo cliente.\n"
                "Si la reserva ya se la confirmó un asesor, o si el lead quiere "
                "CANCELAR, esto no aplica: eso lo decide una persona, hacé "
                "handoff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "oferta_id": {
                        "type": "string",
                        "description": "El oferta_id nuevo, de esta conversación",
                    },
                    "fechas_confirmadas": {
                        "type": "string",
                        "description": "Lo que el lead escribió para aceptar el rango NUEVO",
                    },
                },
                "required": ["oferta_id", "fechas_confirmadas"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "route_out",
            "description": (
                "Marca al lead como no calificado (hoy). Después despedite con "
                "honestidad, compartiendo los recursos alternativos del negocio "
                "si existen, puerta abierta."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff",
            "description": (
                "Pasa la conversación a un humano del negocio y pausa la IA. Tu "
                "mensaje de despedida se envía ANTES de la pausa — salvo en el "
                "handoff por hostilidad, donde cerrás sobrio sin anunciarlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo breve (p.ej. 'pidió humano', 'pide descuento')",
                    }
                },
            },
        },
    },
]


# Herramientas que solo tienen sentido si el CRM tiene inventario.
INVENTORY_TOOLS = frozenset(
    {
        "buscar_maquinas",
        "consultar_disponibilidad",
        "cotizar",
        "crear_reserva_tentativa",
        "cambiar_reserva_tentativa",
    }
)


def tool_schemas(inventory_enabled: bool = True) -> list[dict[str, Any]]:
    """El catálogo que se le ofrece al modelo en ESTE turno.

    Contra un CRM sin inventario no se le enseñan las herramientas de
    maquinaria: si se le enseñan, las llama, fallan todas y el lead recibe
    evasivas en vez de un handoff limpio. Que no exista la herramienta es más
    claro que pedirle al prompt que se acuerde de no usarla.
    """
    if inventory_enabled:
        return TOOL_SCHEMAS
    return [
        t
        for t in TOOL_SCHEMAS
        if t.get("function", {}).get("name") not in INVENTORY_TOOLS
    ]


def _pesos(cents: Any) -> str:
    """Centavos → '$1.391.500'. El LLM copia esto tal cual: nada de decimales
    ni de notación científica, que el modelo después lee mal y dice otra cosa."""
    try:
        value = int(cents) // 100
    except (TypeError, ValueError):
        return "?"
    return "$" + f"{value:,}".replace(",", ".")


def _horas(raw: Any) -> float | None:
    """Las horas por día que mandó el modelo, o None si no mandó nada usable.

    Devuelve None en vez de un default: el que decide qué hacer sin horas es
    cada handler —cotizar corta y pregunta, disponibilidad deja que el CRM
    cotice la jornada y lo aclare—, y un default acá borraría esa diferencia.
    """
    if raw is None or raw == "":
        return None
    try:
        # El modelo a veces manda "8" o "8 horas"; lo primero se salva solo.
        horas = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if not (0 < horas <= 24):
        return None
    return horas


def _offers_from_payload(
    conversation_id: int, raw_offers: list[dict[str, Any]]
) -> list[RentalOffer]:
    """Convierte ofertas del CRM a RentalOffer, tolerante a campos faltantes."""
    out: list[RentalOffer] = []
    for raw in raw_offers[:MAX_OFFERED]:
        offer_id = str(raw.get("ofertaId") or "").strip()
        if not offer_id:
            continue
        out.append(
            RentalOffer(
                conversation_id=conversation_id,
                offer_id=offer_id,
                model_id=str(raw.get("modeloId") or ""),
                label=str(raw.get("etiqueta") or offer_id),
                desde=str(raw.get("desde") or ""),
                hasta=str(raw.get("hasta") or ""),
                amount_cents=int(raw.get("montoCotizadoCents") or 0),
            )
        )
    return out


def _offers_for_llm(offers: list[RentalOffer]) -> list[dict[str, Any]]:
    return [
        {
            # `opcion` existe porque los modelos chicos NO copian un nanoid
            # opaco: mandan "1" o "oferta_id_1" y la reserva se caía. El número
            # es una forma de DIRECCIONAR una oferta ya emitida, no un permiso
            # nuevo: se resuelve contra este mismo espejo, así que la garantía
            # ("solo se reserva lo que el servidor ofreció") no se toca.
            "opcion": i,
            "oferta_id": o.offer_id,
            "modelo_id": o.model_id,
            "etiqueta": o.label,
            "desde": o.desde,
            "hasta": o.hasta,
            "precio_total_sin_iva": _pesos(o.amount_cents),
        }
        for i, o in enumerate(offers, start=1)
    ]


def _match_offer(
    wanted: str, offered: list[RentalOffer]
) -> RentalOffer | None:
    """La oferta que el modelo quiso nombrar, o None.

    Acepta el `oferta_id` exacto (lo correcto) y, como red de seguridad, el
    número de opción tal como se le mostró ("2", "opcion 2", "oferta_id_2").
    Todo se resuelve contra las ofertas EMITIDAS en esta conversación: nunca
    se acepta algo que el servidor no ofreció.
    """
    w = wanted.strip()
    if not w:
        return None
    exact = next((o for o in offered if o.offer_id == w), None)
    if exact is not None:
        return exact
    # Solo se cae al número si NO parece un id real (los ids del CRM traen
    # prefijo): así un id viejo o de otra conversación sigue siendo rechazo.
    digits = "".join(ch for ch in w if ch.isdigit())
    if digits and not w.startswith("roff_") and len(digits) <= 2:
        idx = int(digits)
        if 1 <= idx <= len(offered):
            logger.info(
                "tools: el modelo direccionó la oferta por número (%r → %s)",
                w,
                offered[idx - 1].offer_id,
            )
            return offered[idx - 1]
    return None


class ToolRuntime:
    """Ejecuta las tool-calls de UN turno y acumula sus efectos."""

    def __init__(
        self,
        ctx: AppContext,
        conv: Conversation,
        crm_conversation_id: str,
        profile: BusinessProfile | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        self._ctx = ctx
        self._conv = conv
        self._crm_conv_id = crm_conversation_id
        self._profile = profile or BusinessProfile()
        # Fase 7 — Bitácora del turno para el Laboratorio: qué herramienta se
        # llamó, con qué y qué contestó. En producción es None y no cuesta
        # nada. Sin ella el juez del Lab solo ve el texto, y en alquiler las
        # fallas graves no se ven en el texto: un precio inventado y uno
        # cotizado se leen exactamente igual.
        self._trace = trace
        # Efectos observables por turn.py:
        self.handoff_reason: str | None = None  # se ejecuta DESPUÉS de la despedida
        self.booked = False
        self.routed_out = False
        self.proposed = False

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._execute(name, args)
        if self._trace is not None:
            self._trace.append(
                {"herramienta": name, "argumentos": args, "resultado": result}
            )
        return result

    async def _execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "update_ficha":
                return await self._update_ficha(args)
            if name == "buscar_maquinas":
                return await self._buscar_maquinas(args)
            if name == "consultar_disponibilidad":
                return await self._consultar_disponibilidad(args)
            if name == "cotizar":
                return await self._cotizar(args)
            if name == "crear_reserva_tentativa":
                return await self._crear_reserva(args)
            if name == "cambiar_reserva_tentativa":
                return await self._cambiar_reserva(args)
            if name == "route_out":
                return await self._route_out()
            if name == "handoff":
                return self._handoff(args)
            logger.warning("tools: herramienta desconocida %r", name)
            return {"ok": False, "error": f"herramienta desconocida: {name}"}
        except CrmError as exc:
            logger.warning("tools: %s falló contra el CRM: %s", name, exc)
            return {
                "ok": False,
                "error": "crm_error",
                "detalle": "no pude completar la acción; seguí la conversación o hacé handoff",
            }

    async def _update_ficha(self, args: dict[str, Any]) -> dict[str, Any]:
        # Tolera el drift del LLM: manda lo que haya, el CRM normaliza flojo.
        ficha = {k: v for k, v in args.items() if v is not None}
        if not ficha:
            return {"ok": True, "nota": "sin campos nuevos"}
        await self._ctx.crm.put_ficha(self._crm_conv_id, ficha)
        return {"ok": True}

    def _sin_inventario(self) -> dict[str, Any]:
        """Este CRM no tiene catálogo: dejar de prometer máquinas, no reintentar."""
        self._ctx.inventory_enabled = False
        logger.info("tools: el CRM no expone inventario — maquinaria desactivada")
        return {
            "ok": False,
            "error": "sin_inventario",
            "detalle": (
                "este negocio no maneja el catálogo por acá; no ofrezcas "
                "máquinas ni precios — resolvé lo que puedas y hacé handoff"
            ),
        }

    async def _buscar_maquinas(self, args: dict[str, Any]) -> dict[str, Any]:
        consulta = str(args.get("consulta") or "").strip()
        try:
            data = await self._ctx.crm.get_catalogo(consulta or None)
        except InventoryUnavailable:
            return self._sin_inventario()
        modelos = list(data.get("modelos") or [])
        if not modelos:
            # Con filtro y sin resultados, reintentar sin filtro sería adivinar
            # por el lead: mejor decirle que de eso no hay y mostrar qué sí hay.
            return {
                "ok": True,
                "maquinas": [],
                "instrucciones": (
                    "el catálogo no tiene nada que coincida con eso. Decíselo "
                    "derecho y volvé a llamar buscar_maquinas SIN consulta para "
                    "ofrecerle lo que el negocio sí tiene."
                ),
            }
        maquinas = []
        for m in modelos:
            tarifa = m.get("tarifa") or {}
            maquinas.append(
                {
                    "modelo_id": m.get("modeloId"),
                    "nombre": m.get("nombre"),
                    "marca": m.get("marca"),
                    "categoria": m.get("categoria"),
                    "descripcion": m.get("descripcion"),
                    "specs": m.get("specs") or {},
                    "va_con_operario": bool(m.get("requiereOperario")),
                    "unidades_en_flota": m.get("unidades"),
                    "precio_por_hora": _pesos(tarifa.get("horaCents"))
                    if tarifa.get("horaCents") is not None
                    else None,
                    "minimo_horas": tarifa.get("minimoHoras") or None,
                }
            )
        return {
            "ok": True,
            "maquinas": maquinas,
            "instrucciones": (
                "estas son TODAS las máquinas del negocio que aplican: no "
                "nombres ninguna que no esté acá.\n"
                "El 'precio_por_hora' se lo podés decir tal cual —es la tarifa "
                "del negocio— aclarando SIEMPRE dos cosas: que ya incluye "
                "operario y combustible, y que es SIN IVA. Para el precio de "
                "una obra concreta (tantos días por tantas horas) usá cotizar; "
                "el traslado nunca está incluido y se cotiza aparte.\n"
                "'unidades_en_flota' NO es disponibilidad: para saber si está "
                "libre en unas fechas, consultar_disponibilidad, y una por una "
                "— que una esté tomada no dice NADA de las otras."
            ),
        }

    async def _consultar_disponibilidad(self, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("modelo_id") or "").strip()
        desde = str(args.get("desde") or "").strip()
        hasta = str(args.get("hasta") or "").strip()
        horas = _horas(args.get("horas_por_dia"))
        if not (model_id and desde and hasta):
            return {
                "ok": False,
                "error": "faltan_datos",
                "detalle": "necesito modelo_id del catálogo, desde y hasta (AAAA-MM-DD)",
            }
        try:
            data = await self._ctx.crm.get_disponibilidad(
                self._crm_conv_id, model_id, desde, hasta, horas_por_dia=horas
            )
        except InventoryUnavailable:
            return self._sin_inventario()
        except CrmConflict as exc:
            if exc.code in ("modelo_desconocido", "not_found"):
                return {
                    "ok": False,
                    "error": "modelo_desconocido",
                    "detalle": (
                        "ese modelo_id no existe en el catálogo; volvé a llamar "
                        "buscar_maquinas y usá un modelo_id de ahí"
                    ),
                }
            if exc.code == "rango_invalido":
                return {
                    "ok": False,
                    "error": "rango_invalido",
                    "detalle": "las fechas no son válidas; pedile al lead que las aclare",
                }
            if exc.code == "horas_invalidas":
                return {
                    "ok": False,
                    "error": "horas_invalidas",
                    "detalle": (
                        "las horas por día tienen que estar entre 0,5 y 24; "
                        "preguntale al lead cuántas horas necesita la máquina"
                    ),
                }
            if exc.code == "sin_tarifa":
                return {
                    "ok": False,
                    "error": "sin_tarifa",
                    "detalle": (
                        "esa máquina todavía no tiene precio cargado: no la "
                        "ofrezcas y hacé handoff para que la cotice una persona"
                    ),
                }
            raise

        disponible = bool(data.get("disponible"))
        raw = list(data.get("ofertas") or []) if disponible else list(
            data.get("alternativas") or []
        )
        offers = _offers_from_payload(self._conv.id, raw)
        # Reemplazo completo: la oferta vigente es siempre la última ronda.
        await self._ctx.store.replace_rental_offers(self._conv.id, offers)
        self.proposed = True

        if disponible:
            return {
                "ok": True,
                "disponible": True,
                "horas_por_dia": data.get("horasPorDia"),
                "ofertas": _offers_for_llm(offers),
                "instrucciones": (
                    "ofrecele la máquina con SU etiqueta y SU precio_total_sin_iva tal "
                    "cual. "
                    # La nota viene del CRM y dice sobre cuántas horas se
                    # calculó el monto: es la única forma de que el agente no
                    # cotice una jornada que el lead nunca pidió.
                    + str(data.get("nota") or "")
                    + " Cuando acepte, reservá con crear_reserva_tentativa "
                    "usando el oferta_id exacto. Si quiere traslado, cotizá "
                    "aparte."
                ),
            }
        return {
            "ok": True,
            "disponible": False,
            "motivo": data.get("motivo"),
            "proxima_fecha_libre": data.get("proximaFechaLibre"),
            "alternativas": _offers_for_llm(offers),
            "instrucciones": (
                "esa máquina está tomada en ese rango. NO cortes con un 'no "
                "hay': ofrecele la proxima_fecha_libre, o alguna de las "
                "alternativas (que son máquinas parecidas y SÍ están libres en "
                "las fechas que pidió, con su oferta_id ya reservable). Si no "
                "hay ni una ni otra, ahí sí hacé handoff."
            ),
        }

    async def _cotizar(self, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("modelo_id") or "").strip()
        try:
            dias = int(args.get("dias") or 0)
        except (TypeError, ValueError):
            dias = 0
        horas = _horas(args.get("horas_por_dia"))
        if not model_id or dias < 1:
            return {
                "ok": False,
                "error": "faltan_datos",
                "detalle": "necesito modelo_id del catálogo y cuántos días",
            }
        if horas is None:
            # Sin horas no hay precio, y suponer una jornada sería cotizarle al
            # lead algo que nunca pidió. Se devuelve la pregunta, no un número.
            return {
                "ok": False,
                "error": "faltan_horas",
                "detalle": (
                    "el negocio cotiza por hora de máquina: preguntale al lead "
                    "cuántas horas por día la necesita (jornada completa son 8) "
                    "y recién ahí volvé a cotizar"
                ),
            }
        con_traslado = bool(args.get("con_traslado"))
        km = args.get("km")
        try:
            data = await self._ctx.crm.post_cotizar(
                model_id,
                dias,
                horas,
                con_traslado=con_traslado,
                km=float(km) if km is not None else None,
            )
        except InventoryUnavailable:
            return self._sin_inventario()
        except CrmConflict as exc:
            if exc.code == "sin_tarifa":
                return {
                    "ok": False,
                    "error": "sin_tarifa",
                    "detalle": (
                        "esa máquina no tiene precio cargado: no inventes uno, "
                        "hacé handoff para que la cotice una persona"
                    ),
                }
            if exc.code in ("modelo_desconocido", "not_found"):
                return {
                    "ok": False,
                    "error": "modelo_desconocido",
                    "detalle": "ese modelo_id no existe; volvé a llamar buscar_maquinas",
                }
            raise

        g = data.get("desglose") or {}
        pedidas = data.get("horasPedidas")
        facturadas = data.get("horasFacturadas")
        minimo = (
            "se factura el mínimo de %s horas del tarifario, no las %s que pidió"
            % (data.get("minimoHoras"), pedidas)
            if facturadas is not None and pedidas is not None and facturadas != pedidas
            else None
        )
        return {
            "ok": True,
            "modelo": data.get("modelo"),
            "dias": data.get("dias"),
            "horas_por_dia": data.get("horasPorDia"),
            "horas_facturadas": facturadas,
            "minimo_aplicado": minimo,
            "precio_por_hora": _pesos(data.get("tarifaHoraCents")),
            "maquina": _pesos(g.get("maquinaCents")),
            "traslado": _pesos(g.get("trasladoCents")) if g.get("trasladoCents") else None,
            # El nombre del campo hace la mitad del trabajo: un modelo que copia
            # "total_sin_iva" difícilmente termine diciendo que el IVA está.
            "total_sin_iva": _pesos(g.get("totalCents")),
            "instrucciones": (
                "decí estos números TAL CUAL, sin redondear ni recalcular. Son "
                "%s horas de máquina (%s días × %s por día) a %s la hora.\n"
                "DOS COSAS QUE SIEMPRE van con el precio: que NO incluye IVA "
                "(decilo así, '$X + IVA') y que la máquina va con operario y "
                "combustible incluidos. Lo segundo es un argumento de venta, no "
                "una letra chica: usalo.\n"
                "El traslado NO está en ese total salvo que lo hayas pedido "
                "acá. %s\n"
                "ESTO NO RESERVÓ NADA ni verificó que la máquina esté libre. "
                "Si le vas a ofrecer tomarla en esas fechas, llamá AHORA "
                "consultar_disponibilidad con las MISMAS horas por día: sin eso "
                "no hay oferta y después no vas a poder reservarle nada."
            )
            % (
                facturadas,
                data.get("dias"),
                data.get("horasPorDia"),
                _pesos(data.get("tarifaHoraCents")),
                minimo or "",
            ),
        }

    async def _resolve_offer(
        self, args: dict[str, Any]
    ) -> tuple[RentalOffer | None, dict[str, Any] | None]:
        """La oferta elegida, o el error listo para devolverle al LLM.

        Validación local por id exacto contra el espejo: es el freno barato
        antes del viaje de red. El CRM vuelve a validar del otro lado y su
        palabra manda.
        """
        wanted = str(args.get("oferta_id") or "").strip()
        offered = await self._ctx.store.get_rental_offers(self._conv.id)
        chosen = _match_offer(wanted, offered)
        if chosen is None:
            logger.info(
                "tools: reserva rechazada — %r no está entre las ofertas emitidas",
                wanted[:60],
            )
            # El detalle es MUY concreto a propósito: cuando el modelo manda un
            # placeholder ("oferta_id_1") y solo se le dice "id inválido", se va
            # a re-consultar disponibilidad y quema las rondas del turno. Acá se
            # le dice qué copiar y de dónde.
            vigentes = _offers_for_llm(offered)
            if vigentes:
                detalle = (
                    "ese oferta_id no existe. NO inventes ids ni uses "
                    "placeholders: copiá TAL CUAL uno de los `oferta_id` que "
                    "vienen en `ofertas_vigentes` acá abajo y volvé a llamar "
                    "crear_reserva_tentativa con ese, en este mismo turno. No "
                    "hace falta volver a consultar disponibilidad."
                )
            else:
                detalle = (
                    "no hay ninguna oferta vigente en esta conversación: llamá "
                    "consultar_disponibilidad con las fechas que pidió el lead "
                    "y reservá con el oferta_id que te devuelva"
                )
            return None, {
                "ok": False,
                "error": "oferta_desconocida",
                "detalle": detalle,
                "ofertas_vigentes": vigentes,
            }
        # Deja rastro de sobre qué frase del lead se tomó la decisión: cuando
        # una reserva sale mal, esto dice si hubo confirmación o se asumió.
        logger.info(
            "tools: reserva de %s (el lead confirmó con: %r)",
            chosen.label,
            str(args.get("fechas_confirmadas") or "")[:120],
        )
        return chosen, None

    async def _recovery(self, exc: RecentlyTaken) -> dict[str, Any]:
        """Otro lead ganó la unidad: re-ofrecer sin cortar la conversación.

        El CRM manda la salida en el mismo cuerpo — ofertas frescas de la
        misma máquina y/o alternativas de la categoría. Se espeja lo que venga
        y el modelo vuelve a ofrecer en ESTE mismo turno, sin esperar otro
        mensaje del lead.
        """
        fresh = _offers_from_payload(self._conv.id, exc.ofertas)
        await self._ctx.store.replace_rental_offers(self._conv.id, fresh)
        logger.info(
            "tools: carrera perdida — %d oferta(s) fresca(s), %d alternativa(s)",
            len(fresh),
            len(exc.alternativas),
        )
        if fresh:
            return {
                "ok": False,
                "error": "recien_tomada",
                "detalle": (
                    "otro cliente tomó esa máquina hace un segundo. Discúlpate "
                    "en una línea y ofrecé esta, que es la misma máquina en las "
                    "mismas fechas y ya es reservable"
                ),
                "ofertas": _offers_for_llm(fresh),
            }
        if exc.alternativas:
            return {
                "ok": False,
                "error": "recien_tomada",
                "detalle": (
                    "otro cliente tomó esa máquina hace un segundo y no queda "
                    "otra igual. Discúlpate breve y ofrecele estas alternativas; "
                    "si le interesa alguna, consultá su disponibilidad para "
                    "poder reservarla"
                ),
                "alternativas": [
                    {
                        "modelo_id": a.get("modeloId"),
                        "nombre": a.get("nombre"),
                        "precio_por_hora": _pesos(a.get("tarifaHoraCents")),
                    }
                    for a in exc.alternativas
                ],
            }
        return {
            "ok": False,
            "error": "recien_tomada",
            "detalle": (
                "otro cliente tomó esa máquina y no hay nada equivalente libre "
                "en esas fechas. Decíselo con honestidad y ofrecé otras fechas "
                "con consultar_disponibilidad, o hacé handoff"
            ),
        }

    async def _crear_reserva(self, args: dict[str, Any]) -> dict[str, Any]:
        chosen, error = await self._resolve_offer(args)
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "oferta_desconocida"}
        try:
            result = await self._ctx.crm.create_rental(
                self._crm_conv_id,
                chosen.offer_id,
                localidad_obra=str(args.get("localidad_obra") or "") or None,
            )
        except RecentlyTaken as exc:
            return await self._recovery(exc)
        except InventoryUnavailable:
            return self._sin_inventario()
        except CrmConflict as exc:
            if exc.code == "oferta_vencida":
                return {
                    "ok": False,
                    "error": "oferta_vencida",
                    "detalle": (
                        "esa oferta venció (son por tiempo limitado). Volvé a "
                        "llamar consultar_disponibilidad con las mismas fechas y "
                        "reservá con el oferta_id nuevo"
                    ),
                }
            if exc.code == "oferta_desconocida":
                offered = await self._ctx.store.get_rental_offers(self._conv.id)
                return {
                    "ok": False,
                    "error": "oferta_desconocida",
                    "detalle": (
                        "el negocio no reconoce esa oferta; volvé a consultar "
                        "disponibilidad antes de reservar"
                    ),
                    "ofertas_vigentes": _offers_for_llm(offered),
                }
            if exc.code == "ya_tiene_reserva":
                # 017 Fase 7 (bis) — El CRM ya no deja acumular dos máquinas
                # tomadas por un mismo lead. Antes esto llegaba disfrazado de
                # `recien_tomada` ("otra reserva ganó esa unidad"), que es
                # falso cuando el que la tiene es el propio lead, y mandaba al
                # agente a ofrecer alternativas que no hacían falta.
                previa = (exc.payload or {}).get("reservaExistente") or {}
                return {
                    "ok": False,
                    "error": "ya_tiene_reserva",
                    "reserva_actual": previa,
                    "detalle": (
                        "este lead YA tiene una máquina tomada en esta "
                        "conversación (ver reserva_actual). No se le toma una "
                        "segunda: si cambió de fechas o de máquina, movés la "
                        "que ya tiene con cambiar_reserva_tentativa usando el "
                        "oferta_id nuevo. Si de verdad quiere DOS máquinas a la "
                        "vez, eso lo coordina una persona: hacé handoff."
                    ),
                }
            if exc.code == "ai_paused":
                # Un humano tomó la conversación entre el contexto y la reserva.
                return {
                    "ok": False,
                    "error": "ai_paused",
                    "detalle": "un asesor tomó esta conversación; no sigas, ya está en manos de una persona",
                }
            raise

        await self._ctx.store.clear_rental_offers(self._conv.id)
        self.booked = True
        reserva = result.get("reserva") or {}
        try:
            await self._ctx.crm.put_ficha(
                self._crm_conv_id,
                {
                    "calificado": True,
                    "resultado": "reservo",
                    "maquina_interes": chosen.label,
                },
            )
        except CrmError as exc:  # best-effort: la reserva ya existe
            logger.warning("tools: no pude actualizar ficha tras reservar: %s", exc)
        return {
            "ok": True,
            "etiqueta": chosen.label,
            "desde": reserva.get("desde") or chosen.desde,
            "hasta": reserva.get("hasta") or chosen.hasta,
            "horas_por_dia": reserva.get("horasPorDia"),
            "precio_total_sin_iva": _pesos(
                reserva.get("montoCotizadoCents") or chosen.amount_cents
            ),
            "estado": reserva.get("estado") or "tentativa",
            "instrucciones": (
                "quedó TOMADA, no confirmada. Decile exactamente eso: que se la "
                "dejás tomada con la máquina, las fechas y el precio, y que un "
                "asesor lo confirma a la brevedad. NUNCA digas 'confirmada', "
                "'cerrada' ni 'reservada en firme', y no le prometas hora ni "
                "lugar de entrega: el traslado lo coordina el asesor.\n"
                "Si más adelante quiere CORRER LAS FECHAS o cambiar de máquina, "
                "no le tomes otra: consultá disponibilidad para lo nuevo y mové "
                "esta con cambiar_reserva_tentativa. CANCELAR sí es de una "
                "persona: ahí hacé handoff."
            ),
        }

    async def _cambiar_reserva(self, args: dict[str, Any]) -> dict[str, Any]:
        chosen, error = await self._resolve_offer(args)
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "oferta_desconocida"}
        try:
            result = await self._ctx.crm.move_rental(self._crm_conv_id, chosen.offer_id)
        except RecentlyTaken as exc:
            return await self._recovery(exc)
        except InventoryUnavailable:
            return self._sin_inventario()
        except CrmConflict as exc:
            if exc.code in ("sin_reserva", "reserva_inexistente"):
                return {
                    "ok": False,
                    "error": "sin_reserva",
                    "detalle": (
                        "no hay ninguna reserva tomada que mover en esta "
                        "conversación; usá crear_reserva_tentativa"
                    ),
                }
            if exc.code == "oferta_vencida":
                return {
                    "ok": False,
                    "error": "oferta_vencida",
                    "detalle": (
                        "esa oferta venció; volvé a llamar consultar_disponibilidad "
                        "y movela con el oferta_id nuevo"
                    ),
                }
            if exc.code == "ai_paused":
                return {
                    "ok": False,
                    "error": "ai_paused",
                    "detalle": "un asesor tomó esta conversación; no sigas",
                }
            raise

        await self._ctx.store.clear_rental_offers(self._conv.id)
        self.booked = True
        reserva = result.get("reserva") or {}
        return {
            "ok": True,
            "etiqueta": chosen.label,
            "desde": reserva.get("desde") or chosen.desde,
            "hasta": reserva.get("hasta") or chosen.hasta,
            "horas_por_dia": reserva.get("horasPorDia"),
            "precio_total_sin_iva": _pesos(
                reserva.get("montoCotizadoCents") or chosen.amount_cents
            ),
            "estado": reserva.get("estado") or "tentativa",
            "instrucciones": (
                "quedó movida y sigue TOMADA, no confirmada. Confirmale la "
                "máquina, las fechas nuevas y el precio, y repetí que un asesor "
                "lo confirma. Nunca digas 'confirmada'."
            ),
        }

    async def _route_out(self) -> dict[str, Any]:
        await self._ctx.crm.put_ficha(
            self._crm_conv_id, {"calificado": False, "resultado": "descartado"}
        )
        self.routed_out = True
        out: dict[str, Any] = {"ok": True}
        if self._profile.resources:
            out["recursos"] = self._profile.resources
            out["instrucciones"] = "compartí estos recursos al despedirte, puerta abierta"
        return out

    def _handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        self.handoff_reason = str(args.get("reason") or "lead_request")
        return {
            "ok": True,
            "nota": (
                "el pase a humano se ejecutará después de tu mensaje de despedida"
            ),
        }
