"""Orquestación del turno conversacional.

Gate → contexto del CRM → LLM con tools → envío vía CRM → ficha/fase/seguimiento.
Degradación silenciosa: cualquier fallo termina en silencio + log (y handoff
`error` si el LLM se agotó) — jamás texto roto al lead (Constitución IV).
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

from app import media
from app.config import canonical_identity
from app.promesa_guard import AVISO as PROMESA_AVISO, anuncia_una_busqueda
from app.stock_guard import (
    PREGUNTA_SEGURA as STOCK_PREGUNTA_SEGURA,
    alerta_stock,
    stock_inventado,
)
from app.acceso import (
    alerta_acceso,
    alto_del_lead,
    entidades_de_acceso,
    hablo_de_un_acceso,
    paso_del_lead,
    pregunta_segura_acceso,
    promesas_de_acceso,
)
from app.booking_guard import (
    afirma_reserva,
    alerta_reserva_falsa,
    confirm_booking,
    pregunta_segura,
)
from app.catalog_guard import (
    IMPLEMENTO_A_UN_ASESOR,
    PREGUNTA_SEGURA as CATALOGO_PREGUNTA_SEGURA,
    alerta_implemento_ajeno,
    alerta_maquinas_ajenas,
    aviso_niega_sin_mirar,
    aviso_sin_mirar_el_catalogo,
    fichas_del_crm,
    implementos_mal_colgados,
    maquinas_nombradas,
    menciones_ajenas,
    modelos_del_crm,
    niega_un_servicio,
    permitido_del_crm,
)
from app.format import to_whatsapp
from app.greeting import strip_restart
from app.crm import CrmConflict, CrmError, canonical_handoff_reason
from app.escalation import alert_for, needs_human
from app.hostility import ALERT as HOSTILITY_ALERT, hostile_streak
from app.llm import LlmExhausted
from app.stall import ALERTA as STALL_ALERT, es_relleno, racha_vacia, sin_rumbo
from app.profile import resolve_profile
from app.prompt import build_system_prompt
from app.state import AppContext, InboundMessage, utcnow
from app.tools import ToolRuntime, _pesos, tool_schemas

logger = logging.getLogger("nea.turn")

# 017 — Subido de 5 a 7: el flujo de alquiler encadena más herramientas que el
# de agendamiento (catálogo → disponibilidad → cotizar → reservar), y con 5 un
# solo tropiezo del modelo (mandar un oferta_id inventado, que el guardrail
# rechaza bien) consumía el turno entero y el lead se quedaba sin la reserva.
MAX_TOOL_ROUNDS = 7
# Cuánto calla el agente tras cerrar por falta de rumbo. Un lead que vuelve al
# día siguiente merece respuesta; el que insiste en el mismo hilo muerto, no.
STALL_COOLDOWN = timedelta(hours=24)
# Mensajes que se traen para contar el hilo del lead (el LLM ve menos).
STALL_LOOKBACK = 40
CONTEXT_ATTEMPTS = 3  # el relay puede tardar un instante en aterrizar en el CRM

# Comando de pruebas: reinicia la memoria de ESA conversación. Disponible SOLO
# para identidades de TESTER_WA_IDS (vacía = comando apagado).
RESET_COMMANDS = frozenset({"/reset", "#reset"})


@dataclass
class TurnResult:
    """Qué pasó en el turno. En producción nadie lo mira (handle_flush lo
    descarta); lo consume el Laboratorio (Fase 7), que necesita distinguir
    "el agente eligió callarse" de "el turno se rompió".

    `silencio` nombra el motivo cuando no hubo respuesta: sin él, un turno mudo
    por ventana cerrada y uno mudo por excepción se ven idénticos en el reporte
    y el juez termina castigando al agente por hacer lo correcto.
    """

    reply: str | None = None
    sent: bool = False
    handoff: str | None = None
    silencio: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)


def _agent_tz(settings: Any) -> ZoneInfo:
    try:
        return ZoneInfo(getattr(settings, "agent_timezone", "") or "America/Mexico_City")
    except Exception:
        logger.warning("AGENT_TIMEZONE inválida %r — uso America/Mexico_City",
                       getattr(settings, "agent_timezone", None))
        return ZoneInfo("America/Mexico_City")


@asynccontextmanager
async def conversation_lock(ctx: AppContext, identity: str) -> AsyncIterator[None]:
    """Serializa los turnos de UNA conversación.

    El coalescer agrupa ráfagas por debounce, pero nada le impide disparar un
    turno nuevo mientras el anterior sigue corriendo: el mensaje que llega
    tarde abre su propio turno con el contexto de ANTES de que el turno vivo
    actuara. Así se reserva una cita sin haber leído el mensaje que la
    corregía, y salen dos respuestas pisándose.

    Con el candado, el turno tardío espera, y al arrancar re-lee el contexto
    del CRM y el historial — que ya incluyen lo que hizo el turno anterior.
    """
    lock = ctx.turn_locks.get(identity)
    if lock is None:
        lock = ctx.turn_locks[identity] = asyncio.Lock()
    # El conteo sube ANTES del await: quien ya tiene el objeto en mano queda
    # contado, así que el candado nunca se recicla debajo de un turno que
    # espera (y el diccionario no crece sin fin con cada lead histórico).
    ctx.turn_lock_users[identity] = ctx.turn_lock_users.get(identity, 0) + 1
    if lock.locked():
        logger.info(
            "turno de %s en vuelo — el mensaje nuevo espera su turno", identity
        )
    try:
        async with lock:
            yield
    finally:
        remaining = ctx.turn_lock_users.get(identity, 1) - 1
        if remaining <= 0:
            ctx.turn_lock_users.pop(identity, None)
            ctx.turn_locks.pop(identity, None)
        else:
            ctx.turn_lock_users[identity] = remaining


async def handle_flush(ctx: AppContext, identity: str, items: list[Any]) -> None:
    """Callback del coalescer — nunca propaga excepciones."""
    try:
        async with conversation_lock(ctx, identity):
            await run_turn(ctx, identity, items)
    except Exception:
        logger.exception("turno de %s reventó — silencio", identity)


async def run_turn(
    ctx: AppContext,
    identity: str,
    inbound: list[InboundMessage],
    *,
    lab_conversation_id: str | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> TurnResult:
    """Un turno completo.

    `lab_conversation_id` lo pasa SOLO el Laboratorio (Fase 7): fija contra qué
    conversación del CRM corre el turno en vez de resolverla por identidad. El
    CRM se niega a resolver conversaciones de prueba por identidad — este
    camino es explícito justamente para que no se abra solo en producción.
    """
    settings = ctx.settings
    lab = lab_conversation_id is not None

    # --- Gate 1: allowlist de pruebas (Constitución V) --------------------
    # El Laboratorio no pasa por acá: sus personas tienen teléfonos sintéticos
    # que nunca van a estar en ALLOWED_WA_IDS, y su autorización es otra — la
    # API key con la que el CRM abrió el turno.
    allowed = settings.allowed_identities
    if not lab and allowed and canonical_identity(identity) not in allowed:
        logger.info(
            "allowlist: %s fuera de ALLOWED_WA_IDS — relay sí, respuesta no", identity
        )
        return TurnResult(silencio="allowlist")

    conv = await ctx.store.get_or_create_conversation(identity)

    # --- Comando /reset (líneas de prueba) --------------------------------
    # Corre ANTES de los gates de aiEnabled/ventana: un reset también debe
    # sacar la conversación de un handoff activo.
    if canonical_identity(identity) in settings.tester_identities and any(
        (m.text or "").strip().lower() in RESET_COMMANDS for m in inbound
    ):
        await _run_reset(ctx, conv, identity)
        return TurnResult(silencio="reset")

    # --- Gate 1.5: conversación ya cerrada por no ir a ningún lado --------
    # El agente ya se despidió amable; seguir contestando es perseguir. Se
    # reabre sola tras el enfriamiento (un lead que vuelve al día siguiente
    # merece respuesta) o cuando el dueño reactiva la IA desde el CRM.
    if conv.stalled_at is not None:
        # Un mensaje CON CONTENIDO reabre en el acto: el candado es para el que
        # no dice nada, no para el que vuelve con algo. Pasó en vivo: se cerró
        # la charla y el lead escribió "la quiero el lunes a primera hora" 30
        # segundos después — y nadie le contestó (2026-09-19).
        vuelve_con_algo = any(
            (m.type or "text") != "text" or not es_relleno(m.text or "") for m in inbound
        )
        if utcnow() - conv.stalled_at < STALL_COOLDOWN and not vuelve_con_algo:
            logger.info(
                "turno %s: conversación cerrada por falta de rumbo — silencio",
                identity,
            )
            return TurnResult(silencio="sin_rumbo")
        logger.info(
            "turno %s: el lead volvió %s — reabro",
            identity,
            "con algo concreto" if vuelve_con_algo else "tras el enfriamiento",
        )
        await ctx.store.update_conversation(conv.id, stalled_at=None)
        conv.stalled_at = None

    # --- Gate 2: contexto del CRM (aiEnabled, ventana) --------------------
    context = await _fetch_context(ctx, identity, lab_conversation_id)
    if context is None:
        logger.warning("turno %s: sin contexto del CRM — silencio", identity)
        return TurnResult(silencio="sin_contexto")
    conversation_info = context.get("conversation") or {}
    crm_conv_id = conversation_info.get("id")
    if not crm_conv_id:
        logger.warning("turno %s: contexto sin conversationId — silencio", identity)
        return TurnResult(silencio="sin_contexto")
    if not conversation_info.get("aiEnabled", False):
        logger.info("turno %s: aiEnabled=false (handoff activo) — silencio", identity)
        return TurnResult(silencio="ia_pausada")
    if not conversation_info.get("windowOpen", False):
        logger.info("turno %s: ventana de 24 h cerrada — silencio", identity)
        return TurnResult(silencio="ventana_cerrada")

    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id=str(crm_conv_id),
        last_inbound_at=utcnow(),
        followup_due_at=None,  # el lead habló: se re-agenda al final del turno
    )

    # Señal de vida: leído + "escribiendo…" mientras Nea piensa (007).
    # Best-effort absoluto: un fallo aquí jamás afecta el turno.
    try:
        await ctx.crm.post_typing(str(crm_conv_id))
    except Exception as exc:
        logger.debug("typing de %s falló (%s) — sigo", identity, exc)

    # --- Contenido del turno: texto + multimedia procesada (spec 002) -----
    parts: list[str] = []
    image_uris: list[str] = []
    for m in inbound:
        if m.text:
            parts.append(m.text)
            continue
        if m.type in ("text", "button", "interactive"):
            continue  # texto vacío raro: nada que procesar
        part = await media.describe_item(ctx, m)
        if part.text:
            parts.append(part.text)
        if part.image_data_uri:
            image_uris.append(part.image_data_uri)
    if not parts and not image_uris:
        logger.info("turno %s: nada procesable en la ráfaga — silencio", identity)
        return TurnResult(silencio="nada_procesable")

    user_text = "\n".join(parts)
    await ctx.store.add_message(
        conv.id, "user", user_text, wa_message_id=inbound[0].wa_message_id
    )

    # --- Armar mensajes para el LLM ---------------------------------------
    referral = next((m.referral_headline for m in inbound if m.referral_headline), None)
    offered = await ctx.store.get_rental_offers(conv.id)
    profile = await resolve_profile(ctx)
    system = build_system_prompt(
        profile=profile,
        context=context,
        conv=conv,
        referral_headline=referral,
        offered=offered,
        inventory=ctx.inventory_enabled,
        tz=_agent_tz(settings),
    )
    # Se traen más mensajes de los que ve el LLM: el candado de cierre cuenta
    # el hilo COMPLETO del lead, no solo la ventana de contexto.
    recientes = await ctx.store.recent_messages(conv.id, STALL_LOOKBACK)
    history = recientes[-settings.history_window :]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}] + [
        {"role": m.role, "content": m.content} for m in history
    ]
    # Hostilidad sostenida (AC-18): el CONTEO es determinista — el LLM salió
    # flaky contando entre turnos. Al tercer strike: alerta en el turno y
    # handoff garantizado más abajo aunque el modelo no llame la herramienta.
    streak = hostile_streak([m.content for m in history if m.role == "user"])
    if streak >= 3:
        messages.append({"role": "system", "content": HOSTILITY_ALERT})
    # 017 Fase 7 (bis) — Pedidos que NO decide el agente (descuento,
    # facturación, seguro, reclamo por un alquiler anterior). El chasis ya se
    # lo pide; el Laboratorio mostró que escribe "eso lo ve un asesor" y sigue
    # vendiendo sin llamar la herramienta, así que el dueño nunca se entera y
    # el lead espera una respuesta que no llega. Misma receta que arriba:
    # alerta en el turno + handoff garantizado más abajo.
    escalar = needs_human(user_text)
    if escalar is not None:
        logger.info("turno %s: el lead pide algo que decide una persona (%s)",
                    identity, escalar)
        messages.append({"role": "system", "content": alert_for(escalar)})
    # Candado de cierre: conversación que no va a ningún lado. Se despide con
    # UNA línea cálida en este turno y después calla (gate 1.5). El conteo es
    # determinista aquí; el LLM solo pone la redacción.
    del_lead = [m.content for m in recientes if m.role == "user"]
    cerrar_sin_rumbo = streak < 3 and sin_rumbo(del_lead, conv.phase)
    if cerrar_sin_rumbo:
        logger.info(
            "turno %s: sin rumbo (%d mensajes del lead, racha vacía %d) — cierro",
            identity,
            len(del_lead),
            racha_vacia(del_lead),
        )
        messages.append({"role": "system", "content": STALL_ALERT})
    if image_uris:
        # El último user message de este turno se vuelve multimodal: el
        # historial persiste solo el texto; las imágenes viven en ESTE turno.
        last = messages[-1]
        last["content"] = [{"type": "text", "text": str(last["content"])}] + [
            {"type": "image_url", "image_url": {"url": uri}} for uri in image_uris
        ]

    # --- LLM con tools ----------------------------------------------------
    runtime = ToolRuntime(
        ctx,
        conv,
        str(crm_conv_id),
        profile=profile,
        trace=trace,
        reserva_activa=context.get("reservaActiva"),
        # Si el lead dijo por dónde tiene que pasar la máquina, el veredicto
        # de acceso lo calcula el código (app/acceso.py), no el modelo.
        ancho_paso_m=paso_del_lead(del_lead),
        alto_paso_m=alto_del_lead(del_lead),
        hablo_de_acceso=hablo_de_un_acceso(del_lead),
    )
    try:
        final_text = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error(
            "turno %s: LLM agotó reintentos (%s) — silencio + handoff error",
            identity,
            exc,
        )
        await _safe_handoff(ctx, str(crm_conv_id), "error")
        await ctx.store.update_conversation(
            conv.id, phase="cerrada", followup_due_at=None
        )
        return TurnResult(handoff="error", silencio="llm_agotado", tools=trace or [])

    # Un "te busco las opciones" sin buscar deja al lead esperando: el
    # agente no escribe primero (app/promesa_guard.py). Va antes que las
    # demás guardas para que miren la respuesta que de verdad sale.
    final_text = await _sin_promesa_de_busqueda(ctx, identity, messages, runtime, final_text)
    # …y lo que escribió ANTES de ver el catálogo no sale sin que lo lea.
    final_text = await _sin_respuesta_a_ciegas(ctx, identity, messages, runtime, final_text)

    # Una reserva que el modelo da por hecha sin que nada se haya reservado no
    # sale: se le avisa, tiene una vuelta para reservar o corregirse, y si
    # insiste sale una pregunta de confirmación (app/booking_guard.py).
    final_text = await _sin_reserva_falsa(
        ctx, conv.id, identity, messages, runtime, final_text
    )

    # …y que no nombre una máquina que el negocio no tiene
    # (app/catalog_guard.py).
    final_text = await _sin_maquinas_ajenas(ctx, identity, messages, runtime, final_text)

    # Antes que nada: si nombró una máquina sin haber mirado el catálogo, la
    # ficha real se le pone delante y reescribe (app/catalog_guard.py).
    final_text = await _con_la_ficha_a_la_vista(ctx, identity, messages, runtime, final_text)
    # …ni negar un servicio de memoria ("no hacemos fletes"): el catálogo
    # puede decir lo contrario.
    final_text = await _sin_negar_de_memoria(ctx, identity, messages, runtime, final_text)

    # …ni que le cuelgue el martillo de la minicargadora a una excavadora
    # (app/catalog_guard.py).
    final_text = await _sin_implemento_ajeno(ctx, identity, messages, runtime, final_text)

    # …ni que le invente una falta de stock a una máquina que está libre
    # (app/stock_guard.py).
    final_text = await _sin_falta_de_stock(ctx, identity, messages, runtime, final_text)

    # …ni que prometa que entra por el paso lo que el sistema calculó que no
    # (app/acceso.py).
    final_text = await _sin_acceso_falso(ctx, identity, messages, runtime, final_text)

    # Backstop determinista: al tercer strike el handoff SUCEDE, lo haya
    # llamado el modelo o no (la regla de negocio no depende de su humor).
    if streak >= 3 and runtime.handoff_reason is None:
        runtime.handoff_reason = "hostilidad"
    # Ídem para lo que el agente no decide, y SIEMPRE con el mismo motivo lo
    # haya llamado el modelo o no: si no, la misma situación le aparece al
    # dueño etiquetada `cliente` unas veces y `modelo` otras, según el humor
    # del turno. `cliente` porque lo que pasó es que el LEAD pidió algo que
    # necesita una persona — no que la IA dudó.
    # La hostilidad gana: de las dos señales es la que el dueño necesita ver
    # primero.
    if escalar is not None and canonical_handoff_reason(runtime.handoff_reason) != "hostilidad":
        if runtime.handoff_reason is None:
            logger.info("turno %s: handoff por %s (backstop, el modelo no lo llamó)",
                        identity, escalar)
        runtime.handoff_reason = "cliente"

    # --- Enviar la respuesta (SIEMPRE vía el CRM, nunca Meta directo) -----
    # Fase 7 — Última parada antes del lead: el Markdown que el modelo escribe
    # igual pese al prompt se traduce acá (app/format.py).
    reply_text = to_whatsapp(final_text.strip()) if final_text else ""
    # …y el reinicio de conversación que el modelo pega atrás de una despedida
    # cuando un mensaje de sistema le reencuadra el turno (app/greeting.py).
    # `conv.greeted` es el estado ANTES de este turno: el saludo del primer
    # contacto no se toca.
    if reply_text:
        reply_text = strip_restart(reply_text, profile.agent_name, conv.greeted)
    # …y si en este turno se tomó o se movió una máquina, que la confirmación
    # nombre ESA máquina, escriba lo que escriba el modelo (app/booking_guard.py).
    if runtime.booking is not None:
        reply_text = confirm_booking(reply_text, runtime.booking)
    sent = False
    if reply_text:
        sent = await _send(ctx, conv.id, str(crm_conv_id), reply_text)
        if sent:
            await ctx.store.add_message(conv.id, "assistant", reply_text)

    # El handoff se ejecuta DESPUÉS de la despedida (si no, el CRM la rechaza
    # con 409 ai_paused).
    if runtime.handoff_reason is not None:
        await _safe_handoff(ctx, str(crm_conv_id), runtime.handoff_reason)

    # --- Fase + seguimiento -----------------------------------------------
    updates: dict[str, Any] = {"greeted": True}
    if cerrar_sin_rumbo:
        # Se marca aunque el envío haya fallado: la decisión de cerrar ya se
        # tomó y no queremos que el próximo mensaje reabra el ciclo.
        updates["stalled_at"] = utcnow()
        updates["phase"] = "cerrada"
        updates["followup_due_at"] = None
    elif runtime.handoff_reason is not None or runtime.booked or runtime.routed_out:
        updates["phase"] = "cerrada"
        updates["followup_due_at"] = None
    else:
        if runtime.proposed:
            updates["phase"] = "agendando"
        # FOLLOWUP_HOURS <= 0 = el agente NUNCA escribe primero (decisión del
        # dueño, 2026-09-17: un "¿seguís ahí?" 4 h después se lee como
        # insistencia). Con 0 ni siquiera se agenda: el que decide cuándo
        # sigue la charla es el lead.
        if sent and not conv.followup_sent and settings.followup_hours > 0:
            updates["followup_due_at"] = utcnow() + timedelta(
                hours=settings.followup_hours
            )
    await ctx.store.update_conversation(conv.id, **updates)
    reply = reply_text or None
    return TurnResult(
        reply=reply,
        sent=sent,
        handoff=runtime.handoff_reason,
        silencio=None if reply else "sin_texto",
        tools=trace or [],
    )


def _reserva_falsa(texto: str | None, runtime: ToolRuntime) -> str | None:
    """Qué afirma la respuesta que NO pasó ("reserva" | "movida"), o None."""
    afirmacion = afirma_reserva(texto)
    if afirmacion is None or runtime.booking is not None:
        return None
    # "Ya la tenés tomada" es verdad si la tomó en un turno anterior. "Te la
    # moví" no: un cambio que no ocurrió en ESTE turno no ocurrió.
    if afirmacion == "reserva" and runtime.tiene_reserva:
        return None
    return afirmacion


async def _sin_reserva_falsa(
    ctx: AppContext,
    conv_id: int,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """Pasó en vivo con Qwen: "Listo, te la dejé tomada" sin haber llamado
    crear_reserva_tentativa. El lead cree tener la máquina y en el calendario
    no hay nada. El prompt ya lo prohíbe; esto no depende de que lo cumpla."""
    afirmacion = _reserva_falsa(final_text, runtime)
    if afirmacion is None:
        return final_text
    logger.warning(
        "turno %s: la respuesta afirma una %s que no ocurrió — no sale; le "
        "aviso al modelo y le doy una vuelta más",
        identity,
        afirmacion,
    )
    messages.append(
        {"role": "system", "content": alerta_reserva_falsa(afirmacion, final_text or "")}
    )
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        segundo = None
    if runtime.booking is not None:
        # Ahora sí reservó: confirm_booking se asegura de que nombre la máquina.
        return segundo
    if segundo and segundo.strip() and _reserva_falsa(segundo, runtime) is None:
        return segundo
    logger.warning(
        "turno %s: el modelo volvió a afirmar la %s (o no contestó) — sale la "
        "pregunta de confirmación armada en código",
        identity,
        afirmacion,
    )
    ofertas = await ctx.store.get_rental_offers(conv_id)
    return pregunta_segura(
        afirmacion, [(o.label, _pesos(o.amount_cents)) for o in ofertas]
    )


async def _sin_maquinas_ajenas(
    ctx: AppContext,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """Pasó en vivo: recomendó "una minicargadora Bobcat" y una "New Holland
    RG140". La flota no las tiene y el lead termina pidiéndole al asesor una
    máquina que no existe."""
    if not ctx.inventory_enabled or not final_text:
        return final_text
    permitido = await permitido_del_crm(ctx)
    ajenas = menciones_ajenas(final_text, permitido)
    if not ajenas:
        return final_text
    logger.warning(
        "turno %s: la respuesta nombra lo que el negocio no tiene (%s) — no "
        "sale; le aviso al modelo y le doy una vuelta más",
        identity,
        ", ".join(ajenas),
    )
    messages.append(
        {"role": "system", "content": alerta_maquinas_ajenas(ajenas, final_text)}
    )
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        segundo = None
    if segundo and segundo.strip() and not menciones_ajenas(segundo, permitido):
        return segundo
    logger.warning(
        "turno %s: el modelo volvió a nombrar máquinas ajenas (o no contestó) "
        "— sale la pregunta armada en código",
        identity,
    )
    return CATALOGO_PREGUNTA_SEGURA


async def _con_la_ficha_a_la_vista(
    ctx: AppContext,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """Nombrar una máquina exige haber mirado el catálogo EN ESTE TURNO.

    Pasó con un lead real (2026-09-19): "lo ideal es una miniexcavadora" y
    después "la Miniexcavadora 8018 CTS mide 0,96 m de ancho", sin una sola
    llamada a buscar_maquinas. Validar el nombre no alcanza: los NÚMEROS
    también salen de la memoria. Si el turno no tocó el catálogo, la ficha
    real entra al turno y el modelo reescribe con eso.
    """
    if not ctx.inventory_enabled or not final_text or runtime.miro_catalogo:
        return final_text
    fichas = await fichas_del_crm(ctx)
    nombradas = maquinas_nombradas(final_text, fichas)
    if not nombradas:
        return final_text
    logger.info(
        "turno %s: nombró %s sin mirar el catálogo — le paso la ficha y que "
        "reescriba",
        identity,
        ", ".join(str(f.get("nombre")) for f in nombradas),
    )
    messages.append({"role": "system", "content": aviso_sin_mirar_el_catalogo(nombradas)})
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        return final_text
    # Si la vuelta con la ficha delante no dio texto, vale el borrador: las
    # otras guardas siguen mirándolo.
    return segundo if segundo and segundo.strip() else final_text


async def _sin_promesa_de_busqueda(
    ctx: AppContext,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """Pasó en vivo (2026-09-26): "te busco las mejores opciones para el
    trabajo" y el turno terminó sin buscar."""
    if not ctx.inventory_enabled or runtime.miro_catalogo or not anuncia_una_busqueda(final_text):
        return final_text
    logger.info("turno %s: anuncia una búsqueda que no hizo — que busque", identity)
    messages.append({"role": "system", "content": PROMESA_AVISO})
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        return final_text
    return segundo if segundo and segundo.strip() else final_text


