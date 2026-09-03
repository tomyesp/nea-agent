"""Persistencia real del bot: pool asyncpg + migraciones SQL idempotentes.

Implementa el protocolo `Store` de app/state.py contra Postgres.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from app.state import BotMessage, Conversation, PendingSend, RelayItem, RentalOffer

logger = logging.getLogger("nea.db")

_CONV_COLUMNS = frozenset(
    {
        "crm_conversation_id",
        "phase",
        "greeted",
        "media_notice_sent",
        "followup_due_at",
        "followup_sent",
        "last_inbound_at",
        "stalled_at",
    }
)


def _conv_from_row(row: asyncpg.Record) -> Conversation:
    return Conversation(
        id=row["id"],
        wa_identity=row["wa_identity"],
        crm_conversation_id=row["crm_conversation_id"],
        phase=row["phase"],
        greeted=row["greeted"],
        media_notice_sent=row["media_notice_sent"],
        followup_due_at=row["followup_due_at"],
        followup_sent=row["followup_sent"],
        last_inbound_at=row["last_inbound_at"],
        stalled_at=row["stalled_at"],
    )


class PgStore:
    """Store respaldado por Postgres (asyncpg)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PgStore sin conectar — llama connect() primero")
        return self._pool

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)

    async def migrate(self, migrations_dir: Path) -> None:
        """Aplica todas las migraciones en orden. Son idempotentes: re-correr es seguro."""
        files = sorted(migrations_dir.glob("*.sql"))
        async with self.pool.acquire() as conn:
            for path in files:
                logger.info("migración: aplicando %s", path.name)
                await conn.execute(path.read_text(encoding="utf-8"))

    # -------------------------------------------------------------- dedup ---

    async def mark_processed(self, wa_message_id: str) -> bool:
        row = await self.pool.fetchrow(
            """
            INSERT INTO processed_message (wa_message_id) VALUES ($1)
            ON CONFLICT DO NOTHING
            RETURNING wa_message_id
            """,
            wa_message_id,
        )
        return row is not None

    # -------------------------------------------------------------- relay ---

    async def enqueue_relay(self, body: bytes, signature: str | None) -> int:
        row = await self.pool.fetchrow(
            "INSERT INTO relay_queue (body, signature) VALUES ($1, $2) RETURNING id",
            body,
            signature,
        )
        assert row is not None
        return row["id"]

    async def due_relays(self, now: datetime) -> list[RelayItem]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM relay_queue
            WHERE delivered_at IS NULL AND abandoned_at IS NULL AND next_retry_at <= $1
            ORDER BY id
            LIMIT 50
            """,
            now,
        )
        return [
            RelayItem(
                id=r["id"],
                body=bytes(r["body"]),
                signature=r["signature"],
                attempts=r["attempts"],
                created_at=r["created_at"],
                next_retry_at=r["next_retry_at"],
                delivered_at=r["delivered_at"],
                abandoned_at=r["abandoned_at"],
            )
            for r in rows
        ]

    async def mark_relay_delivered(self, relay_id: int) -> None:
        await self.pool.execute(
            "UPDATE relay_queue SET delivered_at = now() WHERE id = $1", relay_id
        )

    async def mark_relay_abandoned(self, relay_id: int) -> None:
        await self.pool.execute(
            "UPDATE relay_queue SET abandoned_at = now() WHERE id = $1", relay_id
        )

    async def reschedule_relay(
        self, relay_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        await self.pool.execute(
            "UPDATE relay_queue SET attempts = $2, next_retry_at = $3 WHERE id = $1",
            relay_id,
            attempts,
            next_retry_at,
        )

    # ----------------------------------------------------- conversaciones ---

    async def get_or_create_conversation(self, wa_identity: str) -> Conversation:
        row = await self.pool.fetchrow(
            """
            INSERT INTO bot_conversation (wa_identity) VALUES ($1)
            ON CONFLICT (wa_identity) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            wa_identity,
        )
        assert row is not None
        return _conv_from_row(row)

    async def update_conversation(self, conversation_id: int, **fields: Any) -> None:
        unknown = set(fields) - _CONV_COLUMNS
        if unknown:
            raise ValueError(f"columnas desconocidas en update_conversation: {unknown}")
        if not fields:
            return
        sets = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(fields))
        await self.pool.execute(
            f"UPDATE bot_conversation SET {sets}, updated_at = now() WHERE id = $1",
            conversation_id,
            *fields.values(),
        )

    async def reset_conversation(self, conversation_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM bot_message WHERE conversation_id = $1",
                    conversation_id,
                )
                await conn.execute(
                    "DELETE FROM rental_offers WHERE conversation_id = $1",
                    conversation_id,
                )
                await conn.execute(
                    """
                    UPDATE bot_conversation
                    SET phase = 'descubrimiento', greeted = FALSE,
                        media_notice_sent = FALSE, followup_due_at = NULL,
                        followup_sent = FALSE, stalled_at = NULL,
                        updated_at = now()
                    WHERE id = $1
                    """,
                    conversation_id,
                )

    # ----------------------------------------------------------- mensajes ---

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO bot_message (conversation_id, role, content, wa_message_id)
            VALUES ($1, $2, $3, $4)
            """,
            conversation_id,
            role,
            content,
            wa_message_id,
        )

    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM bot_message WHERE conversation_id = $1
            ORDER BY id DESC LIMIT $2
            """,
            conversation_id,
            limit,
        )
        return [
            BotMessage(
                id=r["id"],
                conversation_id=r["conversation_id"],
                role=r["role"],
                content=r["content"],
                wa_message_id=r["wa_message_id"],
                created_at=r["created_at"],
            )
            for r in reversed(rows)
        ]

    # ------------------------------------------- ofertas de alquiler (017) ---

    async def replace_rental_offers(
        self, conversation_id: int, offers: list[RentalOffer]
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM rental_offers WHERE conversation_id = $1",
                    conversation_id,
                )
                for offer in offers:
                    await conn.execute(
                        """
                        INSERT INTO rental_offers
                          (conversation_id, offer_id, model_id, label, desde,
                           hasta, amount_cents)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        conversation_id,
                        offer.offer_id,
                        offer.model_id,
                        offer.label,
                        offer.desde,
                        offer.hasta,
                        offer.amount_cents,
                    )

    async def get_rental_offers(self, conversation_id: int) -> list[RentalOffer]:
        rows = await self.pool.fetch(
            "SELECT * FROM rental_offers WHERE conversation_id = $1 ORDER BY id",
            conversation_id,
        )
        return [
            RentalOffer(
                conversation_id=r["conversation_id"],
                offer_id=r["offer_id"],
                model_id=r["model_id"],
                label=r["label"],
                desde=r["desde"],
                hasta=r["hasta"],
                amount_cents=r["amount_cents"],
                offered_at=r["offered_at"],
            )
            for r in rows
        ]

    async def clear_rental_offers(self, conversation_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM rental_offers WHERE conversation_id = $1", conversation_id
        )

    # ------------------------------------------------- envíos pendientes ---

    async def enqueue_pending_send(
        self, conversation_id: int, crm_conversation_id: str, content: str
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO pending_send (conversation_id, crm_conversation_id, content)
            VALUES ($1, $2, $3) RETURNING id
            """,
            conversation_id,
            crm_conversation_id,
            content,
        )
        assert row is not None
        return row["id"]

    async def due_pending_sends(self, now: datetime) -> list[PendingSend]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM pending_send
            WHERE delivered_at IS NULL AND abandoned_at IS NULL AND next_retry_at <= $1
            ORDER BY id
            LIMIT 50
            """,
            now,
        )
        return [
            PendingSend(
                id=r["id"],
                conversation_id=r["conversation_id"],
                crm_conversation_id=r["crm_conversation_id"],
                content=r["content"],
                attempts=r["attempts"],
                created_at=r["created_at"],
                next_retry_at=r["next_retry_at"],
                delivered_at=r["delivered_at"],
                abandoned_at=r["abandoned_at"],
            )
            for r in rows
        ]

    async def mark_pending_send_delivered(self, pending_id: int) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET delivered_at = now() WHERE id = $1", pending_id
        )

    async def mark_pending_send_abandoned(self, pending_id: int) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET abandoned_at = now() WHERE id = $1", pending_id
        )

    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET attempts = $2, next_retry_at = $3 WHERE id = $1",
            pending_id,
            attempts,
            next_retry_at,
        )

    # -------------------------------------------------------- seguimiento ---

    async def due_followups(self, now: datetime) -> list[Conversation]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM bot_conversation
            WHERE followup_due_at IS NOT NULL
              AND followup_due_at <= $1
              AND followup_sent = FALSE
              AND phase <> 'cerrada'
            ORDER BY followup_due_at
            LIMIT 20
            """,
            now,
        )
        return [_conv_from_row(r) for r in rows]

    async def claim_followup(self, conversation_id: int) -> bool:
        # Se marca ANTES de enviar: a lo sumo un empujón, incluso con crash.
        row = await self.pool.fetchrow(
            """
            UPDATE bot_conversation SET followup_sent = TRUE, updated_at = now()
            WHERE id = $1 AND followup_sent = FALSE
            RETURNING id
            """,
            conversation_id,
        )
        return row is not None

    # --------------------------------------------------------------- misc ---

    async def ping(self) -> None:
        await self.pool.fetchval("SELECT 1")

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
