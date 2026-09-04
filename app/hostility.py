"""Detector determinista de hostilidad sostenida (spec 002, US2-6 / AC-18).

Por qué existe: la regla de negocio "al tercer mensaje hostil → handoff" exige
CONTAR entre turnos, y eso es justo lo que un LLM hace de forma no confiable
(en el bench salió flaky: a veces contaba, a veces seguía vendiendo). El
conteo es determinista aquí; el LLM solo pone la redacción del cierre. El
handoff al tercer strike lo GARANTIZA turn.py aunque el modelo no llame la
herramienta.

El léxico es deliberadamente conservador: agresión DIRIGIDA y desprecio
(insultos, acusaciones de estafa), no coloquialismos. En Argentina "boludo",
"che" o un "la puta madre" de frustración NO cuentan solos — así se habla, y
un obrero puteando por el clima no es un lead hostil. El costo de un falso
positivo es bajo de todas formas: handoff = alerta al dueño, que puede
reactivar la IA con un clic.

017 — Léxico adaptado al rioplatense; se conservan los patrones mexicanos del
upstream porque no estorban y siguen cubiertos por sus tests.
"""
from __future__ import annotations

import re

HOSTILE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        # --- Rioplatense (017) ---
        # Insultos dirigidos. "boludo" solo NO entra: es muletilla; sí entra
        # cuando viene con el vocativo agresivo ("sos un boludo", "andá...").
        r"\band[aá]\s+(a|al)\s+(la\s+)?(concha|mierda|carajo|puta)",
        r"\bsos\s+un[oa]?\s+\w*\s*(boludo|pelotudo|forro|garca|chanta|inútil|estafador)",
        r"\bpelotud[oa]s?\b|\bforr[oa]s?\b|\bgarcas?\b|\bchantas?\b|\bturr[oa]s?\b",
        r"\bla\s+concha\s+de\s+(tu|la)\b|\bconchud[oa]s?\b",
        r"\bhij[oa]\s+de\s+(puta|mil)\b",
        r"\bmuert[oa]s?\s+de\s+hambre\b|\bcagador(es)?\b|\bversero?s?\b",
        # --- Español general: fraude y desprecio ---
        # "ladrón/ladrones" faltaba y es la acusación MÁS común en español:
        # la detectó el Laboratorio (persona `hostil`), que llegó al cuarto
        # mensaje sin que el contador viera un solo strike.
        r"\bladr(ón|on|ones)a?s?\b|\bchor(r|)os?\b",
        r"\bestafador(es)?\b|\bestafas?\b|\bfraudes?\b|\brater[oa]s?\b|\bratas?\b|\brob[oa]s?\b",
        r"\bimb[eé]cil(es)?\b|\best[uú]pid[oa]s?\b|\bidiotas?\b|\bpendej[oa]s?\b",
        r"\bbasura\b|\bporquer[ií]a\b|\bmierdas?\b",
        r"puro humo|pura mentira|puros? cuentos?|es una (farsa|estafa)",
        # --- Mexicano (upstream) ---
        r"vete (mucho )?a la (verga|chingada|mierda|fregada)",
        r"chinga (tu|su) madre|chinguen a su madre|vale (verga|madre)",
        r"\bchafas?\b|\bculer[oa]s?\b",
        r"p[ií]nches?\s+\w+",
    )
)

# Alerta que se inyecta como mensaje de sistema en el turno del tercer strike.
ALERT = (
    "ALERTA DEL SISTEMA (esto NO lo escribió el lead): detectado el TERCER "
    "mensaje hostil seguido. En ESTE turno tu respuesta es únicamente UNA "
    "línea digna de cierre — sin invitación, sin pitch, sin pregunta — y "
    "llamás la herramienta handoff con razón \"hostilidad\". La conversación "
    "pasa al dueño del negocio para que él decida (responder, ignorar o bloquear)."
)


def is_hostile(text: str) -> bool:
    return any(p.search(text) for p in HOSTILE_PATTERNS)


def hostile_streak(user_texts: list[str]) -> int:
    """Mensajes hostiles CONSECUTIVOS al final de la conversación del lead.

    Un mensaje no-hostil corta la racha (el lead que se calmó vuelve a
    empezar de cero — la regla es por hostilidad SOSTENIDA).
    """
    streak = 0
    for text in reversed(user_texts):
        if is_hostile(text or ""):
            streak += 1
        else:
            break
    return streak
