"""Fechas de alquiler como las dice el lead, y como las guarda el CRM.

El CRM guarda un período `[desde, hasta)`: `hasta` es el día que la máquina
VUELVE. Es correcto para calcular y pésimo para conversar: "sábado y domingo"
son dos días, y el modelo, con un parámetro `hasta` en la mano, mandaba
`hasta=domingo` — un solo día, cotizado a mitad de precio, con una etiqueta
("12 al 13") que nadie podía distinguir de la correcta. Pasó con un lead real.

Por eso la herramienta ya no habla de `hasta`. El modelo dice lo que diría
cualquiera —primer día, ÚLTIMO día de uso y cuántos días son— y la conversión
al `hasta` exclusivo del CRM se hace acá, en código. Los días van dos veces a
propósito: si el rango y la cuenta no cierran, el modelo leyó mal al lead, y
eso se frena ANTES de emitir una oferta con las fechas equivocadas.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.prompt import DIAS


def parse_dia(raw: Any) -> date | None:
    """"2026-09-12" o un instante ISO completo → date; basura → None."""
    try:
        return date.fromisoformat(str(raw or "").strip()[:10])
    except ValueError:
        return None


def parse_instante(raw: Any) -> datetime | None:
    """Un instante ISO del CRM ("…Z" incluido) → datetime con zona; si no, None."""
    if not raw:
        return None
    try:
        valor = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


def _dia_es(d: date) -> str:
    return f"{DIAS[d.weekday()]} {d.day:02d}/{d.month:02d}"


def _entero(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        valor = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return int(valor) if valor.is_integer() else None


def rango_de_uso(
    desde_raw: Any, ultimo_raw: Any, dias_raw: Any
) -> tuple[str, str] | dict[str, Any]:
    """`(desde, hasta_exclusivo)` en ISO para el CRM, o el error para el LLM."""
    desde = parse_dia(desde_raw)
    ultimo = parse_dia(ultimo_raw)
    if desde is None or ultimo is None:
        return {
            "ok": False,
            "error": "faltan_datos",
            "detalle": (
                "necesito desde y ultimo_dia en formato AAAA-MM-DD: el primer "
                "y el último día que la máquina trabaja en la obra"
            ),
        }
    if ultimo < desde:
        return {
            "ok": False,
            "error": "rango_invalido",
            "detalle": (
                f"ultimo_dia ({ultimo.isoformat()}) es anterior a desde "
                f"({desde.isoformat()}); pedile al lead que aclare las fechas"
            ),
        }
    contados = (ultimo - desde).days + 1
    dias = _entero(dias_raw)
    if dias is None:
        return {
            "ok": False,
            "error": "faltan_datos",
            "detalle": (
                "mandá también dias: cuántos días trabaja la máquina contando "
                f"el primero y el último (del {_dia_es(desde)} al "
                f"{_dia_es(ultimo)} son {contados})"
            ),
        }
    if dias != contados:
        palabra = "día" if contados == 1 else "días"
        return {
            "ok": False,
            "error": "fechas_no_cierran",
            "detalle": (
                f"del {_dia_es(desde)} al {_dia_es(ultimo)} son {contados} "
                f"{palabra} contando los dos, y mandaste dias={dias}. Uno de "
                "los dos está mal: volvé a leer lo que pidió el lead. Si pidió "
                "'sábado y domingo', desde es el sábado, ultimo_dia el domingo "
                "y dias=2. Si no te queda claro, preguntale antes de consultar."
            ),
        }
    return desde.isoformat(), (ultimo + timedelta(days=1)).isoformat()


def vista_de_periodo(desde_raw: Any, hasta_excl_raw: Any) -> dict[str, Any]:
    """Un período del CRM como lo lee el lead: primer día, último día de uso
    y cuántos días son. Nunca se le muestra al modelo el `hasta` exclusivo:
    es el número que lo hizo equivocarse."""
    desde = parse_dia(desde_raw)
    hasta = parse_dia(hasta_excl_raw)
    if desde is None or hasta is None or hasta <= desde:
        return {"desde": str(desde_raw or ""), "ultimo_dia": None, "dias": None}
    return {
        "desde": desde.isoformat(),
        "ultimo_dia": (hasta - timedelta(days=1)).isoformat(),
        "dias": (hasta - desde).days,
    }
