"""Configuración tipada del bot (pydantic-settings).

Todas las variables se documentan en `.env.example`. Los defaults permiten
importar el módulo sin entorno (los tests inyectan valores explícitos);
la validación de lo obligatorio ocurre al arranque real.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


def canonical_identity(wa_id: str) -> str:
    """Canonicaliza una identidad de WhatsApp, IGUAL que el CRM.

    Esta función tiene que coincidir con `normalizeMx` de Vocero, porque la
    identidad que sale de acá es la llave con la que se le pide el contexto al
    CRM: si Nea normaliza distinto, el CRM devuelve 404 y el lead queda sin
    respuesta.

    México: Meta a veces reporta `521XXXXXXXXXX` (13 dígitos con el "1" de
    móvil) y a veces `52XXXXXXXXXX` — son la misma persona.

    017 — Argentina: acá NO se toca el "9" de móvil (`549…`). Vocero guarda el
    número tal cual lo manda Meta, así que quitárselo rompía el match contra el
    CRM. La tolerancia al 9 vive donde sí es inofensiva: en la allowlist
    (ver `_identities`).

    Los BSUID y otros identificadores pasan tal cual.
    """
    s = wa_id.strip()
    if s.startswith("521") and len(s) == 13 and s.isdigit():
        return "52" + s[3:]
    return s


def identity_variants(wa_id: str) -> set[str]:
    """Las formas en que la MISMA persona puede aparecer escrita.

    Solo para comparar contra listas locales (allowlist, testers): nunca para
    hablar con el CRM. En Argentina el mismo móvil se escribe `5493511234567`
    o `543511234567`; que el dueño cargue una y Meta mande la otra no puede
    dejar al bot mudo.
    """
    base = canonical_identity(wa_id)
    out = {base}
    if base.startswith("549") and len(base) == 13 and base.isdigit():
        out.add("54" + base[3:])
    elif base.startswith("54") and len(base) == 12 and base.isdigit():
        out.add("549" + base[2:])
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Webhook de Meta
    verify_token: str = ""
    meta_app_secret: str = ""  # vacío = no se verifica la firma (dev)

    # CRM (vocero-crm, bot gateway /api/bot/*)
    crm_base_url: str = "http://localhost:3000"
    crm_webhook_url: str = ""  # incluye el segmento del verify token del CRM
    crm_bot_api_key: str = ""

    # Perfil del negocio (capa de persona; ver app/profile.py)
    agent_name: str = "Nea"  # se usa si el CRM no define nombre
    agent_timezone: str = "America/Argentina/Buenos_Aires"  # IANA; fechas del prompt
    brief_path: str = ""  # markdown local, fallback si el CRM no tiene perfil

    # LLM
    openai_api_key: str = ""
    # 017 — Base URL del proveedor. Vacío = OpenAI. Con valor apunta a
    # cualquier proveedor compatible (OpenRouter: https://openrouter.ai/api/v1).
    # OJO: la transcripción de audio es una API de OpenAI; contra otro
    # proveedor degrada honesta (el agente dice que no pudo escuchar el audio).
    openai_base_url: str = ""
    openai_model: str = "gpt-4o-mini"
    # 017 — Modelo que ESCUCHA las notas de voz. Por defecto uno que oye audio
    # nativo por chat, así OpenRouter alcanza para todo. Si acá se pone un
    # modelo de transcripción (whisper-1), se usa la API de OpenAI, que exige
    # una key de OpenAI de verdad.
    audio_model: str = "google/gemini-2.5-flash-lite"
    history_window: int = 10

    # Guardarraíles y tiempos
    allowed_wa_ids: str = ""
    # Identidades que pueden usar el comando /reset. Va SEPARADA de
    # allowed_wa_ids a propósito: en producción esa lista va vacía (el agente
    # atiende a todos los leads), y cuando el /reset colgaba de ella el
    # comando quedaba muerto justo donde hace falta — para correr una ronda
    # de pruebas en vivo había que cerrarle la puerta a los leads reales.
    tester_wa_ids: str = ""  # CSV; vacía = responde a todos (Constitución V)
    coalesce_seconds: float = 4.0
    followup_hours: float = 4.0
    # "Escribiendo…" casi inmediato al recibir un mensaje (antes del coalesce).
    typing_delay_seconds: float = 0.5

    # Infra
    database_url: str = ""
    port: int = 8000

    # Desarrollo: loguear el JSON crudo de mensajes no-texto entrantes para
    # capturar los formatos reales de Meta (spec 002). Apagar al terminar.
    capture_payloads: bool = False

    @staticmethod
    def _identities(csv: str) -> frozenset[str]:
        """Cada entrada se expande a sus variantes (017): así da igual si el
        dueño cargó el móvil argentino con el 9 o sin él."""
        out: set[str] = set()
        for part in csv.split(","):
            if part.strip():
                out |= identity_variants(part)
        return frozenset(out)

    @property
    def allowed_identities(self) -> frozenset[str]:
        """Allowlist canonicalizada; vacía = sin restricción."""
        return self._identities(self.allowed_wa_ids)

    @property
    def tester_identities(self) -> frozenset[str]:
        """Quién puede correr /reset. Vacía = comando apagado."""
        return self._identities(self.tester_wa_ids)
