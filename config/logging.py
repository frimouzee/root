import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from typing import Dict, Any
import json
from datetime import datetime
from opentelemetry import trace
from opentelemetry.instrumentation.logging import LoggingInstrumentor
import structlog
from rich.logging import RichHandler

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

class EvictLogger:
    def __init__(self):
        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)
        
        self.console_format = "\x1b[30;46m{process}\033[0m:{levelname:<9} (\x1b[35m{asctime}\033[0m) \x1b[37;3m@\033[0m \x1b[31m{module:<9}\033[0m -> {message}"
        self.file_format = "{asctime} | {levelname:<8} | {name:<15} | {message}"
        
        self.setup_root_logger()
        self.setup_discord_logger()
        self.setup_command_logger()
        self.setup_error_logger()
        
        LoggingInstrumentor().instrument(
            set_logging_format=True,
            log_level=logging.INFO,
        )

    def create_rotating_handler(self, filename: str, format_str: str) -> RotatingFileHandler:
        handler = RotatingFileHandler(
            filename=self.log_dir / filename,
            maxBytes=10 * 1024 * 1024, 
            backupCount=5,
            encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter(format_str, style='{', datefmt="%Y-%m-%d %H:%M:%S"))
        return handler

    def setup_root_logger(self):
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        
        console_handler = RichHandler(
            rich_tracebacks=True,
            tracebacks_show_locals=True,
            show_time=False
        )
        console_handler.setFormatter(logging.Formatter(self.console_format, style='{'))
        root_logger.addHandler(console_handler)
        
        file_handler = self.create_rotating_handler('bot.log', self.file_format)
        root_logger.addHandler(file_handler)
        
        root_logger.addFilter(IgnoreSpecificMessages())

    def setup_discord_logger(self):
        discord_logger = logging.getLogger('discord')
        discord_logger.setLevel(logging.INFO)
        
        handler = TimedRotatingFileHandler(
            filename=self.log_dir / 'discord.log',
            when='midnight',
            interval=1,
            backupCount=7,
            encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter(self.file_format, style='{'))
        discord_logger.addHandler(handler)

    def setup_command_logger(self):
        command_logger = logging.getLogger('commands')
        command_logger.setLevel(logging.INFO)
        
        handler = RotatingFileHandler(
            filename=self.log_dir / 'commands.log',
            maxBytes=5 * 1024 * 1024, 
            backupCount=3,
            encoding='utf-8'
        )
        handler.setFormatter(
            logging.Formatter('{asctime} | {message}', style='{')
        )
        command_logger.addHandler(handler)

    def setup_error_logger(self):
        error_logger = logging.getLogger('errors')
        error_logger.setLevel(logging.ERROR)
        
        handler = RotatingFileHandler(
            filename=self.log_dir / 'errors.log',
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding='utf-8'
        )
        handler.setFormatter(
            logging.Formatter('{asctime} | {levelname} | {message}', style='{')
        )
        error_logger.addHandler(handler)

class IgnoreSpecificMessages(logging.Filter):
    def __init__(self):
        super().__init__()
        self.ignored_patterns = [
            "HTTP Request: POST",
            "HTTP/1.1 429 Too Many Requests",
            "Shard ID None has connected",
            "Websocket closed with",
        ]
        
        self.ignored_modules = {
            "pomice", "client", "web_log", "gateway",
            "launcher", "pyppeteer", "__init__", "_client"
        }

    def filter(self, record: logging.LogRecord) -> bool:
        if record.module in self.ignored_modules:
            return False
            
        message = record.getMessage()
        return not any(pattern in message for pattern in self.ignored_patterns)

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_data: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "message": record.getMessage(),
        }
        
        if hasattr(record, "trace_id"):
            log_data["trace_id"] = record.trace_id
            
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
            
        return json.dumps(log_data)

logger = EvictLogger()

bot_logger = logging.getLogger("bot")
cmd_logger = logging.getLogger("commands")
err_logger = logging.getLogger("errors")
