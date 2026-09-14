-- 017 — Las ofertas del espejo saben cuándo vencen.
--
-- Hasta acá cada consulta de disponibilidad REEMPLAZABA todas las ofertas de
-- la conversación. Eso rompía la comparación más común del negocio: el lead
-- pregunta por dos retros, el agente consulta una y después la otra, y la
-- segunda consulta borraba la oferta de la primera. Cuando el lead eligió la
-- primera ya no era reservable, y el agente terminó tomando la otra.
--
-- Ahora las ofertas de máquinas distintas conviven. Para que convivir no
-- signifique arrastrar ofertas muertas, el espejo guarda el vencimiento que
-- manda el CRM y deja de mostrarlas cuando pasan. La autoridad sigue siendo
-- el CRM: si igual se intenta reservar una vencida, la rechaza él.
--
-- Idempotente, como todas: corre en cada arranque.

ALTER TABLE rental_offers ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
