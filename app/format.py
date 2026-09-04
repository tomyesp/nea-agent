"""Traducción de Markdown a formato de WhatsApp, determinista.

017 Fase 7 — Por qué no alcanza el prompt
-----------------------------------------
El chasis dice, con todas las letras, que WhatsApp usa *un* asterisco y que
`**doble**` se ve literal. Los modelos chicos igual escriben Markdown más o
menos la mitad de las veces: están entrenados sobre texto donde `**` es lo
normal, y una regla más en un prompt de 13.000 caracteres no gana esa pelea.
Lo detectó el Laboratorio en su primera corrida: `**Retroexcavadora JCB 3CX**`
tal cual, en la respuesta a un lead.

Mismo criterio que el contador de hostilidad (app/hostility.py): lo que tiene
que pasar SIEMPRE no se le pide al modelo, se hace acá. El prompt se queda
igual — sirve para que el modelo acierte solo la mayoría de las veces — y esto
es la red que hace que el lead nunca vea la diferencia.

Deliberadamente conservador: solo toca lo que WhatsApp muestra roto. No
reescribe, no recorta y no "mejora" la redacción del modelo.
"""
from __future__ import annotations

import re

# **negrita** → *negrita*. El cuerpo no puede empezar ni terminar con espacio
# (así `2 * 3 * 4` no se convierte en nada) ni contener saltos de línea.
_BOLD = re.compile(r"\*\*(?!\s)([^\n*]+?)(?<!\s)\*\*")
# __cursiva__ → _cursiva_ (Markdown; en WhatsApp la cursiva es un guion bajo).
_ITALIC = re.compile(r"__(?!\s)([^\n_]+?)(?<!\s)__")
# Títulos ## al principio de una línea: en WhatsApp son un gato suelto.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
# [texto](url) → texto (url). WhatsApp no linkea, muestra el corchete.
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def to_whatsapp(text: str) -> str:
    """Devuelve el texto con el formato que WhatsApp sí entiende."""
    if not text:
        return text
    out = _BOLD.sub(r"*\1*", text)
    out = _ITALIC.sub(r"_\1_", out)
    out = _HEADING.sub("", out)
    out = _LINK.sub(r"\1 (\2)", out)
    return out
