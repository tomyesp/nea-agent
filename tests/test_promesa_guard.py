"""Anunciar una búsqueda y cortar sin buscar deja al lead esperando."""

from __future__ import annotations

import pytest

from app.llm import LlmReply
from app.promesa_guard import anuncia_una_busqueda
from tests.test_catalogo_guard import BUSCAR, _turno
from tests.conftest import FakeLLM


@pytest.mark.parametrize(
    "texto",
    [
        "Ok. Con esa información te busco las mejores opciones para el trabajo.",
        "Dejame revisar qué tenemos y te cuento.",
        "Dame un momento que lo chequeo.",
        "Ya te averiguo.",
        "Voy a buscar la máquina ideal.",
    ],
)
def test_detecta_el_anuncio(texto):
    assert anuncia_una_busqueda(texto)


@pytest.mark.parametrize(
    "texto",
    [
        "¿En qué localidad es la obra?",
        "Te recomiendo la Retroexcavadora 406.",
        "¿Buscás una máquina para zanjas o para cargar?",
    ],
)
def test_no_confunde_otras_respuestas(texto):
    assert not anuncia_una_busqueda(texto)


async def test_si_anuncia_y_no_busca_tiene_otra_vuelta(respx_mock):
    llm = FakeLLM(
        replies=[
            LlmReply(content="Ok, te busco las mejores opciones."),
            BUSCAR,
            LlmReply(content="Para ese pozo va la Retroexcavadora 406, balde de 1 m3."),
        ]
    )
    result, routes, enviados = await _turno(respx_mock, llm)

    assert enviados == ["Para ese pozo va la Retroexcavadora 406, balde de 1 m3."]
    avisos = [
        m["content"]
        for m in llm.calls[1]["messages"]
        if m["role"] == "system" and "anuncia que vas a buscar" in str(m["content"])
    ]
    assert len(avisos) == 1


async def test_si_ya_busco_el_anuncio_no_paga_vuelta_extra(respx_mock):
    llm = FakeLLM(replies=[BUSCAR, LlmReply(content="Te busco la disponibilidad si me decís la fecha.")])
    result, routes, enviados = await _turno(respx_mock, llm)

    assert len(llm.calls) == 2
