-- 017 (fork RPM) — Espejo local de las ofertas de alquiler del CRM.
--
-- Reemplaza a `offered_slots` (agendamiento de citas): este fork no agenda
-- citas, alquila máquinas. El `offer_id` es el id opaco que emite Vocero
-- contra la conversación y lo ÚNICO que acepta de vuelta al reservar; acá se
-- espeja solo para etiquetar bonito y frenar alucinaciones antes del viaje de
-- red. La autoridad sigue siendo el CRM.
--
-- Idempotente, como todas: corre en cada arranque.

CREATE TABLE IF NOT EXISTS rental_offers (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
    -- El ofertaId del CRM (roff_...). Único por conversación: la misma oferta
    -- no se espeja dos veces aunque el LLM repita la consulta.
    offer_id        TEXT NOT NULL,
    model_id        TEXT NOT NULL DEFAULT '',
    label           TEXT NOT NULL DEFAULT '',
    -- Fechas como texto ISO tal cual las ofreció el CRM: se muestran, no se
    -- calculan de este lado (calcular fechas acá es cómo se inventan rangos).
    desde           TEXT NOT NULL DEFAULT '',
    hasta           TEXT NOT NULL DEFAULT '',
    amount_cents    BIGINT NOT NULL DEFAULT 0,
    offered_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS rental_offers_conv_offer_uq
    ON rental_offers (conversation_id, offer_id);

CREATE INDEX IF NOT EXISTS rental_offers_conv_idx
    ON rental_offers (conversation_id, offered_at);

-- La tabla de la agenda ya no la escribe nadie en este fork. Se deja caer para
-- que una instancia migrada no arrastre ofertas de citas que jamás se van a
-- poder reservar (sus tools ya no existen).
DROP TABLE IF EXISTS offered_slots;
