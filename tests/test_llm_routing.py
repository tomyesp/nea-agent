"""017 — Razonamiento controlado y modelos de respaldo en cada pedido.

Qwen 3.8 Flash lo sirven dos proveedores y en el banco de modelos dio un 429:
sin respaldo, tres fallos seguidos dejan al lead sin respuesta. Y un modelo
razonador en su modo por defecto tardaba de 30 s a 2 minutos por turno.
"""
from __future__ import annotations

import json
import logging

import httpx

from app.llm import OpenAiLlm, fallback_models, reasoning_param

PROVIDER = "https://openrouter.test/api/v1"
MENSAJES = [{"role": "user", "content": "hola"}]
HERRAMIENTA = [
    {
        "type": "function",
        "function": {"name": "x", "parameters": {"type": "object", "properties": {}}},
    }
]


def _respuesta(model: str, content: str = "hola") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
        },
    )


def test_la_variable_de_razonamiento_se_traduce_al_parametro():
    assert reasoning_param("") is None
    assert reasoning_param(None) is None
    assert reasoning_param("off") == {"enabled": False}
    assert reasoning_param(" OFF ") == {"enabled": False}
    assert reasoning_param("low") == {"effort": "low"}
    # Un typo no tumba al agente: se ignora.
    assert reasoning_param("apagadisimo") is None


def test_la_lista_de_respaldo_se_limpia():
    assert fallback_models("") == []
    assert fallback_models(" anthropic/claude-haiku-4.5 , openai/gpt-4o-mini,") == [
        "anthropic/claude-haiku-4.5",
        "openai/gpt-4o-mini",
    ]


async def test_el_pedido_lleva_razonamiento_y_respaldo(respx_mock):
    llm = OpenAiLlm(
        "sk-test",
        "qwen/qwen3.8-flash",
        base_url=PROVIDER,
        reasoning="off",
        fallbacks="anthropic/claude-haiku-4.5",
    )
    route = respx_mock.post(f"{PROVIDER}/chat/completions").mock(
        return_value=_respuesta("qwen/qwen3.8-flash")
    )
    reply = await llm.complete(MENSAJES, tools=HERRAMIENTA)
    assert reply.content == "hola"
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "qwen/qwen3.8-flash"
    # El principal va primero: el respaldo solo entra si el principal no puede.
    assert body["models"] == ["qwen/qwen3.8-flash", "anthropic/claude-haiku-4.5"]
    assert body["reasoning"] == {"enabled": False}
    assert body["tools"]
    assert llm.usage["respaldo"] == 0


async def test_sin_configurar_nada_el_pedido_es_el_de_siempre(respx_mock):
    llm = OpenAiLlm("sk-test", "openai/gpt-4o-mini", base_url=PROVIDER)
    route = respx_mock.post(f"{PROVIDER}/chat/completions").mock(
        return_value=_respuesta("openai/gpt-4o-mini")
    )
    await llm.complete(MENSAJES)
    body = json.loads(route.calls[0].request.content)
    assert "models" not in body
    assert "reasoning" not in body


async def test_si_contesta_el_respaldo_queda_en_el_log(respx_mock, caplog):
    """Si pasa seguido, el principal no está sirviendo: tiene que poder verse."""
    llm = OpenAiLlm(
        "sk-test",
        "qwen/qwen3.8-flash",
        base_url=PROVIDER,
        fallbacks="anthropic/claude-haiku-4.5",
    )
    respx_mock.post(f"{PROVIDER}/chat/completions").mock(
        return_value=_respuesta("anthropic/claude-4.5-haiku-20251001")
    )
    with caplog.at_level(logging.WARNING, logger="nea.llm"):
        reply = await llm.complete(MENSAJES)
    assert reply.content == "hola"
    assert llm.usage["respaldo"] == 1
    assert "modelo de respaldo" in caplog.text


async def test_una_version_con_fecha_del_principal_no_cuenta_como_respaldo(respx_mock):
    llm = OpenAiLlm(
        "sk-test",
        "qwen/qwen3.8-flash",
        base_url=PROVIDER,
        fallbacks="anthropic/claude-haiku-4.5",
    )
    respx_mock.post(f"{PROVIDER}/chat/completions").mock(
        return_value=_respuesta("qwen/qwen3.8-flash-20260826")
    )
    await llm.complete(MENSAJES)
    assert llm.usage["respaldo"] == 0


async def test_el_audio_no_viaja_con_el_respaldo_de_la_conversacion(respx_mock):
    """La lista de respaldo es del modelo de conversación: con el audio le
    pediría transcribir a un modelo que no oye."""
    llm = OpenAiLlm(
        "sk-test",
        "qwen/qwen3.8-flash",
        audio_model="google/gemini-2.5-flash-lite",
        base_url=PROVIDER,
        reasoning="off",
        fallbacks="anthropic/claude-haiku-4.5",
    )
    route = respx_mock.post(f"{PROVIDER}/chat/completions").mock(
        return_value=_respuesta("google/gemini-2.5-flash-lite", "hola, necesito una retro")
    )
    texto = await llm.transcribe(b"OggS-audio-falso", "audio/ogg")
    assert texto == "hola, necesito una retro"
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "google/gemini-2.5-flash-lite"
    assert "models" not in body
    assert "reasoning" not in body
