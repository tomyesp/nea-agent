# Nea — Guía para Claude

Microservicio FastAPI del agente de **alquiler de maquinaria** para WhatsApp
(fork RPM Construcciones — 017). Recibe el webhook de WhatsApp (Meta Cloud
API), lo releva al CRM ([vocero-crm](https://github.com/tomyesp/vocero-crm)) y
conversa vía un proveedor compatible con la API de OpenAI (OpenRouter) —
**enviando siempre a través del API del CRM**, nunca directo a Meta.

El upstream agendaba citas; este fork alquila máquinas. Cambiaron las tools y
el prompt del negocio; el chasis conductual, el relay, el coalesce, la
hostilidad y la degradación silenciosa son los mismos.

## Stack

Python 3.11 · FastAPI + uvicorn (:8000, `/health`) · asyncpg + migraciones SQL
idempotentes al arranque · httpx (CRM y OpenAI) · pytest + respx · Docker
(python:3.11-slim).

## Mapa del código

| Quieres cambiar… | Toca… |
|---|---|
| El chasis conductual del agente | `app/prompt.py` (NO relajar los NUNCA) |
| La capa de persona del negocio | `app/profile.py` (CRM → brief local → mínimo) |
| Las acciones del bot | `app/tools.py` (catálogo, disponibilidad, cotizar, reservar) + orquestación en `app/turn.py` |
| El contrato con el CRM | `app/crm.py` (espejo del bot gateway de vocero) |
| Webhook/firma/dedup/relay | `app/webhook.py` · `app/relay.py` |
| Coalesce y seguimiento | `app/coalesce.py` · `app/followup.py` |
| Tablas | `migrations/*.sql` (idempotentes, aplican al boot) |

## Reglas duras

- **El bot NUNCA llama a graph.facebook.com para enviar.** Todo por
  `POST {CRM}/api/bot/messages`.
- **Los NUNCA del chasis** (inventar, fingir humano, jerga, datos sensibles,
  seguir vendiendo a un hostil) viven en `app/prompt.py` — cualquier cambio
  de prompt re-corre una verificación de comportamiento end-to-end.
- **`ALLOWED_WA_IDS`**: con valor, solo se responde a esas identidades. No la
  vacíes sin decisión explícita del dueño de la instancia.
- **Degradación silenciosa**: LLM/CRM fallando jamás rompe el webhook ni manda
  texto roto; tras reintentos → silencio + handoff `error`.
- **El inventario lo manda el CRM.** Vocero emite la oferta contra la
  conversación (por eso `conversationId` va SIEMPRE en `get_disponibilidad`) y
  decide qué es reservable. `rental_offers` de Nea es un ESPEJO: sirve para
  etiquetar bonito y frenar alucinaciones antes del viaje de red. Si el CRM
  rechaza un `oferta_id`, su palabra gana — no se discute.
- **El agente no inventa NADA de tres cosas**: máquinas (solo las del
  catálogo), precios (solo de `cotizar` o de una oferta) y fechas (solo las
  que confirmó la disponibilidad). Y jamás dice "confirmada": lo que crea es
  una **tentativa** que confirma un humano en el CRM. Cualquier cambio de
  prompt que toque esto re-corre el self-test de comportamiento.
- **Descuentos, facturación, seguros y plazos largos son handoff.** No los
  negocia el agente: inventar ahí cuesta plata real.
- **El inventario puede no existir.** En Vocero va detrás de la bandera
  `INVENTARIO`, apagada por defecto: esos endpoints responden 404. Se sondea al
  arrancar (`crm.inventory_available()`); sin inventario no se le enseñan al
  modelo las herramientas de maquinaria y el prompt se lo dice.
- **Castellano rioplatense (voseo)**, timezone `America/Argentina/Buenos_Aires`
  y léxico de hostilidad argentino. "Boludo" y "la puta madre" de frustración
  NO son hostilidad: así se habla en obra.

## Definición de Hecho

Typecheck + pytest verdes son el piso. "Hecho" = self-test de comportamiento
end-to-end: conversación real multi-turno, camino feliz e infeliz, iterando
hasta verde. Prohibido delegar la prueba al dueño.

## Credenciales

Nuevas variables → `.env.example` con placeholder `REEMPLAZA_...` y guía
inline. Jamás secretos en el repo ni en logs.
