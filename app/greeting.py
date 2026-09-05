"""El agente no se presenta dos veces en la misma conversación.

017 (fork RPM) — Qué pasaba
---------------------------
El chasis del prompt dice, y tiene que decir, "Primer mensaje: saludo + gancho
+ UNA pregunta abierta". Pero nada le decía al modelo que YA había saludado.
Mientras la conversación fluye eso no se nota; cuando un mensaje de sistema le
reencuadra el turno —la alerta de escalamiento, el candado de cierre— el modelo
escribe lo que se le pidió y después vuelve a empezar la conversación desde
cero, en el mismo mensaje:

    Lamento no poder igualar precios, eso lo ve un asesor y te contesta
    enseguida. Voy a pasar la conversación a un humano.
    ¡Hola! Soy Nea, de RPM Construcciones 👷. ¿Para qué trabajo necesitás la
    máquina?

Al lead le llega una despedida con un saludo pegado atrás. Queda a robot roto,
y encima invita a seguir hablando con la IA que se acaba de despedir.

Por qué en código y no solo en el prompt
----------------------------------------
Al prompt se le agregó la regla (`ya te presentaste, no vuelvas a saludar`),
pero pedirle algo al modelo no es garantizarlo: la misma lección de
`format.py` con el Markdown y de `hostility.py` con el conteo. Lo que SIEMPRE
tiene que pasar se hace acá.

Qué corta, y qué NO
-------------------
Solo la RE-PRESENTACIÓN que arranca una conversación nueva, reconocible por
dos señales juntas: el agente dice quién es, y lo hace después de un saludo
("¡Hola! Soy Nea") o abriendo un renglón nuevo. Cuando eso aparece con texto
válido delante, ese texto es el mensaje real y todo lo que sigue es el
reinicio: se corta ahí.

Deliberadamente NO toca dos casos parecidos:

- "Sí, te atiendo yo. Soy Nea, la asistente de RPM." — contestar quién sos
  porque te lo preguntaron es correcto, y no viene con saludo ni en renglón
  aparte.
- Un saludo al principio del mensaje, sin nada válido delante. Es redundante,
  no roto, y recortarlo se llevaría puesto el contenido que viene pegado.
"""
from __future__ import annotations

import re

#: Interjección de saludo, con los signos y la puntuación que la rodean.
_SALUDO = (
    r"[¡!]*\s*"
    r"(?:hola|buenas|buenos\s+d[ií]as|buen\s+d[ií]a|buenas\s+tardes|"
    r"buenas\s+noches|qu[eé]\s+tal)"
    r"[!¡.,\s]*"
)

#: Cómo se presenta el agente. `me llamo` y `te habla` porque los modelos
#: chicos rotan entre las tres sin motivo.
#:
#: Cierra con `(?!\w)` y no con `\b` a propósito: el nombre sale de la
#: configuración del negocio y puede terminar en algo que no sea letra
#: ("A.J."), y ahí `\b` no encuentra frontera y la regla no dispara nunca.
_PRESENTA = r"(?:soy|me\s+llamo|te\s+habla)\s+{name}(?!\w)"


def strip_restart(text: str, agent_name: str, already_greeted: bool) -> str:
    """Devuelve `text` sin el reinicio de conversación pegado al final.

    `already_greeted` es el estado ANTES de este turno: en el primer contacto
    el saludo es lo correcto y no se toca nada.
    """
    if not already_greeted or not text or not agent_name.strip():
        return text

    presenta = _PRESENTA.format(name=re.escape(agent_name.strip()))
    patron = re.compile(
        # (a) saludo + presentación, en cualquier parte del mensaje.
        rf"(?P<a>{_SALUDO}{presenta})"
        # (b) presentación abriendo un renglón que no es el primero.
        rf"|(?:(?<=\n))(?P<b>{presenta})",
        re.IGNORECASE,
    )
    m = patron.search(text)
    if not m:
        return text

    head = text[: m.start()].strip()
    # Sin nada válido delante no hay qué salvar: el saludo abre el mensaje y
    # el contenido viene pegado atrás. Se deja como está.
    if not head:
        return text
    return head
