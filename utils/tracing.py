from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes

resource = Resource.create({
    ResourceAttributes.SERVICE_NAME: "evict.bot",
    ResourceAttributes.SERVICE_VERSION: "1.0.0",
})

provider = TracerProvider(resource=resource)

# processor = BatchSpanProcessor(ConsoleSpanExporter())
# provider.add_span_processor(processor)

trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__) 