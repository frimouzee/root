import time
import asyncio
import psutil
from collections import defaultdict
from typing import Optional, Collection, Dict, Any, cast, List
from discord.ext import commands
from discord import (
    Intents, AllowedMentions, Activity, ActivityType, 
    Message, User, ClientUser, ChannelType, PartialMessageable,
    Member, Guild, Invite, HTTPException, NotFound, Forbidden,
    Interaction, MessageType
)
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
from discord.ext.commands import Context
from aiohttp import ClientSession, TCPConnector
from redis.asyncio import Redis
from discord.ext.commands import CooldownMapping, BucketType
from asyncpraw import Reddit as RedditClient
from opentelemetry import trace
from multiprocessing import Pool, cpu_count as logical_cpu_count
import discord
import os
import glob
import secrets
import importlib
from colorama import Fore
from discord.ext.commands import (
    CommandError, CommandNotFound, DisabledCommand, NotOwner,
    MissingRequiredArgument, MissingRequiredAttachment, BadLiteralArgument,
    FlagError, TooManyFlags, BadFlagArgument,
    MissingRequiredFlag, MissingFlagArgument,
    CommandInvokeError, MaxConcurrencyReached,
    CommandOnCooldown, MemberNotFound, UserNotFound,
    RoleNotFound, ChannelNotFound, BadInviteArgument,
    MessageNotFound, BadUnionArgument, RangeError,
    MissingPermissions, NSFWChannelRequired, CheckFailure,
)
from discord.errors import Forbidden, HTTPException, NotFound
from managers.parser.TagScript.exceptions import TagScriptError, EmbedParseError
from core.database import Database, Settings
from core.cache import cache
from utils.computation import heavy_computation
from utils.monitoring import PerformanceMonitoring
from utils.conversions.embed import EmbedScript

from core.http import MonitoredHTTPClient
from utils.prefix import getprefix
from core.help import EvictHelp
from utils.monitoring import PerformanceMonitoring
from utils.optimization import setup_cpu_optimizations
from core.dask import DaskManager
from utils.tracing import tracer
from utils.logger import log
from core.ipc import ClusterIPC
from core.browser import BrowserHandler
import config
from processors.backup import run_pg_dump, process_bunny_upload
from processors.image_generator import process_image_effect
from processors.guild import (
    process_guild_data,
    process_jail_permissions,
    process_add_role
)
from utils.formatter import plural, human_join
from contextlib import suppress
from pomice import NodePool
from core.backup import BackupManager
import asyncpg
from core.context import Context

import jishaku
import jishaku.flags

jishaku.Flags.HIDE = True
jishaku.Flags.RETAIN = True
jishaku.Flags.NO_DM_TRACEBACK = True
jishaku.Flags.NO_UNDERSCORE = True
jishaku.Flags.FORCE_PAGINATOR = True

