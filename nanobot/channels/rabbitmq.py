"""RabbitMQ channel implementation using aio-pika."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import RabbitMQConfig


class RabbitMQChannel(BaseChannel):
    """
    RabbitMQ channel.

    Inbound:
    - Consume messages from a configured queue.
    - Each AMQP message body is treated as plain-text content.

    Outbound:
    - If the incoming message had a ``reply_to`` AMQP property, publish the
      response to that queue (via the default exchange).
    - Otherwise publish to the configured exchange / routing_key.
    """

    name = "rabbitmq"

    def __init__(self, config: RabbitMQConfig, bus: MessageBus) -> None:
        super().__init__(config, bus)
        self.config: RabbitMQConfig = config
        self._connection: Any = None
        self._pub_channel: Any = None

    async def start(self) -> None:
        """Connect to RabbitMQ and start consuming messages."""
        import aio_pika

        if not self.config.queue:
            logger.error("RabbitMQ inbound queue not configured (queue is empty)")
            return

        self._running = True
        reconnect_delay = 1

        while self._running:
            try:
                self._connection = await aio_pika.connect_robust(self.config.url)
                channel = await self._connection.channel()
                await channel.set_qos(prefetch_count=self.config.prefetch_count)
                queue = await channel.declare_queue(
                    self.config.queue,
                    durable=True,
                    passive=not self.config.queue_declare,
                )
                self._pub_channel = await self._connection.channel()

                logger.info(
                    "RabbitMQ channel connected, consuming from queue '{}'",
                    self.config.queue,
                )
                reconnect_delay = 1

                async with queue.iterator() as queue_iter:
                    async for message in queue_iter:
                        if not self._running:
                            break
                        async with message.process():
                            await self._on_message(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(
                        "RabbitMQ connection error: {}. Reconnecting in {}s...",
                        e,
                        reconnect_delay,
                    )
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)
            finally:
                if self._connection and not self._connection.is_closed:
                    try:
                        await self._connection.close()
                    except Exception:
                        pass
                self._connection = None
                self._pub_channel = None

    async def _on_message(self, message: Any) -> None:
        """Process a single incoming AMQP message."""
        body = message.body.decode("utf-8", errors="replace")

        # Use app_id as sender identifier, fall back to correlation_id or a default
        sender_id = message.app_id or message.correlation_id or "rabbitmq"

        # Use correlation_id as chat_id for session continuity, fall back to sender_id
        chat_id = message.correlation_id or sender_id

        metadata: dict[str, Any] = {}
        if message.reply_to:
            metadata["reply_to"] = message.reply_to
        if message.correlation_id:
            metadata["correlation_id"] = message.correlation_id
        if message.message_id:
            metadata["message_id"] = message.message_id

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=body or "[empty message]",
            metadata=metadata,
        )

    async def stop(self) -> None:
        """Stop consuming and close the connection."""
        self._running = False
        if self._connection and not self._connection.is_closed:
            try:
                await self._connection.close()
            except Exception:
                pass
        self._connection = None
        self._pub_channel = None

    async def send(self, msg: OutboundMessage) -> None:
        """Publish a response back to RabbitMQ."""
        if self._pub_channel is None or self._pub_channel.is_closed:
            logger.warning("RabbitMQ publish channel not available, dropping message")
            return

        import aio_pika

        metadata = msg.metadata or {}

        # Determine where to route the reply:
        #   1. reply_to from AMQP message property (most precise — RPC pattern)
        #   2. configured routing_key (static fallback)
        #   3. chat_id as last resort (equals correlation_id or app_id from inbound)
        reply_to: str | None = metadata.get("reply_to")
        routing_key: str = reply_to or self.config.routing_key or msg.chat_id

        props: dict[str, Any] = {}
        correlation_id: str | None = metadata.get("correlation_id")
        if correlation_id:
            props["correlation_id"] = correlation_id

        amqp_message = aio_pika.Message(
            body=(msg.content or "").encode("utf-8"),
            **props,
        )

        try:
            if self.config.exchange:
                exchange = await self._pub_channel.get_exchange(self.config.exchange)
            else:
                exchange = self._pub_channel.default_exchange

            await exchange.publish(amqp_message, routing_key=routing_key)
            logger.debug(
                "RabbitMQ: published reply to routing_key='{}'", routing_key
            )
        except Exception as e:
            logger.error("Error publishing RabbitMQ message: {}", e)
            raise