async def _sin_respuesta_a_ciegas(
    ctx: AppContext,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """El texto escrito junto a buscar_maquinas se escribió sin ver el
    resultado. Si el modelo después no contestó, ese borrador es memoria pura:
    se le cita y tiene una vuelta para reescribirlo con lo que devolvió el
    catálogo."""
    if not runtime.a_ciegas or not final_text:
        return final_text
    runtime.a_ciegas = False
    logger.info("turno %s: la respuesta se escribió antes de ver el catálogo — que la relea", identity)
    messages.append({"role": "system", "content": AVISO_A_CIEGAS.format(borrador=final_text)})
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        return final_text
    return segundo if segundo and segundo.strip() else final_text


AVISO_A_CIEGAS = (
    "AVISO DEL SISTEMA — esta respuesta la escribiste ANTES de ver lo que "
    "devolvieron las herramientas, y todavía no salió: «{borrador}». Leé los "
    "resultados de arriba y escribí la respuesta con ESOS datos: nombre exacto, "
    "specs tal cual y `cuando_elegirla`. Si para este trabajo conviene otra "
    "máquina, buscala antes de nombrarla. Escribí la respuesta completa para "
    "el lead."
)


async def _sin_negar_de_memoria(
    ctx: AppContext,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """Decir que el negocio NO hace algo exige haber mirado el catálogo.

    Pasó en vivo (2026-09-26): "necesito llevar una excavadora mía de Perico a
    Humahuaca" → "No hacemos fletes de máquinas de terceros", sin una sola
    llamada. Los tractores de RPM salen con carretón para transportar
    maquinaria. Si el turno no tocó el catálogo, el catálogo entra al turno y
    el modelo revisa antes de negar."""
    if not ctx.inventory_enabled or runtime.miro_catalogo or not niega_un_servicio(final_text):
        return final_text
    # Los textos armados por las otras guardas no son del modelo: "así no te
    # paso una máquina que no tenemos" no niega ningún servicio.
    if final_text in (CATALOGO_PREGUNTA_SEGURA, IMPLEMENTO_A_UN_ASESOR):
        return final_text
    fichas = await fichas_del_crm(ctx)
    if not fichas:
        return final_text
    logger.info("turno %s: niega un servicio sin mirar el catálogo — que revise", identity)
    messages.append({"role": "system", "content": aviso_niega_sin_mirar(fichas)})
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        return final_text
    return segundo if segundo and segundo.strip() else final_text


async def _sin_implemento_ajeno(
    ctx: AppContext,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """Pasó en vivo: "para demoler te sirve una excavadora con martillo
    hidráulico". Los martillos son de la minicargadora."""
    if not ctx.inventory_enabled or not final_text:
        return final_text
    # El catálogo COMPLETO, no lo que se buscó en este turno: si el modelo
    # buscó "grúa para demoler", con esas fichas nadie sabe de quién es el
    # martillo.
    modelos = await modelos_del_crm(ctx)
    problemas = implementos_mal_colgados(final_text, modelos)
    if not problemas:
        return final_text
    logger.warning(
        "turno %s: la respuesta le cuelga un implemento ajeno a una máquina "
        "(%s) — no sale; le aviso al modelo",
        identity,
        " | ".join(problemas),
    )
    messages.append(
        {"role": "system", "content": alerta_implemento_ajeno(problemas, final_text)}
    )
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        segundo = None
    if segundo and segundo.strip() and not implementos_mal_colgados(segundo, modelos):
        return segundo
    logger.warning(
        "turno %s: el modelo insistió con el implemento ajeno (o no contestó) "
        "— lo toma una persona",
        identity,
    )
    # Si el modelo no encuentra cómo hacer el trabajo con lo que hay, la obra
    # supera a la flota: lo ve un asesor (decisión del dueño).
    if runtime.handoff_reason is None:
        runtime.handoff_reason = "modelo"
    return IMPLEMENTO_A_UN_ASESOR


async def _sin_falta_de_stock(
    ctx: AppContext,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """Pasó con un lead real: consultó una máquina, el sistema le contestó con
    OTRA libre, y le dijo al lead que la primera no estaba disponible. Estaban
    todas libres."""
    if not final_text or not runtime.disponibilidad:
        return final_text
    problemas = stock_inventado(final_text, runtime.disponibilidad)
    if not problemas:
        return final_text
    logger.warning(
        "turno %s: la respuesta inventa una falta de stock (%s) — no sale; le "
        "aviso al modelo",
        identity,
        " | ".join(problemas),
    )
    messages.append({"role": "system", "content": alerta_stock(problemas, final_text)})
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        segundo = None
    if segundo and segundo.strip() and not stock_inventado(segundo, runtime.disponibilidad):
        return segundo
    logger.warning(
        "turno %s: el modelo insistió con la falta de stock (o no contestó) — "
        "sale la disculpa armada en código",
        identity,
    )
    return STOCK_PREGUNTA_SEGURA


async def _modelos_para_acceso(ctx: AppContext, runtime: ToolRuntime) -> dict[str, dict[str, Any]]:
    """Las fichas que vio el modelo en este turno; si no buscó (contesta con lo
    que vio antes), el catálogo completo."""
    if runtime.modelos_vistos:
        return dict(runtime.modelos_vistos)
    try:
        data = await ctx.crm.get_catalogo(None)
    except Exception as exc:  # sin catálogo no hay fuente de verdad: no opino
        logger.warning("guarda de acceso: catálogo inaccesible (%s) — no opino", exc)
        return {}
    return {str(m.get("nombre") or ""): m.get("specs") or {} for m in data.get("modelos") or []}


async def _sin_acceso_falso(
    ctx: AppContext,
    identity: str,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    final_text: str | None,
) -> str | None:
    """Pasó en vivo con el veredicto en la mano: "entra por tu portón de
    1,85 m, aunque va justita", con el cucharón de 1,90 m puesto. La máquina
    se queda en la puerta de la obra."""
    paso = runtime.paso
    if not ctx.inventory_enabled or not final_text or not paso:
        return final_text
    entidades = entidades_de_acceso(await _modelos_para_acceso(ctx, runtime), paso)
    problemas = promesas_de_acceso(final_text, entidades)
    if not problemas:
        return final_text
    logger.warning(
        "turno %s: la respuesta promete que entra por %s lo que el sistema "
        "calculó que no (%s) — no sale; le aviso al modelo",
        identity,
        paso.describir(),
        " | ".join(problemas),
    )
    messages.append({"role": "system", "content": alerta_acceso(problemas, final_text, paso)})
    try:
        segundo = await _tool_loop(ctx, messages, runtime)
    except LlmExhausted as exc:
        logger.error("turno %s: la segunda vuelta no respondió (%s)", identity, exc)
        segundo = None
    if segundo and segundo.strip():
        entidades = entidades_de_acceso(await _modelos_para_acceso(ctx, runtime), paso)
        if not promesas_de_acceso(segundo, entidades):
            return segundo
    logger.warning(
        "turno %s: el modelo insistió con que entra (o no contestó) — sale la "
        "pregunta armada en código",
        identity,
    )
    return pregunta_segura_acceso(paso)


async def _run_reset(ctx: AppContext, conv: Any, identity: str) -> None:
    """Reinicio de pruebas: CRM primero (ficha limpia + IA reactivada, para que
    la confirmación no rebote con 409 ai_paused) y luego la memoria local."""
    crm_conv_id = conv.crm_conversation_id
    if not crm_conv_id:
        context = await _fetch_context(ctx, identity)
        crm_conv_id = ((context or {}).get("conversation") or {}).get("id")
    if crm_conv_id:
        try:
            await ctx.crm.post_reset(str(crm_conv_id))
        except CrmError as exc:
            logger.warning("reset %s: el CRM no pudo reiniciar (%s) — sigo", identity, exc)
    await ctx.store.reset_conversation(conv.id)
    logger.info("reset de pruebas ejecutado para %s", identity)
    if crm_conv_id:
        await _send(
            ctx,
            conv.id,
            str(crm_conv_id),
            "🧹 Listo: memoria reiniciada. Te trato como lead nuevo desde tu "
            "próximo mensaje. (Comando de pruebas, solo líneas autorizadas.)",
        )


async def _fetch_context(
    ctx: AppContext, identity: str, lab_conversation_id: str | None = None
) -> dict[str, Any] | None:
    for attempt in range(CONTEXT_ATTEMPTS):
        try:
            context = (
                await ctx.crm.get_context_by_conversation(lab_conversation_id)
                if lab_conversation_id is not None
                else await ctx.crm.get_context(identity)
            )
        except CrmError as exc:
            logger.warning(
                "context de %s: error del CRM (intento %d): %s",
                identity,
                attempt + 1,
                exc,
            )
            context = None
        if context is not None:
            return context
        if attempt < CONTEXT_ATTEMPTS - 1:
            await asyncio.sleep(1.0)  # chance a que el relay aterrice en el CRM
    return None


#: Herramientas cuyo resultado no le da al modelo nada nuevo que decirle al
#: lead: guardan un dato (update_ficha) o agendan el pase a humano para
#: DESPUÉS de la despedida (handoff). Si el modelo escribió su mensaje en la
#: misma vuelta en que las llamó, ese mensaje es la respuesta del turno.
HERRAMIENTAS_SILENCIOSAS = frozenset({"update_ficha", "handoff"})


async def _tool_loop(
    ctx: AppContext, messages: list[dict[str, Any]], runtime: ToolRuntime
) -> str | None:
    """Rondas de tool-calling hasta obtener texto final (o rendirse).

    El texto que el modelo escribe JUNTO a una llamada a herramienta también es
    su respuesta. Antes se descartaba, y pasó en vivo (2026-09-14): a "buenas
    me gustaría alquilar una máquina" el modelo escribió el saludo y en la
    misma vuelta guardó la ficha. El saludo nunca salió; la vuelta siguiente
    ya no tenía nada que decir y al lead le llegó "Quedo a la espera de tu
    respuesta." Con Haiku peor: tres respuestas vacías, silencio y la charla
    pasada a un humano con motivo `error`. Qwen lo hizo 1 de cada 3 veces.
    """
    # Lo último que el modelo escribió junto a herramientas que sí informan
    # (catálogo, disponibilidad…): si después no contesta, eso es lo que dijo.
    dicho: str | None = None
    # ¿Ese texto se escribió junto a una consulta al catálogo, sin haber visto
    # el resultado? Pasó en vivo (2026-09-26): "te recomiendo la Excavadora
    # 320 DL, balde de 1,20 m" + buscar_maquinas("Excavadora 320 DL") en la
    # misma vuelta, después tres respuestas vacías, y salió el borrador con
    # specs inventadas — y como "había buscado", ninguna guarda lo frenó.
    dicho_a_ciegas = False
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            reply = await ctx.llm.complete(
                messages, tools=tool_schemas(ctx.inventory_enabled)
            )
        except LlmExhausted:
            if dicho:
                logger.warning(
                    "turno: el modelo no contestó después de las herramientas — "
                    "sale lo que ya había escrito"
                )
                runtime.a_ciegas = dicho_a_ciegas
                return dicho
            raise
        if not reply.tool_calls:
            if reply.content:
                return reply.content  # turno de puro texto
            runtime.a_ciegas = dicho_a_ciegas and dicho is not None
            return dicho
        # content vacío con tool_calls es normal (turno solo-herramientas)
        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in reply.tool_calls
                ],
            }
        )
        for tc in reply.tool_calls:
            result = await runtime.execute(tc.name, tc.arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
        if reply.content:
            if all(tc.name in HERRAMIENTAS_SILENCIOSAS for tc in reply.tool_calls):
                return reply.content
            dicho = reply.content
            dicho_a_ciegas = any(tc.name in ToolRuntime.DEL_CATALOGO for tc in reply.tool_calls)
    logger.warning("turno: demasiadas rondas de herramientas — corto")
    runtime.a_ciegas = dicho_a_ciegas and dicho is not None
    return dicho


SEND_ATTEMPTS = 4  # backoff 1 s, 2 s, 4 s entre intentos (~7 s en el turno)


async def _send(ctx: AppContext, conv_id: int, crm_conv_id: str, text: str) -> bool:
    """Envía vía el CRM. Si el turno agota sus reintentos, la respuesta NO se
    descarta: se encola en pending_send y el SenderWorker la reintenta con
    backoff hasta entregar o agotar 24 h (incidente 2026-08-03)."""
    for attempt in range(SEND_ATTEMPTS):
        try:
            await ctx.crm.send_message(crm_conv_id, text)
            return True
        except CrmConflict as exc:
            # ai_paused / window_closed: silencio respetuoso, sin reintento.
            logger.info("envío bloqueado por el CRM (%s) — silencio", exc.code)
            return False
        except CrmError as exc:
            logger.warning("envío falló (intento %d): %s", attempt + 1, exc)
            if attempt < SEND_ATTEMPTS - 1:
                await asyncio.sleep(2.0**attempt)
    pending_id = await ctx.store.enqueue_pending_send(conv_id, crm_conv_id, text)
    logger.error(
        "envío agotó reintentos del turno — encolado como pending_send %d",
        pending_id,
    )
    return False


async def _safe_handoff(ctx: AppContext, crm_conv_id: str, reason: str) -> None:
    try:
        await ctx.crm.post_handoff(crm_conv_id, reason)
        logger.info("handoff registrado en el CRM (reason=%s)", reason)
    except CrmError as exc:
        logger.error("no pude registrar el handoff (%s): %s", reason, exc)
