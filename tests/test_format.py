"""017 Fase 7 — El Markdown nunca llega al lead.

El chasis ya se lo pide al modelo; esto es la red que hace que no importe si
obedece. Lo encontró el Laboratorio: `**Retroexcavadora JCB 3CX**` tal cual en
la respuesta a un lead simulado.
"""
from __future__ import annotations

from app.format import to_whatsapp


def test_negrita_de_markdown_pasa_a_negrita_de_whatsapp():
    assert to_whatsapp("la **Retro JCB 3CX** está libre") == (
        "la *Retro JCB 3CX* está libre"
    )


def test_varias_negritas_en_la_misma_linea():
    assert to_whatsapp("**JCB 3CX** del **24 al 30** por **$1.391.500**") == (
        "*JCB 3CX* del *24 al 30* por *$1.391.500*"
    )


def test_la_negrita_de_whatsapp_no_se_toca():
    texto = "la *Retro JCB 3CX* está libre"
    assert to_whatsapp(texto) == texto


def test_una_multiplicacion_no_es_negrita():
    # Sin esto, "el traslado son 2 * 3 * 4 km" se comería los asteriscos.
    assert to_whatsapp("son 2 * 3 * 4 km") == "son 2 * 3 * 4 km"


def test_titulos_y_enlaces_de_markdown():
    assert to_whatsapp("## Tarifas\nver [acá](https://rpm.test/x)") == (
        "Tarifas\nver acá (https://rpm.test/x)"
    )


def test_cursiva_de_markdown():
    assert to_whatsapp("es __urgente__") == "es _urgente_"


def test_no_cruza_saltos_de_linea():
    # Dos asteriscos sueltos en líneas distintas no son una negrita partida.
    texto = "opción **A\ny opción** B"
    assert to_whatsapp(texto) == texto


def test_texto_vacio_o_sin_formato():
    assert to_whatsapp("") == ""
    assert to_whatsapp("hola, todo bien") == "hola, todo bien"
