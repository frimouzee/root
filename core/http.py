import time
import asyncio
from collections import defaultdict
from discord.http import HTTPClient, Route
from opentelemetry import trace
from typing import Any

class MonitoredHTTPClient(HTTPClient):
    def __init__(self, session, *, bot=None):
        super().__init__(session)
        self.bot = bot
        self._global_over = asyncio.Event()
        self._global_over.set()
        self.tracer = trace.get_tracer(__name__)
        
        if not hasattr(self.bot, 'api_stats'):
            self.bot.api_stats = defaultdict(lambda: {
                'calls': 0,
                'errors': 0,
                'total_time': 0,
                'rate_limits': 0
            })
        if not hasattr(self.bot, '_last_stats_cleanup'):
            self.bot._last_stats_cleanup = time.time()

    async def request(self, route: Route, **kwargs) -> Any:
        method = route.method
        path = route.path
        endpoint = f"{method} {path}"
        
        with self.tracer.start_as_current_span(
            f"discord_api_request",
            attributes={
                "http.method": method,
                "http.route": path
            }
        ) as span:
            start_time = time.time()
            try:
                response = await super().request(route, **kwargs)
                elapsed = time.time() - start_time
                
                self.bot.api_stats[endpoint]['calls'] += 1
                self.bot.api_stats[endpoint]['total_time'] += elapsed
                
                if time.time() - self.bot._last_stats_cleanup > 3600:
                    self.bot.api_stats.clear()
                    self.bot._last_stats_cleanup = time.time()
                
                span.set_attribute("http.status_code", 200)
                return response
                
            except discord.HTTPException as e:
                self.bot.api_stats[endpoint]['errors'] += 1
                if e.status == 429:
                    self.bot.api_stats[endpoint]['rate_limits'] += 1
                span.set_attribute("http.status_code", e.status)
                span.record_exception(e)
                raise 