class Evict(commands.AutoShardedBot, commands.Cog):
    session: ClientSession
    uptime: datetime
    traceback: Dict[str, Exception]
    global_cooldown: CooldownMapping
    owner_ids: Collection[int]
    database: Database
    redis: Redis
    user: ClientUser
    reddit: RedditClient
    version: str = "b4.0"
    user_agent: str = f"Evict (DISCORD BOT/{version})"
    browser: BrowserHandler
    voice_join_times = {}
    voice_update_task = None
    start_time: float
    system_stats: defaultdict
    process: psutil.Process
    _last_system_check: float
    _is_ready: asyncio.Event
    monitoring: PerformanceMonitoring
    translations: dict
    tracer: trace.Tracer
    api_stats: dict
    _last_stats_cleanup: float
    ipc: ClusterIPC
    dask: DaskManager
    _cleanup_event: asyncio.Event
    cluster_id: int
    cluster_count: int
    dask_client: any

    def __init__(self, *args, **kwargs):
        self.monitoring = PerformanceMonitoring()
        self.dask = DaskManager()
        self.translations = {}
        self._load_translations()
        
        self.cluster_id = kwargs.pop('cluster_id', 0)
        self.cluster_count = kwargs.pop('cluster_count', 1)
        self.dask_client = kwargs.pop('dask_client')
        
        super().__init__(
            *args,
            command_prefix=getprefix,
            description=kwargs.get('description'),
            owner_ids=kwargs.get('owner_ids'),
            shard_ids=kwargs.get('shard_ids'),
            shard_count=kwargs.get('shard_count'),
            intents=Intents(
                guilds=True,
                members=True,
                messages=True,
                reactions=True,
                presences=True,
                moderation=True,
                voice_states=True,
                message_content=True,
                emojis_and_stickers=True,
            ),
            allowed_mentions=AllowedMentions(
                replied_user=False,
                everyone=False,
                roles=False,
                users=True,
            ),
            help_command=EvictHelp(),
            case_insensitive=True,
            max_messages=1500,
            activity=Activity(
                type=ActivityType.streaming,
                name="🔗 evict.bot/beta",
                url=f"{config.CLIENT.TWITCH_URL}",
            ),
        )
        
        self._init_attributes()
        self._setup_cooldowns()
        
        self.ipc = None  

    async def setup_hook(self) -> None:
        """Setup hook that runs before the bot starts."""
        try:
            self.monitoring = PerformanceMonitoring(service_name="evict-bot")
            self.tracer = self.monitoring.tracer
            log.info("Performance monitoring initialized")
            
            with self.tracer.start_span("bot_setup") as setup_span:
                await self._init_core_services()  
                await self._init_remaining_services()
                setup_span.set_attribute("status", "complete")
            
            self._setup_process_pool()
            await self.load_cogs()
            
            self._is_ready.set()
            log.info("Setup complete!")
            
        except Exception as e:
            log.error(f"Error in setup_hook: {e}", exc_info=True)
            raise

    async def _handle_cluster_stats(self, data: dict) -> dict:
        """Core handler for cluster stats requests"""
        stats = {
            'guild_count': len(self.guilds),
            'member_count': sum(g.member_count for g in self.guilds)
        }
        log.info(f"Cluster {self.cluster_id} sending stats: {stats}")
        return stats

    async def _init_core_services(self):
        """Initialize core services required for bot operation."""
        self.session = ClientSession(
            headers={"User-Agent": self.user_agent},
            connector=TCPConnector(ssl=False),
        )
        log.info("Created client session")

        self._http = MonitoredHTTPClient(
            self.http._HTTPClient__session,
            bot=self
        )
        log.info("Initialized monitored HTTP client")

        if self.ipc is None:
            self.ipc = ClusterIPC(self, self.cluster_id)
            self.ipc.add_handler("get_cluster_stats", self._handle_cluster_stats)
            await self.ipc.start()
            log.info(f"Started IPC system for cluster {self.cluster_id}")

        self.database = await asyncpg.create_pool(
            dsn=config.DATABASE.DSN,
            min_size=config.DATABASE.MIN_SIZE,
            max_size=config.DATABASE.MAX_SIZE,
            max_queries=config.DATABASE.MAX_QUERIES,
            timeout=config.DATABASE.TIMEOUT,
            command_timeout=config.DATABASE.COMMAND_TIMEOUT
        )
        log.info("Connected to database")

        self.redis = Redis.from_url(config.REDIS.DSN)
        log.info("Connected to Redis")

    async def _init_remaining_services(self):
        """Initialize remaining services after core initialization."""
        self.start_time = time.time()
        self.system_stats = defaultdict(list)
        self.process = psutil.Process()
        self._last_system_check = 0
        self.command_stats = defaultdict(lambda: {'calls': 0, 'total_time': 0})
        log.info("Initialized monitoring systems")

        self.browser = BrowserHandler()
        await self.browser.init()
        log.info("Initialized browser")

        self.voice_update_task = self.loop.create_task(self.update_voice_times())
        log.info("Started voice update task")
        
        self.backup_manager = BackupManager(self)
        self.backup_task = self.loop.create_task(self._backup_task())
        log.info("Started backup manager")

        for guild in self.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if not member.bot:
                        self.voice_join_times[member.id] = time.time()
        log.info("Initialized voice times")

        await self.load_patches()
        log.info("Loaded patches")

        try:
            await self.load_extension("jishaku")
            log.info("Jishaku loaded successfully")
        except Exception as e:
            log.error(f"Failed to load jishaku: {e}")

    def _init_attributes(self):
        """Initialize bot attributes."""
        self.traceback = {}
        self.uptime2 = time.time()
        self.embed_build = EmbedScript("")
        self.cache = cache(self)
        self.start_time = time.time()
        self.system_stats = defaultdict(list)
        self.process = psutil.Process()
        self._last_system_check = 0
        self.command_stats = defaultdict(lambda: {'calls': 0, 'total_time': 0})
        self._is_ready = asyncio.Event()
        self._cleanup_event = asyncio.Event()
        self.voice_join_times = {}
        self.browser = BrowserHandler()

    def _setup_cooldowns(self):
        """Setup cooldown attributes."""
        self.spam_control = commands.CooldownMapping.from_cooldown(
            10, 12.0, commands.BucketType.user
        )
        self.violations = defaultdict(int)
        
        self.global_cooldown = CooldownMapping.from_cooldown(2, 3, BucketType.user)
        self.add_check(self.check_global_cooldown)
        
        self.guild_ratelimits = {
            '10s': CooldownMapping.from_cooldown(
                config.RATELIMITS.PER_10S, 
                10, 
                BucketType.guild
            ),
            '30s': CooldownMapping.from_cooldown(
                config.RATELIMITS.PER_30S, 
                30, 
                BucketType.guild
            ),
            '1m': CooldownMapping.from_cooldown(
                config.RATELIMITS.PER_1M, 
                60, 
                BucketType.guild
            )
        }
        
        self.channel_ratelimit = CooldownMapping.from_cooldown(
            config.RATELIMITS.PER_CHANNEL, 
            5, 
            BucketType.channel
        )
        
        self.heavy_command_cooldown = CooldownMapping.from_cooldown(
            1, 30, BucketType.user
        )

    async def _init_monitoring(self) -> None:
        """Initialize monitoring systems."""
        self.start_time = time.time()
        self.system_stats = defaultdict(list)
        self.process = psutil.Process()
        self._last_system_check = 0
        self.command_stats = defaultdict(lambda: {
            'calls': 0,
            'total_time': 0.0,
            'last_reset': time.time()
        })

    async def _init_voice_system(self) -> None:
        """Initialize voice system and backup manager."""
        self.backup_manager = BackupManager(self)
        
        self.voice_join_times.update({
            member.id: time.time()
            for guild in self.guilds
            for vc in guild.voice_channels
            for member in vc.members
            if not member.bot
        })

        self._setup_process_pool()
        
        self.api_stats = defaultdict(lambda: {
            'calls': 0,
            'errors': 0,
            'total_time': 0,
            'rate_limits': 0
        })
        self._last_stats_cleanup = time.time()

    def _setup_process_pool(self):
        """Setup the process pool for CPU-bound tasks."""
        cpu_count = psutil.cpu_count(logical=True)
        self.process_pool = Pool(
            processes=min(4, cpu_count),
            maxtasksperchild=100  
        )

    @property
    def db(self) -> Database:
        return self.database

    @property
    def owner(self) -> User:
        return self.get_user(self.owner_ids[0])

    def get_message(self, message_id: int) -> Optional[Message]:
        return self._connection._get_message(message_id)

    async def get_or_fetch_user(self, user_id: int) -> User:
        return self.get_user(user_id) or await self.fetch_user(user_id)

    async def on_command(self, ctx: Context) -> None:
        """Custom on_command method that logs command usage."""
        if not ctx.guild:
            return

        if not ctx.command: 
            custom_command = await self.db.fetchrow(
                """
                SELECT word 
                FROM stats.custom_commands 
                WHERE guild_id = $1 AND command = $2
                """,
                ctx.guild.id,
                ctx.invoked_with.lower()
            )
            
            if custom_command:
                ctx.command = type('CustomCommand', (), {
                    'qualified_name': f"wordstats_{ctx.invoked_with}",
                    'cog_name': "Utility"
                })
            else:
                return 

        start_time = time.time()
        command_name = ctx.command.qualified_name

        try:
            await self._track_command_usage(ctx)

            log.info(
                "%s (%s) used %s in %s (%s)",
                ctx.author.name,
                ctx.author.id,
                ctx.command.qualified_name,
                ctx.guild.name,
                ctx.guild.id,
            )

        except Exception as e:
            log.error(f"Error in on_command: {e}")

        finally:
            elapsed = time.time() - start_time
            if not hasattr(self, 'command_stats'):
                self.command_stats = defaultdict(lambda: {'calls': 0, 'total_time': 0})
                
            self.command_stats[command_name]['calls'] += 1
            self.command_stats[command_name]['total_time'] += elapsed

    async def _track_command_usage(self, ctx: Context) -> None:
        """Track command usage statistics."""
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO statistics.daily 
                        (guild_id, date, member_id, messages_sent)
                    VALUES 
                        ($1, CURRENT_DATE, $2, 0)
                    ON CONFLICT (guild_id, date, member_id) DO UPDATE SET 
                        messages_sent = statistics.daily.messages_sent
                    """,
                    ctx.guild.id,
                    ctx.author.id
                )
                
                await conn.execute(
                    """
                    INSERT INTO invoke_history.commands 
                    (guild_id, user_id, command_name, category, timestamp)
                    VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                    """,
                    ctx.guild.id,
                    ctx.author.id,
                    ctx.command.qualified_name,
                    ctx.command.cog_name or "No Category",
                )
        except Exception as e:
            log.error(f"Error tracking command usage: {e}")

    async def on_shard_ready(self, shard_id: int) -> None:
        """Custom on_shard_ready method that logs shard status."""
        log.info(f"Shard {shard_id} is ready, starting post-connection setup...")

        try:
            log.info(f"Shard ID {shard_id} has connected to Gateway")
            
            if all(shard_id in self.shards for shard_id in range(self.shard_count)):
                self._is_ready.set()
                log.info(f"All {self.shard_count} shards connected and ready!")
                
                log.info(f"Logged in as {self.user} (ID: {self.user.id})")
                log.info(f"Connected to {len(self.guilds)} guilds")
                log.info(f"Connected to {len(self.users)} users")
                log.info(f"Using shard count: {self.shard_count}")
                
        except Exception as e:
            log.error(f"Error in on_shard_ready for shard {shard_id}: {e}", exc_info=True)

    async def on_shard_resumed(self, shard_id: int) -> None:
        """Custom on_shard_resumed method that logs shard status."""
        log.info(
            f"Shard ID {Fore.LIGHTGREEN_EX}{shard_id}{Fore.RESET} has {Fore.LIGHTYELLOW_EX}resumed{Fore.RESET}."
        )

    async def connect_nodes(self) -> None:
        """Connect to Lavalink nodes."""
        for _ in range(config.LAVALINK.NODE_COUNT):
            identifier = "evict"
            try:
                await NodePool().create_node(
                    bot=self,
                    host="127.0.0.1",
                    port=config.LAVALINK.PORT,
                    password=config.LAVALINK.PASSWORD,
                    secure=False,
                    identifier=identifier,
                    spotify_client_id=config.AUTHORIZATION.SPOTIFY.CLIENT_ID,
                    spotify_client_secret=config.AUTHORIZATION.SPOTIFY.CLIENT_SECRET,
                )
                log.info(f"Successfully connected to node {identifier}")
            except Exception as e:
                log.error(f"Failed to connect to node {identifier}: {e}")

    async def load_extensions(self) -> None:
        """Load all extensions."""
        await self.load_extension("jishaku")
        for feature in Path("cogs").iterdir():
            if feature.is_dir() and (feature / "__init__.py").is_file():
                try:
                    await self.load_extension(".".join(feature.parts))
                except Exception as exc:
                    log.exception(f"Failed to load extension {feature.name}.", exc_info=exc)

    async def load_patches(self) -> None:
        """Load all patches."""
        for module in glob.glob("managers/patches/**/*.py", recursive=True):
            if module.endswith("__init__.py"):
                continue
            module_name = (
                module.replace(os.path.sep, ".").replace("/", ".").replace(".py", "")
            )
            try:
                importlib.import_module(module_name)
                log.info(f"Patched: {module}")
            except (ModuleNotFoundError, ImportError) as e:
                log.error(f"Error importing {module_name}: {e}")

    async def process_heavy_task(self, *args):
        """Process a heavy computation task"""
        start_time = time.time()
        try:
            result = await self.dask.submit_task(heavy_computation, *args)
            return result
        except Exception as e:
            span.record_exception(e)
            raise

    async def close(self) -> None:
        """Cleanup and close all resources."""
        try:
            await self.monitoring.shutdown()
            
            await self.db.close()
            
            await self.redis.close()
            
            if hasattr(self, 'session'):
                await self.session.close()
            
            if hasattr(self, 'dask_client'):
                await self.dask_client.close()
            
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            [task.cancel() for task in tasks]
            await asyncio.gather(*tasks, return_exceptions=True)
            
        finally:
            await super().close()

    async def log_traceback(self, ctx: Context, exc: Exception) -> Message:
        """Store an Exception in memory for future reference."""
        span.record_exception(exc)
            
        log.exception(
            "Unexpected exception occurred in %s.",
            ctx.command.qualified_name,
            exc_info=exc,
        )

        key = secrets.token_urlsafe(54)
        self.traceback[key] = exc
            
        return await ctx.warn(
            await self.get_text("system.errors.COMMAND.EXCEPTION", ctx,
                command=ctx.command.qualified_name),
            content=f"`{key}`",
        )
        
    async def on_command_error(self, ctx: Context, exc: CommandError) -> Any:
        """Handle command errors with detailed error tracking."""
        log.debug(f"Handling error for command {ctx.command}: {type(exc).__name__}")
        
        if isinstance(exc, MissingRequiredArgument):
            log.debug(f"Missing required argument: {exc.param.name}")
            try:
                await ctx.send_help(ctx.command)
                log.debug("Help message sent successfully")
                return
            except Exception as e:
                log.error(f"Failed to send help: {e}")
                try:
                    await ctx.warn(
                        f"Missing required argument: `{exc.param.name}`\n"
                        f"Use `{ctx.clean_prefix}help {ctx.command.qualified_name}` for more information."
                    )
                    log.debug("Warning message sent successfully")
                    return
                except Exception as e:
                    log.error(f"Failed to send warning: {e}")
                    return
        
        if not ctx.channel:
            log.debug("No channel found")
            return
                
        if not ctx.guild:
            can_send = True
        else:
            can_send = (
                ctx.channel.permissions_for(ctx.guild.me).send_messages
                and ctx.channel.permissions_for(ctx.guild.me).embed_links
            )
            log.debug(f"Can send messages: {can_send}")

        if not can_send:
            log.debug("Cannot send messages in channel")
            return

        if isinstance(exc, (CommandNotFound, DisabledCommand, NotOwner)):
            return

        elif isinstance(exc, (
            MissingRequiredAttachment,
            BadLiteralArgument,
            BadUnionArgument
        )):
            return await ctx.send_help(ctx.command)

        elif isinstance(exc, TagScriptError):
            if isinstance(exc, EmbedParseError):
                return await ctx.warn(
                    await self.get_text("system.errors.SCRIPT.PARSE_ERROR", ctx),
                    *exc.args
                )

        elif isinstance(exc, CommandOnCooldown):
            if exc.retry_after > 30:
                return await ctx.warn(
                    await self.get_text("system.errors.COOLDOWN.MESSAGE", ctx),
                    await self.get_text("system.errors.COOLDOWN.RETRY", ctx, 
                        time=format_timespan(exc.retry_after))
                )
            return await ctx.message.add_reaction("⏰")

        elif isinstance(exc, FlagError):
            if isinstance(exc, TooManyFlags):
                return await ctx.warn(
                    await self.get_text("system.errors.COMMAND.FLAG.DUPLICATE", ctx,
                        flag_name=exc.flag.name)
                )

            elif isinstance(exc, BadFlagArgument):
                try:
                    annotation = exc.flag.annotation.__name__
                except AttributeError:
                    annotation = exc.flag.annotation.__class__.__name__

                msg = await self.get_text("system.errors.COMMAND.FLAG.CAST_ERROR", ctx,
                    flag_name=exc.flag.name,
                    annotation=annotation)
                
                if annotation == "Status":
                    msg = [msg, await self.get_text("system.errors.COMMAND.FLAG.STATUS_HINT", ctx)]
                    
                return await ctx.warn(*msg)

            elif isinstance(exc, MissingRequiredFlag):
                return await ctx.warn(
                    await self.get_text("system.errors.COMMAND.FLAG.MISSING_REQUIRED", ctx,
                        flag_name=exc.flag.name)
                )

            elif isinstance(exc, MissingFlagArgument):
                return await ctx.warn(
                    await self.get_text("system.errors.COMMAND.FLAG.MISSING_VALUE", ctx,
                        flag_name=exc.flag.name)
                )

        elif isinstance(exc, CommandInvokeError):
            return await ctx.warn(exc.original)

        elif isinstance(exc, MaxConcurrencyReached):
            if ctx.command.qualified_name in ("lastfm set", "lastfm index"):
                return

            return await ctx.warn(
                await self.get_text("system.errors.CONCURRENCY.MAX_CONCURRENT", ctx,
                    number=plural(exc.number),
                    time="time",
                    per=exc.per.name),
                delete_after=5,
            )

        elif isinstance(exc, (MemberNotFound, UserNotFound, RoleNotFound, 
                           ChannelNotFound, BadInviteArgument, MessageNotFound)):
            error_types = {
                MemberNotFound: "MEMBER",
                UserNotFound: "USER",
                RoleNotFound: "ROLE",
                ChannelNotFound: "CHANNEL",
                BadInviteArgument: "INVITE",
                MessageNotFound: "MESSAGE"
            }
            return await ctx.warn(
                await self.get_text(f"system.errors.SEARCH.NOT_FOUND.{error_types[type(exc)]}", ctx,
                    argument=getattr(exc, 'argument', ''))
            )

        elif isinstance(exc, RangeError):
            label = ""
            if exc.minimum is None and exc.maximum is not None:
                label = f"no more than `{exc.maximum}`"
            elif exc.minimum is not None and exc.maximum is None:
                label = f"no less than `{exc.minimum}`"
            elif exc.maximum is not None and exc.minimum is not None:
                label = f"between `{exc.minimum}` and `{exc.maximum}`"

            translation_key = "RANGE_CHARS" if label and isinstance(exc.value, str) else "RANGE"
            return await ctx.warn(
                await self.get_text(f"system.errors.VALIDATION.{translation_key}", ctx,
                    label=label)
            )

        elif isinstance(exc, MissingPermissions):
            permissions = human_join(
                [f"`{permission}`" for permission in exc.missing_permissions],
                final="and"
            )
            return await ctx.warn(
                await self.get_text("system.errors.PERMISSIONS.USER_MISSING", ctx,
                    permissions=permissions,
                    plural="s" if len(exc.missing_permissions) > 1 else "")
            )
        
        elif isinstance(exc, NSFWChannelRequired):
            return await ctx.warn(
                await self.get_text("system.errors.PERMISSIONS.NSFW_REQUIRED", ctx)
            )

        elif isinstance(exc, CommandError):
            if isinstance(exc, (HTTPException, NotFound)) and not isinstance(exc, (CheckFailure, Forbidden)):
                if "Unknown Channel" in exc.text:
                    return
                return await ctx.warn(exc.text.capitalize())
            
            if isinstance(exc, (Forbidden, CommandInvokeError)):
                error = exc.original if isinstance(exc, CommandInvokeError) else exc
                
                if isinstance(error, Forbidden):
                    perms = ctx.guild.me.guild_permissions
                    missing_perms = []
                    
                    if not perms.manage_channels:
                        missing_perms.append('`manage_channels`')
                    if not perms.manage_roles:
                        missing_perms.append('`manage_roles`')
                        
                    if missing_perms:
                        error_msg = await self.get_text("system.errors.PERMISSIONS.BOT_MISSING", ctx,
                            permissions=', '.join(missing_perms))
                    else:
                        error_msg = await self.get_text("system.errors.PERMISSIONS.BOT_MISSING_GENERIC", ctx)
                        
                    return await ctx.warn(
                        error_msg,
                        f"Error: {str(error)}"
                    )
                    
                return await ctx.warn(str(error))

            origin = getattr(exc, "original", exc)
            with suppress(TypeError):
                if any(
                    forbidden in origin.args[-1]
                    for forbidden in (
                        "global check",
                        "check functions",
                        "Unknown Channel",
                    )
                ):
                    return

            return await ctx.warn(*origin.args)

        else:
            return await ctx.send_help(ctx.command)

    async def get_context(
        self,
        origin: Message | Interaction,
        /,
        *,
        cls=Context,
    ) -> Context:
        """
        Custom get_context method that adds additional attributes.
        """
        context = await super().get_context(origin, cls=cls)
        if context.guild: 
            context.settings = await Settings.fetch(self, context.guild)
        else:
            context.settings = None  

        return context

    async def check_global_cooldown(self, ctx: Context) -> bool:
        """Check global cooldown with monitoring."""
        if ctx.author.id in self.owner_ids:
            return True

        bucket = self.global_cooldown.get_bucket(ctx.message)
        if bucket:
            retry_after = bucket.update_rate_limit()
            if retry_after:
                raise CommandOnCooldown(bucket, retry_after, BucketType.user)

        return True

    async def process_commands(self, message: Message) -> None:
        """Process commands with monitoring."""
        if message.author.bot:
            return

        log.debug("Processing command from %s: %s", message.author, message.content)

        blacklisted = await self._check_blacklist(message.author.id)
        if blacklisted:
            log.debug("User is blacklisted")
            return

        if not await self._check_channel_permissions(message):
            log.debug("Missing channel permissions")
            return

        ctx = await self.get_context(message)
        log.debug("Context created: command=%s, valid=%s", ctx.command, ctx.valid)
        
        if self._should_discard_command(ctx, message):
            log.warning(
                "Discarded command message (ID: %s) with PartialMessageable channel: %r.",
                message.id,
                message.channel,
            )
            return

        try:
            log.debug("Invoking command %s", ctx.command)
            await self.invoke(ctx)
            log.debug("Command invocation completed")
        except Exception as e:
            log.error(f"Error in process_commands: {e}", exc_info=True)
            try:
                log.debug("Dispatching command_error event")
                self.dispatch('command_error', ctx, e)
                log.debug("Error event dispatched")
            except Exception as dispatch_error:
                log.error(f"Error dispatching command_error: {dispatch_error}", exc_info=True)

        if not ctx.valid:
            log.debug("Command not valid, dispatching message_without_command")
            self.dispatch("message_without_command", ctx)

    async def on_message(self, message: Message) -> None:
        """Handle messages with statistics tracking."""
        if message.guild and not message.author.bot:
            if not await self.check_ratelimit(message):
                return

        if message.type == MessageType.premium_guild_subscription:
            self.dispatch("member_boost", message.author)

        return await super().on_message(message)

    async def on_message_edit(self, before: Message, after: Message) -> None:
        """Handle message edits with monitoring."""
        self.dispatch("member_activity", after.channel, after.author)
        if before.content == after.content:
            return

        if after.guild and not after.author.bot:
            if not await self.check_ratelimit(after):
                return

        return await self.process_commands(after)

    async def update_voice_times(self):
        """Update voice statistics with monitoring."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                current_time = time.time()
                # stats = await self._collect_voice_statistics(current_time)
                # if stats:
                #     await self._update_voice_database(stats)
            except Exception as e:
                log.error(f"Error updating voice times: {e}")
            finally:
                await asyncio.sleep(30)

    async def update_system_stats(self):
        """Update system statistics with monitoring."""
        current_time = time.time()
        if current_time - self._last_system_check < 60:
            return

        try:
            # stats = await self._collect_system_stats(current_time)
            # self._update_system_metrics(stats)
            self._last_system_check = current_time
        except Exception as e:
            log.error(f"Failed to update system stats: {e}")

    async def _backup_task(self) -> None:
        """Run periodic backups with monitoring."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                next_run = self._calculate_next_backup_time()
                await asyncio.sleep((next_run - datetime.now(timezone.utc)).total_seconds())
                
                success = await self.dask.submit_task(
                    self.backup_manager.run_backup
                )
                
                if success:
                    log.info("8-hour backup completed successfully")
                else:
                    log.error("8-hour backup failed")
                    
            except Exception as e:
                log.error(f"Error in backup task: {e}")
                await asyncio.sleep(300)

    async def _check_blacklist(self, user_id: int) -> bool:
        """Check if user is blacklisted."""
        return cast(
            bool,
            await self.db.fetchval(
                "SELECT EXISTS(SELECT 1 FROM blacklist WHERE user_id = $1)",
                user_id,
            ),
        )

    async def _check_channel_permissions(self, message: Message) -> bool:
        """Check channel permissions."""
        if not message.guild:
            return True
            
        channel = message.channel
        permissions = channel.permissions_for(message.guild.me)
        return (
            permissions.send_messages
            and permissions.embed_links
            and permissions.attach_files
        )

    def _should_discard_command(self, ctx: Context, message: Message) -> bool:
        """Check if command should be discarded."""
        return (
            ctx.invoked_with
            and isinstance(message.channel, PartialMessageable)
            and message.channel.type != ChannelType.private
        )

    async def _collect_system_stats(self, current_time: float) -> dict:
        """Collect system statistics."""
        return {
            'timestamp': current_time,
            'cpu_percent': self.process.cpu_percent(),
            'memory_percent': self.process.memory_percent(),
            'memory_rss': self.process.memory_info().rss,
            'threads': self.process.num_threads(),
            'handles': self.process.num_handles() if hasattr(self.process, 'num_handles') else 0,
            'commands_rate': len(self._connection._commands) / (current_time - self.start_time)
        }

    def _update_system_metrics(self, stats: dict):
        """Update system metrics."""
        self.system_stats['metrics'].append(stats)
        while len(self.system_stats['metrics']) > 60:
            self.system_stats['metrics'].pop(0)

    def _load_translations(self):
        """Load all translation files."""
        langs_dir = Path("langs")
        self.translations = {}
        
        system_dir = langs_dir / "system"
        if system_dir.exists():
            for category_dir in system_dir.iterdir():
                if not category_dir.is_dir():
                    continue
                    
                for lang_file in category_dir.glob("*.json"):
                    try:
                        lang_code = lang_file.stem 
                        with lang_file.open(encoding='utf-8') as f:
                            data = json.load(f)
                            if 'system' in data:
                                self.translations.setdefault(lang_code, {})
                                self.translations[lang_code].setdefault('system', {})
                                self.translations[lang_code]['system'].update(data['system'])
                    except Exception as e:
                        log.error(f"Error loading system translation file {lang_file}: {e}")

        for category_dir in langs_dir.iterdir():
            if not category_dir.is_dir() or category_dir.name == "system":
                continue
                
            category = category_dir.name
            self.translations[category] = {}
            
            for lang_file in category_dir.glob("*.json"):
                try:
                    lang_code = lang_file.stem  
                    with lang_file.open(encoding='utf-8') as f:
                        self.translations.setdefault(lang_code, {})
                        self.translations[lang_code][category] = json.load(f)
                except Exception as e:
                    log.error(f"Error loading command translation file {lang_file}: {e}")
                
        log.info(f"Loaded translations for {len(self.translations)} categories")

    async def get_text(self, path: str, ctx=None, **kwargs) -> str:
        """
        Get translated text with parameter substitution.
        Path format: 'category.key.subkey.value' or 'system.category.key.value'
        """
        user_lang = 'en-US'  
        if ctx and hasattr(ctx, 'author'):
            try:
                user_lang = await self.db.fetchval(
                    "SELECT language FROM user_settings WHERE user_id = $1",
                    ctx.author.id
                ) or 'en-US'
            except Exception as e:
                log.error(f"Error fetching user language: {e}")

        try:
            parts = path.split('.')
            try:
                current = self.translations[user_lang]
                for part in parts[:-1]: 
                    current = current[part]
                
                if parts[-1] in current:
                    result = current[parts[-1]]
                    if isinstance(result, str):
                        return result.format(**kwargs) if kwargs else result
                    elif isinstance(result, dict):
                        if 'description' in result:
                            return result['description'].format(**kwargs) if kwargs else result['description']
            except (KeyError, AttributeError):
                pass

            try:
                current = self.translations['en-US']
                for part in parts[:-1]: 
                    current = current[part]
                
                if parts[-1] in current:
                    result = current[parts[-1]]
                    if isinstance(result, str):
                        return result.format(**kwargs) if kwargs else result
                    elif isinstance(result, dict):
                        if 'description' in result:
                            return result['description'].format(**kwargs) if kwargs else result['description']
            except (KeyError, AttributeError):
                pass

            current = self.translations
            for part in parts:
                current = current[part]
            
            if isinstance(current, str):
                return current.format(**kwargs) if kwargs else current
            elif isinstance(result, dict):
                if 'description' in result:
                    return result['description'].format(**kwargs) if kwargs else result['description']
                    
        except (KeyError, AttributeError) as e:
            log.warning(f"Missing translation: {path}")
            return f"Missing translation: {path}"

    async def process_image(self, buffer: bytes, effect_type: str, **kwargs) -> Any:
        """Process image effects using process pool."""
        try:
            return await self.dask.submit_pool_task(
                process_image_effect,
                buffer,
                effect_type,
                **kwargs
            )
        except Exception as e:
            log.error(f"Image processing error: {e}")
            raise

    async def process_data(self, process_type: str, *args, **kwargs) -> Any:
        """Process data tasks using process pool."""
        processors_map = {
            'guild_data': process_guild_data,
            'jail_permissions': process_jail_permissions,
            'add_role': process_add_role,
            'upload_bunny': process_bunny_upload
        }
        
        try:
            processor = processors_map[process_type]
            return await self.dask.submit_pool_task(
                processor,
                *args,
                **kwargs
            )
        except Exception as e:
            log.error(f"Data processing error: {e}")
            raise

    async def process_backup(self, command: str) -> Any:
        """Process backup tasks using process pool."""
        try:
            result = await self.dask.submit_pool_task(
                run_pg_dump,
                command
            )
            return result
        except Exception as e:
            log.error(f"Backup processing error: {e}")
            raise
        finally:
            if hasattr(self, 'process_pool'):
                self.process_pool._maintain_pool()

    async def _heartbeat_task(self):
        """Task to send periodic heartbeats via IPC."""
        try:
            while not self.is_closed():
                await self.ipc.send_heartbeat()
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Error in heartbeat task: {e}", exc_info=True)

    async def check_ratelimit(self, message: Message) -> bool:
        """Check if message passes rate limits."""
        if not message.guild:
            return True
            
        current_time = time.time()
        
        for duration, cooldown in self.guild_ratelimits.items():
            bucket = cooldown.get_bucket(message)
            if bucket and bucket.update_rate_limit(current_time):
                guild_violations = self.violation_tracker[message.guild.id]
                guild_violations['count'] += 1
                guild_violations['last_violation'] = current_time
                
                if guild_violations['count'] > 5:
                    guild_violations['cooldown_multiplier'] = min(
                        guild_violations['cooldown_multiplier'] * 2,
                        10 
                    )
                return False
        
        if bucket := self.channel_ratelimit.get_bucket(message):
            if bucket.update_rate_limit(current_time):
                return False
                
        return True

    async def _cleanup_violations(self):
        """Periodically clean up old violation records."""
        while not self.is_closed():
            try:
                current_time = time.time()
                for guild_id, data in list(self.violation_tracker.items()):
                    if current_time - data['last_violation'] > 3600:
                        data['count'] = max(0, data['count'] - 1)
                        data['cooldown_multiplier'] = max(1, data['cooldown_multiplier'] / 2)
                        
                    if data['count'] == 0:
                        del self.violation_tracker[guild_id]
                
                try:
                    await asyncio.wait_for(
                        self._cleanup_event.wait(),
                        timeout=300
                    )
                except asyncio.TimeoutError:
                    continue
                    
            except Exception as e:
                log.error(f"Error in violation cleanup: {e}")
                await asyncio.sleep(60)  

    def get_commands(self) -> List[commands.Command]:
        """Get list of commands for help command."""
        return self.commands

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Cog-wide check."""
        return True  

    async def _handle_reload_cog(self, data: dict) -> None:
        """Handle cog reload IPC command."""
        try:
            cog_name = data.get('cog')
            if not cog_name:
                return
                
            await self.reload_extension(f"cogs.{cog_name}")
            log.info(f"Reloaded cog {cog_name} via IPC")
            
        except Exception as e:
            log.error(f"Failed to reload cog {cog_name}: {e}")

    async def _ipc_heartbeat(self) -> None:
        """Send periodic heartbeats to IPC."""
        while not self.is_closed():
            try:
                await self.ipc.heartbeat()
            except Exception as e:
                log.error(f"Failed to send IPC heartbeat: {e}")
            await asyncio.sleep(15)  

    async def load_cogs(self) -> None:
        """Load all cogs."""
        log.info("Starting cog loading process...")
        
        cogs_dir = Path("cogs")
        
        try:
            cog_categories = [
                d for d in cogs_dir.iterdir() 
                if d.is_dir() and not d.name.startswith("_")
            ]
            
            log.info(f"Found {len(cog_categories)} cog categories")
            
            for category in cog_categories:
                try:
                    if (category / "__init__.py").exists():
                        cog_name = f"cogs.{category.name}"
                        log.info(f"Loading category: {cog_name}")
                        await self.load_extension(cog_name)
                        log.info(f"Successfully loaded category: {cog_name}")
                except Exception as e:
                    log.error(f"Failed to load category {category.name}: {e}", exc_info=True)
                    continue
            
            log.info("Finished loading cogs")
            
        except Exception as e:
            log.error(f"Error in load_cogs: {e}", exc_info=True)
            raise  
    async def start(self, token: str) -> None:
        """Start the bot."""
        try:
            log.info("Starting bot login process...")
            await super().start(token, reconnect=True)
            log.info("Bot logged in successfully")
        except Exception as e:
            log.error(f"Failed to start bot: {e}", exc_info=True)
            raise  

    async def _calculate_next_backup_time(self):
        """Calculate the next backup time."""
        try:
            current_hour = datetime.now(timezone.utc).hour
            next_hour = (current_hour + 1) % 24
            next_backup = datetime.now(timezone.utc).replace(
                hour=next_hour, 
                minute=0, 
                second=0, 
                microsecond=0
            )
            if next_backup <= datetime.now(timezone.utc):
                next_backup = next_backup + timedelta(days=1)
            return next_backup
        except Exception as e:
            log.error(f"Error calculating next backup time: {e}", exc_info=True)
            return datetime.now(timezone.utc) + timedelta(hours=1)

    async def update_voice_times(self):
        """Update voice activity times."""
        try:
            while not self.is_closed():
                # await self._collect_voice_statistics()
                await asyncio.sleep(60)
        except Exception as e:
            log.error(f"Error updating voice times: {e}", exc_info=True)

    async def _backup_task(self):
        """Run periodic backups."""
        try:
            while not self.is_closed():
                next_backup = await self._calculate_next_backup_time()
                now = datetime.now(timezone.utc)
                wait_time = (next_backup - now).total_seconds()
                await asyncio.sleep(wait_time)
                
                try:
                    await self.backup_manager.run_backup()
                except Exception as e:
                    log.error(f"Error running backup: {e}", exc_info=True)
        except Exception as e:
            log.error(f"Error in backup task: {e}", exc_info=True)

    async def on_ready(self):
        """Called when all shards are ready."""
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Connected to {len(self.guilds)} guilds")
        log.info(f"Connected to {len(self.users)} users")
        log.info(f"Using shard count: {self.shard_count}")
        
        if hasattr(self, '_ready'):
            self._ready.set()