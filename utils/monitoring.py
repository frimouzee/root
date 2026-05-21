from opentelemetry import trace, metrics
# ✅ REPLACED: Jaeger thrift with modern OTLP GRPC Trace Exporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
# ✅ FIXED: Modern Prometheus metric reader location
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from prometheus_client import start_http_server
from pyinstrument import Profiler
from typing import Optional, Dict
from collections import defaultdict
import time
import functools
import config
from utils.logger import log
import logging
import socket

logging.getLogger('opentelemetry').setLevel(logging.ERROR)
logging.getLogger('distributed').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('', port))
            return False
        except OSError:
            return True

class PerformanceMonitoring:
    def __init__(self, service_name: str = "evict-bot"):
        try:
            resource = Resource.create({
                "service.name": service_name,
                "environment": "production"
            })
            
            trace_provider = TracerProvider(resource=resource)

            otlp_exporter = OTLPSpanExporter(
                endpoint="http://67.219.138.179:4317",
                insecure=True,
            )

            processor = BatchSpanProcessor(otlp_exporter)
            trace_provider.add_span_processor(processor)

            trace.set_tracer_provider(trace_provider)

            log.info(f"Initialized OTLP exporter at {otlp_exporter._endpoint}")

            self.tracer = trace.get_tracer(service_name)

            with self.tracer.start_as_current_span("test_initialization") as span:
                span.set_attribute("test", "true")
                span.set_attribute("timestamp", time.time())
                log.info("Created test span for OTLP verification")

            reader = PrometheusMetricReader()
            meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
            metrics.set_meter_provider(meter_provider)
            self.meter = metrics.get_meter(service_name)
            
            base_port = 28000
            max_attempts = 10
            prometheus_port = None
            
            for port in range(base_port, base_port + max_attempts):
                if not is_port_in_use(port):
                    prometheus_port = port
                    break
            
            if prometheus_port is None:
                log.warning("Could not find available port for Prometheus metrics")
                self.metrics_enabled = False
            else:
                start_http_server(port=prometheus_port)
                self.metrics_enabled = True
                log.info(f"Prometheus metrics available at :{prometheus_port}/metrics")
            
            if self.metrics_enabled:
                self.command_counter = self.meter.create_counter(
                    name="bot_commands",
                    description="Number of commands executed",
                    unit="1"
                )
                
                self.command_duration = self.meter.create_histogram(
                    name="command_duration",
                    description="Duration of command execution",
                    unit="ms"
                )
                
                self.guild_gauge = self.meter.create_up_down_counter(
                    name="guild_count",
                    description="Number of guilds the bot is in",
                    unit="1"
                )

            RequestsInstrumentor().instrument()
            AioHttpClientInstrumentor().instrument()

            self.profiler: Optional[Profiler] = None
            self.stats = defaultdict(lambda: {
                'calls': 0,
                'errors': 0,
                'total_time': 0
            })
            self._last_cleanup = time.time()
            log.info(f"Performance monitoring initialized for service {service_name}")
            log.info("Connected to OTLP collector at http://67.219.138.179:4317")

        except Exception as e:
            log.error(f"Failed to initialize performance monitoring: {e}", exc_info=True)
            raise

    def setup(self, otlp_endpoint: str = None, **kwargs):
        """Setup monitoring with optional OTLP endpoint."""
        try:
            log.info("Monitoring setup complete")
        except Exception as e:
            log.error(f"Failed to setup monitoring: {e}")

    def cleanup(self):
        """Cleanup monitoring systems"""
        if self.profiler:
            self.profiler.stop()
            self.profiler.write_html("profile_results.html")

    def trace_method(self, func):
        """Decorator to trace method execution."""
        async def wrapper(*args, **kwargs):
            with self.tracer.start_as_current_span(func.__name__) as span:
                try:
                    result = await func(*args, **kwargs)
                    return result
                except Exception as e:
                    span.record_exception(e)
                    raise
        return wrapper

    def record_operation(self, name: str, duration: float, error: bool = False):
        """Record an operation's duration and status."""
        with self.tracer.start_as_current_span(name) as span:
            span.set_attribute("duration", duration)
            span.set_attribute("error", error)

    def get_stats(self) -> Dict:
        """Get current statistics"""
        return dict(self.stats)

    def shutdown(self):
        """Cleanup monitoring resources."""
        pass

    def create_span(self, name: str, attributes: dict = None):
        """Create a new span with optional attributes."""
        try:
            span = self.tracer.start_span(name)
            if attributes:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            return span
        except Exception as e:
            log.error(f"Failed to create span {name}: {e}")
            return None

    async def record_metric(self, name: str, value: float, attributes: dict = None):
        """Record a metric with optional attributes."""
        try:
            with self.tracer.start_span(name) as span:
                span.set_attribute("value", value)
                if attributes:
                    for key, value in attributes.items():
                        span.set_attribute(key, value)
        except Exception as e:
            log.error(f"Failed to record metric {name}: {e}")

    def record_command(self, command_name: str, guild_id: str = None):
        """Record a command execution."""
        if not hasattr(self, 'metrics_enabled') or not self.metrics_enabled:
            return
        try:
            self.command_counter.add(1, {"command": command_name, "guild": guild_id})
        except Exception as e:
            log.error(f"Failed to record command metric: {e}")

    def time_command(self, command_name: str):
        """Context manager to time command execution."""
        if not hasattr(self, 'metrics_enabled') or not self.metrics_enabled:
            return lambda: None
            
        start_time = time.time()
        
        def _record_duration():
            try:
                duration = (time.time() - start_time) * 1000
                self.command_duration.record(duration, {"command": command_name})
            except Exception as e:
                log.error(f"Failed to record command duration: {e}")
        
        return _record_duration

    def update_guild_count(self, count: int):
        """Update the guild count metric."""
        try:
            self.guild_gauge.add(count)
        except Exception as e:
            log.error(f"Failed to update guild count: {e}")

monitoring = PerformanceMonitoring()

def setup_monitoring():
    """Setup monitoring with OpenTelemetry."""
    resource = Resource.create({
        "service.name": "evict.bot",
        "service.version": "1.0.0"
    })

    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)

    return provider

def cleanup_monitoring():
    """Cleanup monitoring systems"""
    try:
        monitoring.cleanup()
        log.info("Monitoring cleanup complete")
    except Exception as e:
        log.warning(f"Failed to cleanup monitoring: {e}")

__all__ = ['monitoring', 'setup_monitoring', 'cleanup_monitoring'] 