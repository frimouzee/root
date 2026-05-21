from __future__ import annotations

import time
from contextlib import suppress
from datetime import timedelta
from hashlib import sha1
from json import JSONDecodeError, dumps, loads
from typing import Any, List, Literal, Optional, Union, Type
from types import TracebackType

from redis.asyncio import Redis as DefaultRedis
from redis.asyncio.connection import BlockingConnectionPool
from redis.asyncio.lock import Lock
from redis.backoff import EqualJitterBackoff
from redis.retry import Retry
from redis.typing import AbsExpiryT, EncodableT, ExpiryT, KeyT
from opentelemetry import trace

from utils.logger import log
from config import REDIS

REDIS_URL = REDIS.DSN

INCREMENT_SCRIPT = b"""
    local current
    current = tonumber(redis.call("incrby", KEYS[1], ARGV[2]))
    if current == tonumber(ARGV[2]) then
        redis.call("expire", KEYS[1], ARGV[1])
    end
    return current
"""

class Redis(DefaultRedis):
    INCREMENT_SCRIPT_HASH = sha1(INCREMENT_SCRIPT).hexdigest()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tracer = trace.get_tracer(__name__)

    async def __aenter__(self) -> Redis:
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        log.info("Shutting down Redis client")
        await self.close()

    @classmethod
    async def from_url(
        cls,
        url: str = REDIS_URL,
        name: str = "evict",
        attempts: int = 100,
        timeout: int = 120,
        **kwargs,
    ) -> Redis:
        retry = Retry(backoff=EqualJitterBackoff(3, 1), retries=attempts)
        connection_pool = BlockingConnectionPool.from_url(
            url, 
            timeout=timeout, 
            max_connections=100,
            retry=retry,
            decode_responses=True, 
            **kwargs
        )

        client = cls(
            connection_pool=connection_pool,
            auto_close_connection_pool=True,
            retry_on_timeout=True,
            health_check_interval=5,
            client_name=name,
        )

        try:
            with client.tracer.start_as_current_span("redis_connect"):
                start = time.perf_counter()
                await client.ping()
                latency = time.perf_counter() - start

                log.info(
                    "Established Redis connection with %sμs latency",
                    int(latency * 1000000),
                )
        except Exception as e:
            log.exception(f"Failed to establish Redis connection: {e}")
            raise

        return client

    async def get(
        self,
        name: str,
        validate: bool = True,
    ) -> Optional[str | int | dict | list]:
        with self.tracer.start_as_current_span("redis_get") as span:
            span.set_attribute("redis.key", name)
            output = await super().get(name)
            
            if not validate or output is None:
                return output

            if output.isnumeric():
                return int(output)

            with suppress(JSONDecodeError):
                return loads(output)

            return output

    async def set(
        self,
        name: KeyT,
        value: EncodableT | dict | list | Any,
        ex: Union[ExpiryT, None] = None,
        px: Union[ExpiryT, None] = None,
        nx: bool = False,
        xx: bool = False,
        keepttl: bool = False,
        get: bool = False,
        exat: Union[AbsExpiryT, None] = None,
        pxat: Union[AbsExpiryT, None] = None,
    ) -> bool | Any:
        with self.tracer.start_as_current_span("redis_set") as span:
            span.set_attribute("redis.key", name)
            if isinstance(value, (dict, list)):
                value = dumps(value, separators=(',', ':'))
            return await super().set(name, value, ex, px, nx, xx, keepttl, get, exat, pxat)

    async def delete(self, key: str) -> int:
        with self.tracer.start_as_current_span("redis_delete") as span:
            span.set_attribute("redis.key", key)
            return await self.pool.delete(key)

    async def exists(self, key: str) -> bool:
        with self.tracer.start_as_current_span("redis_exists") as span:
            span.set_attribute("redis.key", key)
            return await self.pool.exists(key)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            await self.pool.wait_closed()
            log.info("Redis connection closed")

async def create_redis() -> Redis:
    redis = Redis()
    await redis.connect()
    return redis 