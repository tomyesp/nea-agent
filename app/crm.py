"""Cliente httpx del API de servicio del CRM (bot gateway de vocero-crm).

Endpoints:
  GET  /api/bot/profile                               → agent profile + KB (404 = sin perfil)
  GET  /api/bot/context?waIdentity=... | ?conversationId=...
  POST /api/bot/messages   {conversationId, text}   → 409 ai_paused|window_closed
  PUT  /api/bot/ficha      {conversationId, ficha}
  POST /api/bot/handoff    {conversationId, reason}
  GET  /api/bot/media/{mediaId}                       → binario + content-type
  POST /api/bot/reset      {conversationId}           → reinicio de pruebas (002)

017 — inventario de maquinaria (detrás de la bandera INVENTARIO del CRM):
  GET  /api/bot/catalogo?q=                           → modelos, specs y tarifas
  GET  /api/bot/disponibilidad?conversationId=&modeloId=&desde=&hasta=
                                                    → ofertas con ofertaId,
                                                      REGISTRADAS contra esa
                                                      conversación; o
                                                      alternativas si no hay
  POST /api/bot/cotizar    {modeloId, dias, conTraslado, km}  → desglose
  POST /api/bot/reservas   {conversationId, ofertaId} → tentativa; 409
                                                      recien_tomada + ofertas
  PATCH /api/bot/reservas  {conversationId, ofertaId} → mueve la tentativa
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("nea.crm")


class CrmError(Exception):
    """Fallo genérico hablando con el CRM (red, 5xx, 401...)."""


class CrmConflict(CrmError):
    """409 tipado del CRM: ai_paused | window_closed | slot_taken."""

    def __init__(self, code: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.payload = payload or {}


class InventoryUnavailable(CrmError):
    """El CRM no tiene inventario (404 en `/api/bot/catalogo|disponibilidad|...`).

    Vocero trae el motor de maquinaria detrás de una bandera de despliegue
    (`INVENTARIO`) y viene APAGADA por defecto: en una instancia así esos
    endpoints no existen. No es una caída ni un error de configuración de Nea
    — es una capacidad que ese CRM no ofrece, y el agente tiene que dejar de
    prometer máquinas en vez de reintentar contra una puerta que no está.
    """


class RecentlyTaken(CrmConflict):
    """017 — Otro lead ganó esa unidad entre la oferta y la reserva.

    Lo corta el constraint de exclusión de Postgres, así que es una carrera
    real y no un bug. Viene con salida en el mismo cuerpo: `ofertas` frescas
    de la MISMA máquina (reservables ya) y/o `alternativas` de la categoría
    (que hay que volver a consultar). Sin eso, la conversación se queda sin
    salida y el lead se va.
    """

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        super().__init__("recien_tomada", payload)
        data = payload or {}
        self.ofertas: list[dict[str, Any]] = list(data.get("ofertas") or [])
        self.alternativas: list[dict[str, Any]] = list(data.get("alternativas") or [])


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _conflict_code(response: httpx.Response) -> str:
    """Código tipado de un 409.

    El CRM anida `{"error": {"code": ...}}`; la forma plana `{"code": ...}`
    solo vivía en los mocks, así que en producción TODO 409 se leía como
    "conflict" genérico y el camino de `slot_taken` (re-ofrecer alternativas
    frescas) nunca se activaba. Se toleran las dos formas.
    """
    payload = _payload(response)
    nested = payload.get("error")
    if isinstance(nested, dict) and nested.get("code"):
        return str(nested["code"])
    return str(payload.get("code") or "conflict")


def _rental_conflict(response: httpx.Response) -> CrmConflict:
    """409 de reservas: `recien_tomada` trae la salida adjunta."""
    payload = _payload(response)
    code = _conflict_code(response)
    if code == "recien_tomada":
        return RecentlyTaken(payload)
    return CrmConflict(code, payload)


# Catálogo cerrado del CRM para handoff.reason (006). El LLM escribe motivos
# libres ("pidió humano", "duda técnica") — aquí se normalizan SIEMPRE:
# certificación 002 cazó en vivo que un reason fuera de catálogo era 422 y el
# handoff se perdía con la IA aún activa.
HANDOFF_REASONS = frozenset({"cliente", "modelo", "error", "ventana", "hostilidad"})


def canonical_handoff_reason(reason: str | None) -> str:
    r = (reason or "").strip().lower()
    if r in HANDOFF_REASONS:
        return r
    if "hostil" in r or "groser" in r or "insult" in r:
        return "hostilidad"
    if any(k in r for k in ("humano", "persona", "pidi", "lead_request", "hablar")):
        return "cliente"
    if "error" in r:
        return "error"
    return "modelo"


class CrmClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CrmError(f"error de red hacia el CRM: {exc}") from exc

    async def get_context(self, wa_identity: str) -> dict[str, Any] | None:
        """Contexto conversacional; None si el CRM aún no conoce la identidad (404)."""
        resp = await self._request(
            "GET", "/api/bot/context", params={"waIdentity": wa_identity}
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmError(f"context devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def get_context_by_conversation(self, conv_id: str) -> dict[str, Any] | None:
        """Contexto por id de conversación — camino del Laboratorio (Fase 7).

        El CRM se NIEGA a resolver una conversación de prueba por identidad
        (`?waIdentity=` filtra is_test=false a propósito: el bot de producción
        jamás debe terminar hablándole a un cliente simulado). Por id sí, que
        es explícito: solo llega aquí quien ya tiene el id en la mano porque
        el Laboratorio se lo pasó.
        """
        resp = await self._request(
            "GET", "/api/bot/context", params={"conversationId": conv_id}
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmError(f"context devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def get_profile(self) -> dict[str, Any] | None:
        """Agent profile + knowledge base del negocio; None si el CRM no lo
        expone todavía (404) — el bot cae al brief local (app/profile.py)."""
        resp = await self._request("GET", "/api/bot/profile")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CrmError(f"profile devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def send_message(self, conversation_id: str, text: str) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/api/bot/messages",
            json={"conversationId": conversation_id, "text": text},
        )
        if resp.status_code == 409:
            raise CrmConflict(_conflict_code(resp))
        if resp.status_code != 200:
            raise CrmError(f"messages devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def put_ficha(
        self, conversation_id: str, ficha: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._request(
            "PUT",
            "/api/bot/ficha",
            json={"conversationId": conversation_id, "ficha": ficha},
        )
        if resp.status_code != 200:
            raise CrmError(f"ficha devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def post_handoff(self, conversation_id: str, reason: str | None = None) -> None:
        resp = await self._request(
            "POST",
            "/api/bot/handoff",
            json={
                "conversationId": conversation_id,
                "reason": canonical_handoff_reason(reason),
            },
        )
        if resp.status_code != 200:
            raise CrmError(f"handoff devolvió {resp.status_code}")

    async def get_catalogo(self, q: str | None = None) -> dict[str, Any]:
        """017 — Catálogo de máquinas: la única fuente de nombres y specs.

        Lo que no esté acá el agente no puede nombrarlo. Trae la tarifa
        vigente de cada modelo solo como referencia: el precio que se le dice
        al lead sale SIEMPRE de `post_cotizar` o de la oferta.
        """
        params = {"q": q} if q else None
        resp = await self._request("GET", "/api/bot/catalogo", params=params)
        if resp.status_code == 404:
            raise InventoryUnavailable("este CRM no tiene el motor de inventario")
        if resp.status_code != 200:
            raise CrmError(f"catalogo devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def get_disponibilidad(
        self,
        conversation_id: str,
        model_id: str,
        desde: str,
        hasta: str,
        horas_por_dia: float | None = None,
    ) -> dict[str, Any]:
        """Disponibilidad y, a la vez, la OFERTA de esta conversación.

        `conversationId` no es opcional: el CRM guarda contra esa conversación
        exactamente los `ofertaId` que devuelve acá, y después solo acepta
        reservar uno de esos. Cuando no hay, la respuesta igual trae
        `proximaFechaLibre` y `alternativas` reservables — nunca un "no hay"
        seco, que es la forma más cara de perder un lead de anuncio.

        `horas_por_dia` es opcional: sin ella el CRM cotiza jornada completa y
        lo DICE en la nota y en la etiqueta de la oferta. Se manda cuando el
        lead ya pactó las horas, porque el precio de la oferta cambia con
        ellas — RPM cotiza la hora de máquina, no el día.
        """
        params: dict[str, Any] = {
            "conversationId": conversation_id,
            "modeloId": model_id,
            "desde": desde,
            "hasta": hasta,
        }
        if horas_por_dia is not None:
            params["horasPorDia"] = horas_por_dia
        resp = await self._request(
            "GET",
            "/api/bot/disponibilidad",
            params=params,
        )
        if resp.status_code == 404:
            # 404 con cuerpo = modelo desconocido; vacío = no hay inventario.
            if _payload(resp):
                raise CrmConflict("modelo_desconocido", _payload(resp))
            raise InventoryUnavailable("este CRM no tiene el motor de inventario")
        if resp.status_code == 409:
            raise CrmConflict(_conflict_code(resp), _payload(resp))
        if resp.status_code != 200:
            raise CrmError(f"disponibilidad devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def inventory_available(self) -> bool:
        """¿Este CRM ofrece inventario de maquinaria?

        Se pregunta SIN parámetros a propósito: con el inventario encendido el
        CRM responde 422 ("faltan modeloId, desde, hasta…") y con él apagado,
        404. Basta para distinguir, no ensucia la oferta de ninguna
        conversación y no necesita un endpoint nuevo.

        Ante cualquier otra cosa (red caída, 5xx) se asume que SÍ hay: fallar
        hacia "sí" solo cuesta un intento que ya degrada solo; fallar hacia
        "no" le apagaría el catálogo a una instancia que sí lo tiene.
        """
        try:
            resp = await self._request("GET", "/api/bot/disponibilidad")
        except CrmError:
            return True
        return resp.status_code != 404

    async def post_cotizar(
        self,
        model_id: str,
        dias: int,
        horas_por_dia: float,
        con_traslado: bool = False,
        km: float | None = None,
    ) -> dict[str, Any]:
        """El desglose de precio. El agente NUNCA calcula ni redondea.

        `horas_por_dia` es obligatoria porque RPM cotiza la HORA de máquina:
        sin ella no hay precio, y un default silencioso acá sería el servidor
        inventando una jornada que nadie pactó con el lead.
        """
        body: dict[str, Any] = {
            "modeloId": model_id,
            "dias": dias,
            "horasPorDia": horas_por_dia,
            "conTraslado": con_traslado,
        }
        if km is not None:
            body["km"] = km
        resp = await self._request("POST", "/api/bot/cotizar", json=body)
        if resp.status_code == 404:
            if _payload(resp):
                raise CrmConflict("modelo_desconocido", _payload(resp))
            raise InventoryUnavailable("este CRM no tiene el motor de inventario")
        if resp.status_code == 409:
            raise CrmConflict(_conflict_code(resp), _payload(resp))
        if resp.status_code != 200:
            raise CrmError(f"cotizar devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def create_rental(
        self,
        conversation_id: str,
        offer_id: str,
        localidad_obra: str | None = None,
        con_traslado: bool | None = None,
    ) -> dict[str, Any]:
        """Reserva TENTATIVA. Confirmarla es cosa de un humano en el CRM."""
        body: dict[str, Any] = {
            "conversationId": conversation_id,
            "ofertaId": offer_id,
        }
        if localidad_obra:
            body["localidadObra"] = localidad_obra
        if con_traslado is not None:
            body["conTraslado"] = con_traslado
        resp = await self._request("POST", "/api/bot/reservas", json=body)
        if resp.status_code == 409:
            raise _rental_conflict(resp)
        if resp.status_code == 404:
            raise InventoryUnavailable("este CRM no tiene el motor de inventario")
        if resp.status_code not in (200, 201):
            raise CrmError(f"reservas devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def move_rental(self, conversation_id: str, offer_id: str) -> dict[str, Any]:
        """Mueve la tentativa de esta conversación a otra oferta emitida."""
        resp = await self._request(
            "PATCH",
            "/api/bot/reservas",
            json={"conversationId": conversation_id, "ofertaId": offer_id},
        )
        if resp.status_code == 409:
            raise _rental_conflict(resp)
        if resp.status_code == 404:
            if _payload(resp):
                raise CrmConflict("sin_reserva", _payload(resp))
            raise InventoryUnavailable("este CRM no tiene el motor de inventario")
        if resp.status_code != 200:
            raise CrmError(f"mover reserva devolvió {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def post_typing(self, conversation_id: str) -> None:
        """Marca leído + "escribiendo…" (007). Best-effort: sin reintentos."""
        resp = await self._request(
            "POST",
            "/api/bot/typing",
            json={"conversationId": conversation_id},
            timeout=8.0,
        )
        if resp.status_code != 200:
            raise CrmError(f"typing devolvió {resp.status_code}")

    async def post_reset(self, conversation_id: str) -> None:
        """Reinicio de pruebas (spec 002): ficha limpia + IA reactivada + etapa
        al inicio en el CRM. Solo lo dispara el comando /reset de la allowlist."""
        resp = await self._request(
            "POST", "/api/bot/reset", json={"conversationId": conversation_id}
        )
        if resp.status_code != 200:
            raise CrmError(f"reset devolvió {resp.status_code}")

    async def get_media(self, media_id: str) -> tuple[bytes, str]:
        """Descarga un binario de Meta A TRAVÉS del CRM (el token vive allá).

        Devuelve (bytes, mime). Timeout amplio: los adjuntos pueden pesar.
        """
        resp = await self._request(
            "GET", f"/api/bot/media/{media_id}", timeout=60.0
        )
        if resp.status_code != 200:
            raise CrmError(f"media devolvió {resp.status_code}")
        mime = resp.headers.get("content-type") or "application/octet-stream"
        return resp.content, mime

    async def aclose(self) -> None:
        await self._http.aclose()
