"""Puerta del Laboratorio: un turno del agente REAL, síncrono y sin debounce.

017 Fase 7 — Por qué existe
---------------------------
El Laboratorio de Vocero evaluaba a su agente in-process. En esta instalación
ese agente está apagado y no vende nada: quien atiende a los leads es Nea, con
tool calling contra el inventario. Evaluar al otro daba un puntaje bonito de un
agente que nadie usa.

Así que el Lab conversa con Nea. Pero el camino de producción (webhook de Meta →
relay → coalescer de 4 s → turno) es asíncrono por diseño y no sirve para un
banco de pruebas, que necesita "mandá esta línea y decime qué contestó". Este
router es esa costura, y NADA más: mismo `run_turn`, mismo prompt, mismas
herramientas, mismo CRM. Lo único que cambia es que el turno se pide de frente
y se espera.

Seguridad — tres candados independientes:

1. Autenticación: `X-API-Key` con la misma clave del bot gateway. Sin ella, 401.
2. La conversación viene por ID, no por identidad. El CRM se niega a resolver
   conversaciones de prueba por identidad, así que este camino no puede
   "descubrir" un hilo real por accidente: alguien tiene que pasarle el id.
3. La respuesta sale por `POST /api/bot/messages`, donde el CRM decide qué
   hacer con ella. Si la conversación es de prueba, la persiste y no la manda;
   si alguien apuntara esto a una conversación real, iría a WhatsApp igual que
   cualquier turno — no hay un modo "enviar de mentira" que pueda desincronizarse
   de la verdad. El sandbox lo define el CRM, dueño del dato, no este endpoint.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.state import AppContext, InboundMessage
from app.turn import conversation_lock, run_turn

logger = logging.getLogger("nea.lab")

router = APIRouter(prefix="/lab")


class LabTurnRequest(BaseModel):
    """Una línea del guion de una persona simulada."""

    crm_conversation_id: str = Field(min_length=1)
    identity: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=4096)
    #: Primera línea de la persona: borra la memoria local de Nea para esa
    #: identidad. Sin esto la corrida de hoy arrastra la de ayer y el agente
    #: "recuerda" una obra en Alta Gracia que la persona nunca mencionó.
    reset: bool = False


@router.post("/turn")
async def lab_turn(req: LabTurnRequest, request: Request) -> JSONResponse:
    ctx: AppContext | None = request.app.state.ctx
    if ctx is None:
        return JSONResponse({"error": "starting"}, status_code=503)

    expected = ctx.settings.crm_bot_api_key
    if not expected or request.headers.get("x-api-key") != expected:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if req.reset:
        conv = await ctx.store.get_or_create_conversation(req.identity)
        await ctx.store.reset_conversation(conv.id)

    trace: list[dict[str, Any]] = []
    started = time.monotonic()
    # El candado es el mismo de producción: el Lab corre secuencial, pero si
    # alguna vez no lo hiciera, dos turnos de la misma persona no se pisan.
    async with conversation_lock(ctx, req.identity):
        result = await run_turn(
            ctx,
            req.identity,
            [
                InboundMessage(
                    wa_message_id=None,
                    identity=req.identity,
                    type="text",
                    text=req.text,
                )
            ],
            lab_conversation_id=req.crm_conversation_id,
            trace=trace,
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "lab: turno de %s en %d ms — %d herramienta(s), %s",
        req.identity,
        elapsed_ms,
        len(trace),
        "respondió" if result.reply else f"silencio ({result.silencio})",
    )
    return JSONResponse(
        {
            "reply": result.reply,
            "sent": result.sent,
            "handoff": result.handoff,
            "silencio": result.silencio,
            "tools": trace,
            "elapsedMs": elapsed_ms,
        }
    )
