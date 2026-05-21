from typing import Any, List, Optional, Union
from json import dumps, loads
from asyncpg import Connection, Pool, Record as DefaultRecord, create_pool
from opentelemetry import trace
from utils.logger import log
from .settings import Settings

def json_encoder(obj: Any) -> str:
    return dumps(obj)

def json_decoder(data: bytes) -> Any:
    return loads(data)

class Record(DefaultRecord):
    def __getattr__(self, name: Union[str, Any]) -> Any:
        return self[name]

    def __setitem__(self, name: Union[str, Any], value: Any) -> None:
        self.__dict__[name] = value

    def to_dict(self) -> dict[str, Any]:
        return dict(self)

class Database(Pool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._statement_cache = {}
        self.tracer = trace.get_tracer(__name__)

    async def execute(
        self,
        query: str,
        *args: Any,
        timeout: Optional[float] = None,
    ) -> str:
        with self.tracer.start_as_current_span("db_execute") as span:
            span.set_attribute("db.statement", query)
            if query not in self._statement_cache:
                self._statement_cache[query] = await self.prepare(query)
            stmt = self._statement_cache[query]
            return await stmt.execute(*args, timeout=timeout)

    async def fetch(
        self,
        query: str,
        *args: Any,
        timeout: Optional[float] = None,
    ) -> List[Record]:
        with self.tracer.start_as_current_span("db_fetch") as span:
            span.set_attribute("db.statement", query)
            if query not in self._statement_cache:
                self._statement_cache[query] = await self.prepare(query)
            stmt = self._statement_cache[query]
            return await stmt.fetch(*args, timeout=timeout)

    async def fetchrow(
        self,
        query: str,
        *args: Any,
        timeout: Optional[float] = None,
    ) -> Optional[Record]:
        with self.tracer.start_as_current_span("db_fetchrow") as span:
            span.set_attribute("db.statement", query)
            if query not in self._statement_cache:
                self._statement_cache[query] = await self.prepare(query)
            stmt = self._statement_cache[query]
            return await stmt.fetchrow(*args, timeout=timeout)

    async def fetchval(
        self,
        query: str,
        *args: Any,
        timeout: Optional[float] = None,
    ) -> Optional[str | int]:
        with self.tracer.start_as_current_span("db_fetchval") as span:
            span.set_attribute("db.statement", query)
            if query not in self._statement_cache:
                self._statement_cache[query] = await self.prepare(query)
            stmt = self._statement_cache[query]
            return await stmt.fetchval(*args, timeout=timeout)

async def init_connection(conn: Connection) -> None:
    await conn.set_type_codec(
        "JSONB",
        schema="pg_catalog",
        encoder=json_encoder,
        decoder=json_decoder,
    )

async def create_db_pool() -> Database:
    try:
        pool = await create_pool(
            config.DATABASE.DSN,
            record_class=Record,
            init=init_connection,
            min_size=10,
            max_size=20,
            max_queries=50000,
            max_inactive_connection_lifetime=300.0,
            command_timeout=60.0,
        )
        if not pool:
            raise RuntimeError("Failed to establish PostgreSQL connection!")
        
        log.info("Successfully connected to PostgreSQL")
        return pool  # type: ignore
    except Exception as e:
        log.exception(f"Failed to connect to PostgreSQL: {e}")
        raise

__all__ = ("Database", "Settings", "create_db_pool") 