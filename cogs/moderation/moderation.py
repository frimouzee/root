import asyncio
import re
import json
import discord
import requests
from contextlib import suppress
from datetime import timedelta
from io import BytesIO
from logging import getLogger
from textwrap import wrap
from time import perf_counter
from typing import Annotated, Callable, List, Literal, Optional, cast, Union
from zipfile import ZipFile
import humanize
from discord.ui import Button, View, button
import hashlib
import string
import random
from datetime import datetime, timedelta, timezone
import secrets

from main import Evict
from core.context import Context
from utils.conversions import (
    Duration,
    PartialAttachment,
    StrictMember,
    StrictRole,
    StrictUser,
    TouchableMember,
    GoodRole,
)

from core import FlagConverter
from .classes import ModConfig, Mod, ClearMod
from discord import (
    AuditLogAction, AuditLogEntry, Color, Embed, Emoji, File, Guild,
    HTTPException, Member, Message, NotFound, NotificationLevel,
    Object, PartialEmoji, RateLimited, Role, StageChannel,
    TextChannel, Thread, User, VoiceChannel, GuildSticker
)
from discord.abc import GuildChannel
from discord.ext.commands import (
    hybrid_command, hybrid_group, BadArgument, BucketType,
    Cog, CommandError, Greedy, MaxConcurrency, Range,
    check, command, cooldown, group, has_permissions,
    max_concurrency, parameter, flag
)
from discord.utils import MISSING, format_dt, get, utcnow
from humanfriendly import format_timespan
from humanize import precisedelta
from xxhash import xxh64_hexdigest

from opentelemetry import trace
from prometheus_client import Counter, Histogram, REGISTRY

from utils.formatter import codeblock, human_join, plural
from managers.paginator import Paginator
from utils.conversions.script import Script
from managers.paginator import Paginator
from managers.patches.permissions import donator

_mod_actions: Optional[Counter] = None
_mod_action_duration: Optional[Histogram] = None
_channel_metrics: Optional[Counter] = None
_role_metrics: Optional[Counter] = None

def unregister_if_exists(*metric_names):
    for name in metric_names:
        collectors = [
            collector for collector in REGISTRY._names_to_collectors.values()
            if hasattr(collector, '_metrics') and 
            isinstance(collector._metrics, (dict, list)) and
            (any(name in metric.name for metric in collector._metrics.values()) 
             if isinstance(collector._metrics, dict)
             else any(name in metric.name for metric in collector._metrics))
        ]
        for collector in collectors:
            try:
                REGISTRY.unregister(collector)
            except:
                pass

        for metric_name in list(REGISTRY._names_to_collectors.keys()):
            if name in metric_name:
                try:
                    REGISTRY.unregister(REGISTRY._names_to_collectors[metric_name])
                except:
                    pass

def get_mod_actions() -> Counter:
    global _mod_actions
    if _mod_actions is None:
        unregister_if_exists(
            'moderation_actions',
            'moderation_actions_total',
            'moderation_actions_created'
        )
        _mod_actions = Counter(
            'moderation_actions_total',
            'Number of moderation actions performed',
            ['action', 'guild_id']
        )
    return _mod_actions

def get_mod_action_duration() -> Histogram:
    global _mod_action_duration
    if _mod_action_duration is None:
        unregister_if_exists('moderation_action_duration_seconds')
        _mod_action_duration = Histogram(
            'moderation_action_duration_seconds',
            'Time spent processing moderation actions',
            ['action']
        )
    return _mod_action_duration

def get_channel_metrics() -> Counter:
    global _channel_metrics
    if _channel_metrics is None:
        unregister_if_exists(
            'moderation_channel_actions',
            'moderation_channel_actions_total',
            'moderation_channel_actions_created'
        )
        _channel_metrics = Counter(
            'moderation_channel_actions_total',
            'Number of channel moderation actions performed',
            ['action', 'guild_id']
        )
    return _channel_metrics

def get_role_metrics() -> Counter:
    global _role_metrics
    if _role_metrics is None:
        unregister_if_exists(
            'moderation_role_actions',
            'moderation_role_actions_total',
            'moderation_role_actions_created'
        )
        _role_metrics = Counter(
            'moderation_role_actions_total',
            'Number of role moderation actions performed',
            ['action', 'guild_id']
        )
    return _role_metrics

MOD_ACTIONS = get_mod_actions()
MOD_ACTION_DURATION = get_mod_action_duration()
CHANNEL_METRICS = get_channel_metrics()
ROLE_METRICS = get_role_metrics()

from utils.formatter import codeblock, human_join, plural
from managers.paginator import Paginator
from utils.conversions.script import Script
from managers.paginator import Paginator
from managers.patches.permissions import donator

log = getLogger("evict/mod")
MASS_ROLE_CONCURRENCY = MaxConcurrency(1, per=BucketType.guild, wait=False)

FAKE_PERMISSIONS_TABLE = "fake_permissions"
fake_permissions = [
    "administrator",
    "ban_members",
    "kick_members",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "manage_messages",
    "view_audit_log",
    "manage_webhooks",
    "manage_expressions",
    "mute_members",
    "deafen_members",
    "move_members",
    "manage_nicknames",
    "mention_everyone",
    "view_guild_insights",
    "external_emojis",
    "moderate_members",
]

class WarnActionFlags(FlagConverter):
    threshold: Range[int, 1, 50] = flag(
        description="The number of warns needed to trigger this action"
    )
    duration: Optional[Duration] = flag(
        default=None,
        description="Duration for timeout/jail/ban actions"
    )

class ModStatsView(View):
    def __init__(self, bot, moderator_id):
        super().__init__()
        self.bot = bot
        self.moderator_id = moderator_id
        self._message_cache = {}

    @button(label="Details", style=discord.ButtonStyle.primary, custom_id="modstats_details")
    async def modstats_details(self, button, interaction):
        with self.bot.tracer.start_span("mod_stats_details") as span:
            span.set_attribute("moderator_id", str(self.moderator_id))
            
            detailed_stats = await self.bot.db.fetch(
                """
                SELECT action, COUNT(*) AS count
                FROM history.moderation
                WHERE moderator_id = $1
                GROUP BY action
                """,
                self.moderator_id,
            )

            details_embed = Embed(title="Detailed Moderation Stats")
            for stat in detailed_stats:
                details_embed.add_field(
                    name=stat["action"], 
                    value=str(stat["count"]), 
                    inline=True
                )

            await interaction.response.send_message(embed=details_embed)

class Moderation(Cog):
    def __init__(self, bot: Evict):
        self.bot = bot
        self.description = "Moderation commands to make things easier."

    @property
    def actions(self) -> dict[str, str]:
        return {
            "guild_update": "updated server",
            "channel_create": "created channel",
            "channel_update": "updated channel",
            "channel_delete": "deleted channel",
            "overwrite_create": "created channel permission in",
            "overwrite_update": "updated channel permission in",
            "overwrite_delete": "deleted channel permission in",
            "kick": "kicked member",
            "member_prune": "pruned members in",
            "ban": "banned member",
            "unban": "unbanned member",
            "member_update": "updated member",
            "member_role_update": "updated member roles for",
            "member_disconnect": "disconnected member",
            "member_move": "moved member",
            "bot_add": "added bot",
            "role_create": "created role",
            "role_update": "updated role",
            "role_delete": "deleted role",
            "invite_create": "created invite",
            "invite_update": "updated invite",
            "invite_delete": "deleted invite",
            "webhook_create": "created webhook",
            "webhook_update": "updated webhook",
            "webhook_delete": "deleted webhook",
            "emoji_create": "created emoji",
            "emoji_update": "updated emoji",
            "emoji_delete": "deleted emoji",
            "message_delete": "deleted message by",
            "message_bulk_delete": "bulk deleted messages in",
            "message_pin": "pinned message by",
            "message_unpin": "unpinned message by",
            "integration_create": "created integration",
            "integration_update": "updated integration",
            "integration_delete": "deleted integration",
            "sticker_create": "created sticker",
            "sticker_update": "updated sticker",
            "sticker_delete": "deleted sticker",
            "thread_create": "created thread",
            "thread_update": "updated thread",
            "thread_delete": "deleted thread",
        }

    async def is_immune(self, ctx: Context, member: Member) -> bool:
        """
        Check if a specified member is immune to moderation actions.
        """
        immune = await self.bot.db.fetchrow(
            """
            SELECT * FROM immune 
            WHERE guild_id = $1 
            AND entity_id = $2 
            AND type = 'user'
            """,
            ctx.guild.id,
            member.id
        )

        if immune:
            return await ctx.warn(f"**{member}** is **immune** to moderation actions!")
        
        for role in member.roles:
            role_immune = await self.bot.db.fetchrow(
                """
                SELECT * FROM immune 
                WHERE guild_id = $1 
                AND role_id = $2 
                AND type = 'role'
                """,
                ctx.guild.id,
                role.id
            )
            if role_immune:
                return await ctx.warn(f"{role.mention} is **immune** to moderation actions!") 

        return False

    async def reconfigure_settings(
        self,
        guild: Guild,
        channel: TextChannel | Thread,
        new_channel: TextChannel | Thread,
    ) -> List[str]:
        """
        Update server wide settings for a channel.
        """

        reconfigured: List[str] = []
        config_map = {
            "System Channel": "system_channel",
            "Public Updates Channel": "public_updates_channel",
            "Rules Channel": "rules_channel",
            "AFK Channel": "afk_channel",
        }
        for name, attr in config_map.items():
            value = getattr(channel.guild, attr, None)
            if value == channel:
                await guild.edit(**{attr: new_channel})  # type: ignore
                reconfigured.append(name)

        for table in (
            "logging",
            "gallery",
            "timer.message",
            "timer.purge",
            "sticky_message",
            "welcome_message",
            "goodbye_message",
            "boost_message",
            ("disboard.config", "last_channel_id"),
            "level.notification",
            "commands.disabled",
            "fortnite.rotation",
            "alerts.twitch",
            "feeds.tiktok",
            "feeds.pinterest",
            "feeds.reddit",
        ):
            table_name = table if isinstance(table, str) else table[0]
            column = "channel_id" if isinstance(table, str) else table[1]
            result = await self.bot.db.execute(
                f"""
                UPDATE {table_name}
                SET {column} = $2
                WHERE {column} = $1
                """,
                channel.id,
                new_channel.id,
            )
            if result != "UPDATE 0":
                pretty_name = (
                    table_name.replace("_", " ")
                    .replace(".", " ")
                    .title()
                    .replace("Feeds Youtube", "YouTube Notifications")
                    .replace("Alerts Twitch", "Twitch Notifications")
                    .replace("Feeds Twitter", "Twitter Notifications")
                    .replace("Feeds Tiktok", "TikTok Notifications")
                    .replace("Feeds Pinterest", "Pinterest Notifications")
                    .replace("Feeds Reddit", "Subreddit Notifications")
                    .replace("Feeds Twitter", "Twitter Notifications")
                )
                reconfigured.append(pretty_name)

        return reconfigured

    def restore_key(self, guild: Guild, member: Member) -> str:
        """
        Generate a Redis key for role restoration.
        """
        return xxh64_hexdigest(f"roles:{guild.id}:{member.id}")

    def forcenick_key(self, guild: Guild, member: Member) -> str:
        """
        Generate a Redis key for forced nicknames.
        """
        return xxh64_hexdigest(f"forcenick:{guild.id}:{member.id}")
    
    def restore_key(self, guild: Guild, member: Member) -> str:
        """
        Generate a Redis key for role restoration.
        """
        return f"restore:{guild.id}:{member.id}"

    @Cog.listener()
    async def on_member_remove(self, member: Member):
        """
        Remove a member's previous roles.
        """
        if member.bot:
            return

        role_ids = [r.id for r in member.roles if r.is_assignable()]
        if role_ids:
            key = self.restore_key(member.guild, member)
            await self.bot.redis.set(key, role_ids, ex=3600)

    @Cog.listener("on_member_join")
    async def restore_roles(self, member: Member):
        """
        Restore a member's previous roles.
        """
        key = self.restore_key(member.guild, member)
        role_ids = cast(
            Optional[List[int]],
            await self.bot.redis.get(key),
        )
        if not role_ids:
            return

        roles = [
            role
            for role_id in role_ids
            if (role := member.guild.get_role(role_id)) is not None
            and role.is_assignable()
            and role not in member.roles
        ]
        if not roles:
            return

        record = await self.bot.db.fetchrow(
            """
            SELECT
                reassign_roles,
                reassign_ignore_ids
            FROM settings
            WHERE guild_id = $1
            """,
            member.guild.id,
        )
        if not record or not record["reassign_roles"]:
            return

        roles = [role for role in roles if role.id not in record["reassign_ignore_ids"]]
        if not roles:
            return

        await self.bot.redis.delete(key)
        with suppress(HTTPException):
            await member.add_roles(*roles, reason="Restoration of previous roles")
            log.info(
                "Restored %s for %s (%s) in %s (%s).",
                format(plural(len(roles)), "role"),
                member,
                member.id,
                member.guild,
                member.guild.id,
            )

    @Cog.listener("on_member_join")
    async def hardban_event(self, member: Member):
        """
        Check if a member is hard banned and ban them if they are.
        """
        hardban = await self.bot.db.fetchrow(
            """
            SELECT * 
            FROM hardban 
            WHERE guild_id = $1 
            AND user_id = $2
            """,
            member.guild.id,
            member.id,
        )
        if not hardban:
            return

        with suppress(HTTPException):
            await member.ban(reason="User is hard banned")

    @Cog.listener()
    async def on_member_unban(self, guild: Guild, user: User):
        """
        Check if a member is hard banned and ban them if they are.
        """
        hardban = await self.bot.db.fetchrow(
            """
            SELECT * 
            FROM hardban 
            WHERE guild_id = $1 
            AND user_id = $2
            """,
            guild.id,
            user.id,
        )
        if not hardban:
            return

        with suppress(HTTPException):
            await guild.ban(user, reason="User is hard banned")

    @Cog.listener("on_member_update")
    async def forcenick_event(self, before: Member, after: Member):
        """
        Force a user to have a specific nickname.
        """
        key = self.forcenick_key(before.guild, before)
        log.debug("key gen: %s", key)

        nickname = await self.bot.db.fetchval(
            """
            SELECT nickname FROM forcenick 
            WHERE guild_id = $1 
            AND user_id = $2
            """,
            before.guild.id,
            before.id,
        )
        log.debug("postgres fetched: %s", nickname)

        if not nickname or (nickname == after.display_name):
            log.debug("There is no nickname to set or nickname is already correct")
            return

        if await self.bot.redis.ratelimited(f"nick:{key}", limit=8, timespan=15):
            log.warning("Rate limit exceeded for key: %s, deleting key.", key)
            await self.bot.db.execute(
                """
                DELETE FROM forcenick 
                WHERE guild_id = $1 
                AND user_id = $2
                """,
                before.guild.id,
                before.id,
            )
            return

        with suppress(HTTPException):
            log.info(
                "Setting nickname for %s to %r in %s (%s).",
                after,
                nickname,
                after.guild,
                after.guild.id,
            )
            await after.edit(nick=nickname, reason="Forced nickname")

    @Cog.listener("on_audit_log_entry_member_update")
    async def forcenick_audit(self, entry: AuditLogEntry):
        """
        Remove forced nicknames if a user changes their nickname.
        """
        if (
            not entry.user
            or not entry.target
            or not entry.user.bot
            or entry.user == self.bot.user
        ):
            return

        elif not isinstance(entry.target, Member):
            return

        if hasattr(entry.after, "nick"):
            removed = await self.bot.db.execute(
                """
                DELETE FROM forcenick 
                WHERE guild_id = $1 
                AND user_id = $2
                """,
                entry.guild.id,
                entry.target.id,
            )

    async def do_removal(
        self,
        ctx: Context,
        amount: int,
        predicate: Callable[[Message], bool] = lambda _: True,
        *,
        before: Optional[Message] = None,
        after: Optional[Message] = None,
    ) -> List[Message]:
        """A helper function to do bulk message removal."""
        with self.bot.tracer.start_span("do_removal") as span:
            span.set_attribute("amount", amount)
            span.set_attribute("channel_id", str(ctx.channel.id))
            span.set_attribute("guild_id", str(ctx.guild.id))
            span.set_attribute("has_before", bool(before))
            span.set_attribute("has_after", bool(after))

            try:
                if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
                    span.set_attribute("error", "missing_permissions")
                    raise CommandError("I don't have permission to delete messages!")

                if not before:
                    before = ctx.message

                def check_message(message: Message) -> bool:
                    if message.created_at < (utcnow() - timedelta(weeks=2)):
                        return False
                    elif message.pinned:
                        return False
                    return predicate(message)

                with COMMAND_DURATION.labels(command="message_removal").time():
                    await quietly_delete(ctx.message)
                    
                    BATCH_SIZE = 100
                    all_messages = []
                    remaining = amount
                    
                    while remaining > 0:
                        batch_amount = min(remaining, BATCH_SIZE)
                        messages = await ctx.channel.history(
                            limit=batch_amount,
                            before=before,
                            after=after
                        ).flatten()
                        
                        if not messages:
                            break
                            
                        valid_messages = await self.bot.loop.run_in_executor(
                            None,
                            self.bot.process_pool.apply,
                            lambda msgs: [msg for msg in msgs if check_message(msg)],
                            (messages,)
                        )
                        
                        all_messages.extend(valid_messages)
                        remaining -= len(messages)
                        before = messages[-1]

                    if not all_messages:
                        span.set_attribute("error", "no_messages_found")
                        raise CommandError("No messages were found, try a larger search?")

                    deleted = 0
                    for i in range(0, len(all_messages), 100):
                        batch = all_messages[i:i + 100]
                        try:
                            await ctx.channel.delete_messages(batch)
                            deleted += len(batch)
                        except discord.HTTPException as e:
                            span.set_attribute("error", str(e))
                            span.record_exception(e)
                            continue

                    PURGE_METRICS.labels(
                        purge_type="bulk_delete",
                        guild_id=str(ctx.guild.id)
                    ).inc(deleted)
                    
                    span.set_attribute("messages_deleted", deleted)
                    return all_messages

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @hybrid_command(aliases=["bc"], examples="100")
    @has_permissions(manage_messages=True)
    async def cleanup(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove bot invocations and messages from bots.
        """
        await self.do_removal(
            ctx,
            amount,
            lambda message: (
                message.author.bot
                or message.content.startswith(
                    (ctx.clean_prefix, ",", ";", ".", "!", "$")
                )
            ),
        )

    @group(
        aliases=["prune", "rm", "c"],
        invoke_without_command=True,
        example="@x 100"
    )
    @max_concurrency(1, BucketType.channel)
    @has_permissions(manage_messages=True)
    async def purge(
        self,
        ctx: Context,
        user: Optional[
            Annotated[
                Member,
                StrictMember,
            ]
            | Annotated[
                User,
                StrictUser,
            ]
        ],
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ],
    ):
        """
        Remove messages which meet a criteria.
        """
        with self.bot.tracer.start_span("purge_command") as span:
            span.set_attribute("amount", amount)
            span.set_attribute("user_id", str(user.id) if user else None)
            span.set_attribute("channel_id", str(ctx.channel.id))
            
            with COMMAND_DURATION.labels(command="purge").time():
                await self.do_removal(ctx, amount, 
                    lambda message: message.author == user if user else True)
                PURGE_METRICS.labels(
                    purge_type="user", 
                    guild_id=str(ctx.guild.id)
                ).inc(amount)

    @purge.command(
        name="embeds",
        aliases=["embed"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_embeds(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which have embeds.
        """
        with self.bot.tracer.start_span("purge_embeds") as span:
            span.set_attribute("amount", amount)
            with COMMAND_DURATION.labels(command="purge_embeds").time():
                await self.do_removal(ctx, amount, lambda message: bool(message.embeds))
                PURGE_METRICS.labels(
                    purge_type="embeds",
                    guild_id=str(ctx.guild.id)
                ).inc(amount)

    @purge.command(
        name="files",
        aliases=["file"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_files(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which have files.
        """
        with self.bot.tracer.start_span("purge_files") as span:
            span.set_attribute("amount", amount)
            with COMMAND_DURATION.labels(command="purge_files").time():
                await self.do_removal(ctx, amount, lambda message: bool(message.attachments))
                PURGE_METRICS.labels(
                    purge_type="files",
                    guild_id=str(ctx.guild.id)
                ).inc(amount)

    @purge.command(
        name="images",
        aliases=["image"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_images(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which have images.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: bool(message.attachments or message.embeds),
        )

    @purge.command(
        name="stickers",
        aliases=["sticker"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_stickers(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which have stickers.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: bool(message.stickers),
        )

    @purge.command(
        name="voice",
        aliases=["vm"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_voice(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove voice messages.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: any(
                attachment.waveform for attachment in message.attachments
            ),
        )

    @purge.command(
        name="system",
        aliases=["sys"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_system(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove system messages.
        """

        await self.do_removal(ctx, amount, lambda message: message.is_system())

    @purge.command(
        name="mentions",
        aliases=["mention"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_mentions(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which have mentions.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: bool(message.mentions),
        )

    @purge.command(
        name="emojis",
        aliases=[
            "emotes",
            "emoji",
            "emote",
        ],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_emojis(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which have custom emojis.
        """

        custom_emoji = re.compile(r"<a?:[a-zA-Z0-9\_]+:([0-9]+)>")

        await self.do_removal(
            ctx,
            amount,
            lambda message: bool(message.content)
            and bool(custom_emoji.search(message.content)),
        )

    @purge.command(
        name="invites",
        aliases=[
            "invite",
            "inv",
        ],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_invites(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which have invites.
        """

        invite_link = re.compile(
            r"(?:https?://)?discord(?:\.gg|app\.com/invite)/[a-zA-Z0-9]+/?"
        )

        await self.do_removal(
            ctx,
            amount,
            lambda message: bool(message.content)
            and bool(invite_link.search(message.content)),
        )

    EMOJI_PATTERN = re.compile(r"<a?:[a-zA-Z0-9\_]+:([0-9]+)>")
    INVITE_PATTERN = re.compile(r"(?:https?://)?discord(?:\.gg|app\.com/invite)/[a-zA-Z0-9]+/?")
    URL_PATTERN = re.compile(r'https?://\S+', re.IGNORECASE)

    @purge.command(
        name="links",
        aliases=["link"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_links(self, ctx: Context, amount: Annotated[int, Range[int, 1, 1000]] = 100):
        """Remove messages which have links."""
        with self.bot.tracer.start_span("purge_links") as span:
            span.set_attribute("amount", amount)
            with COMMAND_DURATION.labels(command="purge_links").time():
                await self.do_removal(
                    ctx, amount,
                    lambda message: bool(message.content) and bool(self.URL_PATTERN.search(message.content))
                )
                PURGE_METRICS.labels(
                    purge_type="links",
                    guild_id=str(ctx.guild.id)
                ).inc(amount)

    @purge.command(
        name="contains",
        aliases=["contain"],
        example="xx 100"
    )
    @has_permissions(manage_messages=True)
    async def purge_contains(self, ctx: Context, 
                           substring: Annotated[str, Range[str, 2]], 
                           amount: Annotated[int, Range[int, 1, 1000]] = 100):
        """Remove messages which contain a substring."""
        with self.bot.tracer.start_span("purge_contains") as span:
            span.set_attribute("amount", amount)
            span.set_attribute("substring_length", len(substring))
            
            pattern = re.compile(re.escape(substring.lower()))
            
            with COMMAND_DURATION.labels(command="purge_contains").time():
                await self.do_removal(
                    ctx, amount,
                    lambda message: bool(message.content) and bool(pattern.search(message.content.lower()))
                )
                PURGE_METRICS.labels(
                    purge_type="contains",
                    guild_id=str(ctx.guild.id)
                ).inc(amount)

    @purge.command(
        name="startswith",
        aliases=[
            "prefix",
            "start",
            "sw",
        ],
        example="sin 100"
    )
    @has_permissions(manage_messages=True)
    async def purge_startswith(
        self,
        ctx: Context,
        substring: Annotated[
            str,
            Range[str, 3],
        ],
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which start with a substring.

        The substring must be at least 3 characters long.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: bool(message.content)
            and message.content.lower().startswith(substring.lower()),
        )

    @purge.command(
        name="endswith",
        aliases=[
            "suffix",
            "end",
            "ew",
        ],
        example="sin 100"
    )
    @has_permissions(manage_messages=True)
    async def purge_endswith(
        self,
        ctx: Context,
        substring: Annotated[
            str,
            Range[str, 3],
        ],
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which end with a substring.

        The substring must be at least 3 characters long.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: bool(message.content)
            and message.content.lower().endswith(substring.lower()),
        )

    @purge.command(
        name="humans",
        aliases=["human"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_humans(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which are not from a bot.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: not message.author.bot,
        )

    @purge.command(
        name="bots",
        aliases=["bot"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_bots(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which are from a bot.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: message.author.bot,
        )

    @purge.command(
        name="webhooks",
        aliases=["webhook"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    async def purge_webhooks(
        self,
        ctx: Context,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 100,
    ):
        """
        Remove messages which are from a webhook.
        """

        await self.do_removal(
            ctx,
            amount,
            lambda message: bool(message.webhook_id),
        )

    @purge.command(name="before", example="1320937696968970281")
    @has_permissions(manage_messages=True)
    async def purge_before(
        self,
        ctx: Context,
        message: Optional[Message],
    ):
        """
        Remove messages before a target message.
        """

        message = message or ctx.replied_message
        if not message:
            return await ctx.send_help(ctx.command)

        if message.channel != ctx.channel:
            return await ctx.send_help(ctx.command)

        await self.do_removal(
            ctx,
            300,
            before=message,
        )

    @purge.command(
        name="after",
        aliases=["upto", "up"],
        example="1320937696968970281"
    )
    @has_permissions(manage_messages=True)
    async def purge_after(
        self,
        ctx: Context,
        message: Optional[Message],
    ):
        """
        Remove messages after a target message.
        """

        message = message or ctx.replied_message
        if not message:
            return await ctx.send_help(ctx.command)

        if message.channel != ctx.channel:
            return await ctx.send_help(ctx.command)

        await self.do_removal(
            ctx,
            300,
            after=message,
        )

    @purge.command(name="between", example="1320937691063517264 1320937696968970281")
    @has_permissions(manage_messages=True)
    async def purge_between(
        self,
        ctx: Context,
        start: Message,
        finish: Message,
    ):
        """
        Remove messages between two messages.
        """

        if start.channel != ctx.channel or finish.channel != ctx.channel:
            return await ctx.send_help(ctx.command)

        await self.do_removal(
            ctx,
            2000,
            after=start,
            before=finish,
        )

    @purge.command(
        name="except",
        aliases=[
            "besides",
            "schizo",
        ],
        example="@x 100"
    )
    @has_permissions(manage_messages=True)
    async def purge_except(
        self,
        ctx: Context,
        member: Member,
        amount: Annotated[
            int,
            Range[int, 1, 1000],
        ] = 500,
    ):
        """
        Remove messages not sent by a member.
        """

        await self.do_removal(ctx, amount, lambda message: message.author != member)

    @purge.command(
        name="reactions",
        aliases=["reaction", "react"],
        example="100"
    )
    @has_permissions(manage_messages=True)
    @max_concurrency(1, BucketType.channel)
    async def purge_reactions(self, ctx: Context, amount: Annotated[int, Range[int, 1, 1000]] = 100):
        """Remove reactions from messages."""
        with self.bot.tracer.start_span("purge_reactions") as span:
            span.set_attribute("amount", amount)
            total_removed = 0
            
            with COMMAND_DURATION.labels(command="purge_reactions").time():
                async with ctx.typing():
                    batch_size = 25
                    messages = []
                    async for message in ctx.channel.history(limit=amount, before=ctx.message):
                        if message.reactions:
                            messages.append(message)
                            if len(messages) >= batch_size:
                                await asyncio.gather(*[msg.clear_reactions() for msg in messages])
                                total_removed += sum(reaction.count for msg in messages 
                                                  for reaction in msg.reactions)
                                messages = []
                    
                    if messages:
                        await asyncio.gather(*[msg.clear_reactions() for msg in messages])
                        total_removed += sum(reaction.count for msg in messages 
                                          for reaction in msg.reactions)

            span.set_attribute("reactions_removed", total_removed)
            PURGE_METRICS.labels(
                purge_type="reactions",
                guild_id=str(ctx.guild.id)
            ).inc(total_removed)
            
            return await ctx.approve(
                await ctx.bot.get_text("moderation.purge.reactions.SUCCESS", ctx, count=total_removed)
            )

    async def do_mass_role(
        self,
        ctx: Context,
        role: Role,
        predicate: Callable[[Member], bool] = lambda _: True,
        *,
        action: Literal["add", "remove"] = "add",
        failure_message: Optional[str] = None,
    ) -> Message:
        """A helper method to mass add or remove a role from members."""
        with self.bot.tracer.start_span("mass_role_action") as span:
            span.set_attribute("role_id", str(role.id))
            span.set_attribute("action", action)
            span.set_attribute("guild_id", str(ctx.guild.id))

            try:
                if not failure_message:
                    failure_message = await ctx.bot.get_text(
                        f"moderation.role.mass.{'ALREADY_HAS' if action == 'add' else 'NOBODY_HAS'}", 
                        ctx,
                        role=role.mention
                    )

                if not ctx.guild.chunked:
                    await ctx.guild.chunk(cache=True)

                def filter_members(members, role_ids):
                    return [
                        member for member in members
                        if predicate(member) and 
                        ((role.id in role_ids) != (action == "add"))
                    ]

                with ROLE_ACTION_DURATION.labels(action=f"mass_{action}").time():
                    member_role_map = {
                        member.id: [r.id for r in member.roles]
                        for member in ctx.guild.members
                    }
                    
                    filtered_members = await self.bot.loop.run_in_executor(
                        None,
                        self.bot.process_pool.apply,
                        filter_members,
                        (ctx.guild.members, [member_role_map[m.id] for m in ctx.guild.members])
                    )

                    members = []
                    for member in filtered_members:
                        try:
                            await TouchableMember(allow_author=True).check(ctx, member)
                            members.append(member)
                        except BadArgument:
                            continue

                    if not members:
                        span.set_attribute("error", "no_valid_members")
                        return await ctx.warn(failure_message)

                    word = "to" if action == "add" else "from"
                    pending_message = await ctx.neutral(
                        await ctx.bot.get_text(
                            "moderation.role.mass.STARTING",
                            ctx,
                            action=action,
                            role=role.mention,
                            word=word,
                            count=len(members)
                        ),
                        await ctx.bot.get_text(
                            "moderation.role.mass.DURATION",
                            ctx,
                            timespan=format_timespan(len(members))
                        ),
                    )

                    failed: List[Member] = []
                    try:
                        async with ctx.typing():
                            batch_size = 10
                            for i in range(0, len(members), batch_size):
                                batch = members[i:i + batch_size]
                                tasks = []
                                
                                for member in batch:
                                    if action == "add":
                                        tasks.append(member.add_roles(
                                            role,
                                            reason=f"Mass role {action} by {ctx.author}",
                                            atomic=True
                                        ))
                                    else:
                                        tasks.append(member.remove_roles(
                                            role,
                                            reason=f"Mass role {action} by {ctx.author}",
                                            atomic=True
                                        ))

                                results = await asyncio.gather(*tasks, return_exceptions=True)
                                
                                for member, result in zip(batch, results):
                                    if isinstance(result, Exception):
                                        failed.append(member)
                                        if len(failed) >= 10:
                                            break

                    finally:
                        await quietly_delete(pending_message)

                    success_count = len(members) - len(failed)
                    ROLE_METRICS.labels(
                        action=f"mass_{action}",
                        guild_id=str(ctx.guild.id)
                    ).inc(success_count)

                    span.set_attribute("members_processed", len(members))
                    span.set_attribute("members_failed", len(failed))

                    result = [
                        await ctx.bot.get_text(
                            "moderation.role.mass.SUCCESS",
                            ctx,
                            action_past=action.title()[:5]+"ed",
                            role=role.mention,
                            word=word,
                            count=success_count
                        )
                    ]
                    if failed:
                        result.append(
                            await ctx.bot.get_text(
                                "moderation.role.mass.FAILED",
                                ctx,
                                action_short=action[:5],
                                role=role.mention,
                                word=word,
                                count=len(failed),
                                failed_members=', '.join(member.mention for member in failed)
                            )
                        )

                    return await ctx.approve(*result)

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @group(aliases=["r"], invoke_without_command=True, example="@x @member")
    @has_permissions(manage_roles=True)
    async def role(self, ctx: Context, member: Annotated[Member, TouchableMember(allow_author=True)], *, role: GoodRole) -> Message:
        """Add or remove a role from a member."""
        with self.bot.tracer.start_span("role_toggle") as span:
            span.set_attribute("member_id", str(member.id))
            span.set_attribute("role_id", str(role.id))
            
            with ROLE_ACTION_DURATION.labels(action="toggle").time():
                if role in member.roles:
                    return await ctx.invoke(self.role_remove, member=member, role=role)
                return await ctx.invoke(self.role_add, member=member, role=role)

    @role.command(name="add", aliases=["grant"], example="@x @member")
    @has_permissions(manage_roles=True)
    async def role_add(self, ctx: Context, member: Annotated[Member, TouchableMember(allow_author=True)], *, role: GoodRole) -> Message:
        """Add a role to a member."""
        with self.bot.tracer.start_span("role_add") as span:
            span.set_attribute("member_id", str(member.id))
            span.set_attribute("role_id", str(role.id))
            
            try:
                with ROLE_ACTION_DURATION.labels(action="add").time():
                    if role in member.roles:
                        return await ctx.warn(
                            await ctx.bot.get_text(
                                "moderation.role.add.ALREADY_HAS",
                                ctx,
                                member=member.mention,
                                role=role.mention
                            )
                        )

                    if await self.is_immune(ctx, member):
                        return

                    reason = f"Added by {ctx.author.name} ({ctx.author.id})"
                    await member.add_roles(role, reason=reason, atomic=True)
                    
                    try:
                        await ModConfig.sendlogs(self.bot, "role add", ctx.author, member, reason)
                    except Exception as e:
                        span.record_exception(e)

                    ROLE_METRICS.labels(action="add", guild_id=str(ctx.guild.id)).inc()

                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.role.add.SUCCESS",
                            ctx,
                            role=role.mention,
                            member=member.mention
                        )
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="remove", aliases=["rm"], example="@x @staff")
    @has_permissions(manage_roles=True)
    async def role_remove(self, ctx: Context, member: Annotated[Member, TouchableMember(allow_author=True)], *, role: GoodRole) -> Message:
        """Remove a role from a member."""
        with self.bot.tracer.start_span("role_remove") as span:
            span.set_attribute("member_id", str(member.id))
            span.set_attribute("role_id", str(role.id))
            
            try:
                with ROLE_ACTION_DURATION.labels(action="remove").time():
                    if role not in member.roles:
                        return await ctx.warn(
                            await ctx.bot.get_text(
                                "moderation.role.remove.DOESNT_HAVE",
                                ctx,
                                member=member.mention,
                                role=role.mention
                            )
                        )

                    if await self.is_immune(ctx, member):
                        return

                    reason = f"Removed by {ctx.author.name} ({ctx.author.id})"
                    await member.remove_roles(role, reason=reason, atomic=True)

                    try:
                        await ModConfig.sendlogs(self.bot, "role remove", ctx.author, member, reason)
                    except Exception as e:
                        span.record_exception(e)

                    ROLE_METRICS.labels(action="remove", guild_id=str(ctx.guild.id)).inc()

                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.role.remove.SUCCESS",
                            ctx,
                            role=role.mention,
                            member=member.mention
                        )
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="restore", aliases=["re"], example="@x")
    @has_permissions(manage_roles=True)
    async def role_restore(self, ctx: Context, member: Annotated[Member, TouchableMember]) -> Message:
        """Restore a member's previous roles."""
        with self.bot.tracer.start_span("role_restore") as span:
            span.set_attribute("member_id", str(member.id))
            
            try:
                with ROLE_ACTION_DURATION.labels(action="restore").time():
                    key = self.restore_key(ctx.guild, member)
                    role_ids = cast(Optional[List[int]], await self.bot.redis.getdel(key))
                    
                    if not role_ids:
                        return await ctx.warn(
                            await ctx.bot.get_text(
                                "moderation.role.restore.NO_ROLES",
                                ctx,
                                member=member.mention
                            )
                        )

                    def validate_roles(role_ids, guild_roles, member_roles):
                        return [
                            role_id for role_id in role_ids
                            if role_id in guild_roles
                            and guild_roles[role_id].is_assignable()
                            and role_id not in member_roles
                        ]

                    guild_roles = {r.id: r for r in ctx.guild.roles}
                    member_roles = {r.id for r in member.roles}
                    
                    valid_role_ids = await self.bot.loop.run_in_executor(
                        None,
                        self.bot.process_pool.apply,
                        validate_roles,
                        (role_ids, guild_roles, member_roles)
                    )

                    roles = [guild_roles[role_id] for role_id in valid_role_ids]
                    roles = [r for r in roles if await StrictRole().check(ctx, r)]

                    if not roles:
                        return await ctx.warn(
                            await ctx.bot.get_text(
                                "moderation.role.restore.NO_PREVIOUS",
                                ctx,
                                member=member.mention
                            )
                        )

                    try:
                        await ModConfig.sendlogs(self.bot, "role restore", ctx.author, member, "No reason provided")
                    except Exception as e:
                        span.record_exception(e)

                    reason = f"Restoration of previous roles by {ctx.author.name} ({ctx.author.id})"
                    await member.add_roles(*roles, reason=reason, atomic=True)

                    ROLE_METRICS.labels(action="restore", guild_id=str(ctx.guild.id)).inc(len(roles))

                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.role.restore.SUCCESS",
                            ctx,
                            roles=human_join([role.mention for role in roles], final='and'),
                            member=member.mention
                        )
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="create", aliases=["make"], example="#ff0000 true staff")
    @has_permissions(manage_roles=True)
    async def role_create(self, ctx: Context, color: Optional[Color] = None, 
                         hoist: Optional[bool] = None, *, name: Range[str, 1, 100]) -> Message:
        """Create a role."""
        with self.bot.tracer.start_span("role_create") as span:
            span.set_attribute("role_name", name)
            
            try:
                with ROLE_ACTION_DURATION.labels(action="create").time():
                    if len(ctx.guild.roles) >= 250:
                        return await ctx.warn(
                            await ctx.bot.get_text("moderation.role.create.TOO_MANY", ctx)
                        )

                    config = await Settings.fetch(self.bot, ctx.guild)
                    if (config and config.role and not config.is_whitelisted(ctx.author) 
                        and await config.check_threshold(self.bot, ctx.author, "role")):
                        await strip_roles(ctx.author, dangerous=True, reason="Antinuke role threshold reached")
                        return await ctx.warn(
                            await ctx.bot.get_text("moderation.role.create.ANTINUKE.THRESHOLD", ctx),
                            await ctx.bot.get_text("moderation.role.create.ANTINUKE.REVOKED", ctx)
                        )

                    reason = f"Created by {ctx.author.name} ({ctx.author.id})"
                    role = await ctx.guild.create_role(
                        name=name,
                        color=color or Color.default(),
                        hoist=hoist or False,
                        reason=reason
                    )

                    ROLE_METRICS.labels(action="create", guild_id=str(ctx.guild.id)).inc()

                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.role.create.SUCCESS",
                            ctx,
                            role=role.mention
                        )
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="delete", aliases=["del"], example="@bots")
    @has_permissions(manage_roles=True)
    async def role_delete(self, ctx: Context, *, role: Annotated[Role, StrictRole]) -> Optional[Message]:
        """Delete a role."""
        with self.bot.tracer.start_span("role_delete") as span:
            span.set_attribute("role_id", str(role.id))
            
            try:
                with ROLE_ACTION_DURATION.labels(action="delete").time():
                    if role.members:
                        await ctx.prompt(
                            await ctx.bot.get_text(
                                "moderation.role.delete.CONFIRM",
                                ctx,
                                role=role.mention,
                                count=len(role.members)
                            )
                        )

                    config = await Settings.fetch(self.bot, ctx.guild)
                    if (config and config.role and not config.is_whitelisted(ctx.author) 
                        and await config.check_threshold(self.bot, ctx.author, "role")):
                        await strip_roles(ctx.author, dangerous=True, reason="Antinuke role threshold reached")
                        return await ctx.warn(
                            await ctx.bot.get_text("moderation.role.delete.ANTINUKE.THRESHOLD", ctx),
                            await ctx.bot.get_text("moderation.role.delete.ANTINUKE.REVOKED", ctx)
                        )

                    await role.delete(reason=f"Deleted by {ctx.author.name} ({ctx.author.id})")
                    ROLE_METRICS.labels(action="delete", guild_id=str(ctx.guild.id)).inc()
                    return await ctx.check()

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="color", aliases=["colour"], example="@member #ff0000")
    @has_permissions(manage_roles=True)
    async def role_color(self, ctx: Context, role: Annotated[Role, StrictRole(check_integrated=False)], *, color: Color) -> Message:
        """Change a role's color."""
        with self.bot.tracer.start_span("role_color") as span:
            span.set_attribute("role_id", str(role.id))
            span.set_attribute("color", str(color))
            
            try:
                with ROLE_ACTION_DURATION.labels(action="color").time():
                    reason = f"Changed by {ctx.author.name} ({ctx.author.id})"
                    await role.edit(color=color, reason=reason)
                    
                    ROLE_METRICS.labels(
                        action="color",
                        guild_id=str(ctx.guild.id)
                    ).inc()
                    
                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.role.color.SUCCESS",
                            ctx,
                            role=role.mention,
                            color=color
                        )
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="rename", aliases=["name"], example="@member humans")
    @has_permissions(manage_roles=True)
    async def role_rename(self, ctx: Context, role: Annotated[Role, StrictRole(check_integrated=False)], *, name: Range[str, 1, 100]) -> None:
        """Change a role's name."""
        with self.bot.tracer.start_span("role_rename") as span:
            span.set_attribute("role_id", str(role.id))
            span.set_attribute("new_name", name)
            
            try:
                with ROLE_ACTION_DURATION.labels(action="rename").time():
                    reason = f"Changed by {ctx.author.name} ({ctx.author.id})"
                    await role.edit(name=name, reason=reason)
                    
                    ROLE_METRICS.labels(
                        action="rename",
                        guild_id=str(ctx.guild.id)
                    ).inc()
                    
                    return await ctx.check()

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="hoist", example="@staff")
    @has_permissions(manage_roles=True)
    async def role_hoist(self, ctx: Context, *, role: Annotated[Role, StrictRole(check_integrated=False)]) -> Message:
        """Toggle if a role should appear in the sidebar."""
        with self.bot.tracer.start_span("role_hoist") as span:
            span.set_attribute("role_id", str(role.id))
            
            try:
                with ROLE_ACTION_DURATION.labels(action="hoist").time():
                    reason = f"Changed by {ctx.author.name} ({ctx.author.id})"
                    await role.edit(hoist=not role.hoist, reason=reason)
                    
                    ROLE_METRICS.labels(
                        action="hoist",
                        guild_id=str(ctx.guild.id)
                    ).inc()
                    
                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.role.hoist.SUCCESS",
                            ctx,
                            role=role.mention,
                            status='now' if role.hoist else 'no longer'
                        )
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="mentionable", example="@staff")
    @has_permissions(manage_roles=True)
    async def role_mentionable(self, ctx: Context, *, role: Annotated[Role, StrictRole(check_integrated=False)]) -> Message:
        """Toggle if a role should be mentionable."""
        with self.bot.tracer.start_span("role_mentionable") as span:
            span.set_attribute("role_id", str(role.id))
            
            try:
                with ROLE_ACTION_DURATION.labels(action="mentionable").time():
                    reason = f"Changed by {ctx.author.name} ({ctx.author.id})"
                    await role.edit(mentionable=not role.mentionable, reason=reason)
                    
                    ROLE_METRICS.labels(
                        action="mentionable",
                        guild_id=str(ctx.guild.id)
                    ).inc()
                    
                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.role.mentionable.SUCCESS",
                            ctx,
                            role=role.mention,
                            status='now' if role.mentionable else 'no longer'
                        )
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.command(name="icon", example="@member https://example.com/image.png")
    @has_permissions(manage_roles=True)
    async def role_icon(self, ctx: Context, role: Annotated[Role, StrictRole(check_integrated=False)],
                       icon: PartialEmoji | PartialAttachment | str = parameter(default=PartialAttachment.fallback)) -> Message:
        """Change a role's icon."""
        with self.bot.tracer.start_span("role_icon") as span:
            span.set_attribute("role_id", str(role.id))
            
            try:
                with ROLE_ACTION_DURATION.labels(action="icon").time():
                    if ctx.guild.premium_tier < 2:
                        return await ctx.warn("Role icons are only available for **level 2** boosted servers!")

                    reason = f"Changed by {ctx.author.name} ({ctx.author.id})"
                    if isinstance(icon, str) and icon in ("none", "remove", "delete"):
                        if not role.display_icon:
                            return await ctx.warn(f"{role.mention} doesn't have an icon!")

                        await role.edit(display_icon=None, reason=reason)
                        ROLE_METRICS.labels(action="icon_remove", guild_id=str(ctx.guild.id)).inc()
                        return await ctx.approve(f"Removed {role.mention}'s icon")

                    buffer: bytes | str
                    processing: Optional[Message] = None

                    if isinstance(icon, str):
                        buffer = icon
                    elif isinstance(icon, PartialEmoji):
                        buffer = await icon.read()
                        if icon.animated:
                            processing = await ctx.neutral("Converting animated emoji to a static image...")
                            buffer = await self.bot.loop.run_in_executor(
                                None,
                                convert_image,
                                buffer,
                                "png"
                            )
                    elif icon.is_gif():
                        processing = await ctx.neutral("Converting GIF to a static image...")
                        buffer = await self.bot.loop.run_in_executor(
                            None,
                            convert_image,
                            icon.buffer,
                            "png"
                        )
                    elif not icon.is_image():
                        return await ctx.warn("The attachment must be an image!")
                    else:
                        buffer = icon.buffer

                    if processing:
                        await processing.delete(delay=0.5)

                    await role.edit(display_icon=buffer, reason=reason)
                    
                    ROLE_METRICS.labels(
                        action="icon_set",
                        guild_id=str(ctx.guild.id)
                    ).inc()
                    
                    return await ctx.approve(
                        f"Changed {role.mention}'s icon to "
                        + (f"[**image**]({icon.url})" if isinstance(icon, PartialAttachment) else f"**{icon}**")
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @role.group(
        name="all",
        aliases=["everyone"],
        invoke_without_command=True,
        max_concurrency=MASS_ROLE_CONCURRENCY,
        example="@member"
    )
    @has_permissions(manage_roles=True)
    async def role_all(
        self,
        ctx: Context,
        *,
        role: Annotated[
            Role,
            StrictRole,
        ],
    ) -> Message:
        """
        Add a role to everyone.
        """

        return await self.do_mass_role(ctx, role)

    @role_all.command(
        name="remove",
        aliases=["rm"],
        max_concurrency=MASS_ROLE_CONCURRENCY,
        example="@admin"
    )
    @has_permissions(manage_roles=True)
    async def role_all_remove(
        self,
        ctx: Context,
        *,
        role: Annotated[
            Role,
            StrictRole,
        ],
    ) -> Message:
        """
        Remove a role from everyone.
        """

        return await self.do_mass_role(
            ctx,
            role,
            action="remove",
        )

    @role.group(
        name="humans",
        invoke_without_command=True,
        max_concurrency=MASS_ROLE_CONCURRENCY,
        example="@member"
    )
    @has_permissions(manage_roles=True)
    async def role_humans(
        self,
        ctx: Context,
        *,
        role: Annotated[
            Role,
            StrictRole,
        ],
    ) -> Message:
        """
        Add a role to all humans.
        """

        return await self.do_mass_role(
            ctx,
            role,
            lambda member: not member.bot,
        )

    @role_humans.command(
        name="remove",
        aliases=["rm"],
        max_concurrency=MASS_ROLE_CONCURRENCY,
        example="@bots"
    )
    @has_permissions(manage_roles=True)
    async def role_humans_remove(
        self,
        ctx: Context,
        *,
        role: Annotated[
            Role,
            StrictRole,
        ],
    ) -> Message:
        """
        Remove a role from all humans.
        """

        return await self.do_mass_role(
            ctx,
            role,
            lambda member: not member.bot,
            action="remove",
        )

    @role.group(
        name="bots",
        invoke_without_command=True,
        max_concurrency=MASS_ROLE_CONCURRENCY,
        example="@bots"
    )
    @has_permissions(manage_roles=True)
    async def role_bots(
        self,
        ctx: Context,
        *,
        role: Annotated[
            Role,
            StrictRole,
        ],
    ) -> Message:
        """
        Add a role to all bots.
        """

        return await self.do_mass_role(
            ctx,
            role,
            lambda member: member.bot,
        )

    @role_bots.command(
        name="remove",
        aliases=["rm"],
        max_concurrency=MASS_ROLE_CONCURRENCY,
        example="@member"
    )
    @has_permissions(manage_roles=True)
    async def role_bots_remove(
        self,
        ctx: Context,
        *,
        role: Annotated[
            Role,
            StrictRole,
        ],
    ) -> Message:
        """
        Remove a role from all bots.
        """

        return await self.do_mass_role(
            ctx,
            role,
            lambda member: member.bot,
            action="remove",
        )

    @role.group(
        name="has",
        aliases=["with", "in"],
        invoke_without_command=True,
        max_concurrency=MASS_ROLE_CONCURRENCY,
        example="@muted @member"
    )
    @has_permissions(manage_roles=True)
    async def role_has(
        self,
        ctx: Context,
        role: Annotated[
            Role,
            StrictRole(
                check_integrated=False,
            ),
        ],
        *,
        assign_role: Annotated[
            Role,
            StrictRole,
        ],
    ) -> Message:
        """
        Add a role to everyone with a role.
        """

        return await self.do_mass_role(
            ctx,
            assign_role,
            lambda member: role in member.roles,
        )

    @role_has.command(
        name="remove",
        aliases=["rm"],
        max_concurrency=MASS_ROLE_CONCURRENCY,
        example="@muted @member"
    )
    @has_permissions(manage_roles=True)
    async def role_has_remove(
        self,
        ctx: Context,
        role: Annotated[
            Role,
            StrictRole(
                check_integrated=False,
            ),
        ],
        *,
        remove_role: Annotated[
            Role,
            StrictRole,
        ],
    ) -> Message:
        """
        Remove a role from everyone with a role.
        """

        return await self.do_mass_role(
            ctx,
            remove_role,
            lambda member: role in member.roles,
            action="remove",
        )

    @hybrid_group(
        aliases=["lock"],
        invoke_without_command=True,
        example="#general idk why",
    )
    @has_permissions(manage_roles=True)
    async def lockdown(
        self,
        ctx: Context,
        channel: Optional[TextChannel | Thread],
        *,
        reason: str = "No reason provided",
    ) -> Message:
        """Prevent members from sending messages."""
        channel = cast(TextChannel | Thread, channel or ctx.channel)
        lock_role = ctx.guild.get_role(ctx.settings.lock_role_id) or ctx.guild.default_role

        if (
            isinstance(channel, Thread)
            and channel.locked
            or isinstance(channel, TextChannel)
            and channel.overwrites_for(lock_role).send_messages is False 
        ):
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.lockdown.ALREADY_LOCKED",
                    ctx,
                    channel=channel.mention
                )
            )

        if isinstance(channel, Thread):
            await channel.edit(
                locked=True,
                reason=f"{ctx.author.name} / {reason}",
            )
        else:
            overwrite = channel.overwrites_for(lock_role)
            overwrite.send_messages = False
            await channel.set_permissions(
                lock_role,
                overwrite=overwrite,
                reason=f"{ctx.author.name} / {reason}",
            )

        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.lockdown.SUCCESS",
                ctx,
                channel=channel.mention
            )
        )

    @lockdown.command(name="all", example="idk why")
    @has_permissions(manage_roles=True)
    @max_concurrency(1, BucketType.guild)
    @cooldown(1, 30, BucketType.guild)
    async def lockdown_all(self, ctx: Context, *, reason: str = "No reason provided") -> Message:
        """Prevent members from sending messages in all channels."""
        with self.bot.tracer.start_span("lockdown_all") as span:
            try:
                if not ctx.settings.lock_ignore:
                    await ctx.prompt(
                        await ctx.bot.get_text("moderation.lockdown.all.CONFIRM.TITLE", ctx),
                        await ctx.bot.get_text("moderation.lockdown.all.CONFIRM.WARNING", ctx),
                    )

                initial_message = await ctx.neutral(
                    await ctx.bot.get_text("moderation.lockdown.all.PROGRESS", ctx)
                )

                channels_to_lock = [
                    channel for channel in ctx.guild.text_channels
                    if channel.overwrites_for(ctx.settings.lock_role).send_messages is not False
                    and channel not in ctx.settings.lock_ignore
                ]
                
                batch_size = 10
                for i in range(0, len(channels_to_lock), batch_size):
                    batch = channels_to_lock[i:i + batch_size]
                    tasks = []
                    
                    for channel in batch:
                        overwrite = channel.overwrites_for(ctx.settings.lock_role)
                        overwrite.send_messages = False
                        tasks.append(channel.set_permissions(
                            ctx.settings.lock_role,
                            overwrite=overwrite,
                            reason=f"{ctx.author.name} / {reason} (SERVER LOCKDOWN)",
                        ))
                    
                    await asyncio.gather(*tasks, return_exceptions=True)

                duration = perf_counter() - start
                CHANNEL_METRICS.labels(
                    action="lockdown_all",
                    guild_id=str(ctx.guild.id)
                ).inc(len(channels_to_lock))
                
                span.set_attribute("channels_locked", len(channels_to_lock))
                span.set_attribute("duration_seconds", duration)
                
                return await ctx.approve(
                    await ctx.bot.get_text(
                        "moderation.lockdown.all.SUCCESS",
                        ctx,
                        count=len(channels_to_lock),
                        duration=duration
                    ),
                    patch=initial_message,
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @lockdown.command(name="role", example="@Muted")
    @has_permissions(manage_roles=True)
    async def lockdown_role(self, ctx: Context, *, role: Annotated[Role, StrictRole(check_integrated=False, allow_default=True)]) -> Message:
        """Set the role which will be locked from sending messages."""
        with self.bot.tracer.start_span("lockdown_role_set") as span:
            span.set_attribute("role_id", str(role.id))
            
            try:
                with CHANNEL_METRICS.labels(action="lockdown_role_set").time():
                    await ctx.settings.update(lock_role_id=role.id)
                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.lockdown.role.SUCCESS",
                            ctx,
                            role=role.mention
                        )
                    )
            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @lockdown.group(name="ignore", aliases=["exempt"], invoke_without_command=True, example="#announcements")
    @has_permissions(manage_roles=True)
    async def lockdown_ignore(self, ctx: Context, *, channel: TextChannel) -> Message:
        """Ignore a channel from being unintentionally locked."""
        with self.bot.tracer.start_span("lockdown_ignore_add") as span:
            span.set_attribute("channel_id", str(channel.id))
            
            try:
                if channel in ctx.settings.lock_ignore:
                    return await ctx.warn(
                        await ctx.bot.get_text(
                            "moderation.lockdown.ignore.ALREADY_IGNORED",
                            ctx,
                            channel=channel.mention
                        )
                    )

                with CHANNEL_METRICS.labels(action="lockdown_ignore_add").time():
                    ctx.settings.lock_ignore_ids.append(channel.id)
                    await ctx.settings.update()
                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.lockdown.ignore.SUCCESS",
                            ctx,
                            channel=channel.mention
                        )
                    )
                    
            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @lockdown_ignore.command(
        name="remove",
        aliases=["delete", "del", "rm"],
        example="#announcements"
    )
    @has_permissions(manage_roles=True)
    async def lockdown_ignore_remove(
        self,
        ctx: Context,
        *,
        channel: TextChannel,
    ) -> Message:
        """Remove a channel from being ignored."""
        if channel not in ctx.settings.lock_ignore:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.lockdown.ignore.remove.NOT_IGNORED",
                    ctx,
                    channel=channel.mention
                )
            )

        ctx.settings.lock_ignore_ids.remove(channel.id)
        await ctx.settings.update()
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.lockdown.ignore.remove.SUCCESS",
                ctx,
                channel=channel.mention
            )
        )

    @lockdown_ignore.command(
        name="list",
        aliases=["ls"],
    )
    @has_permissions(manage_roles=True)
    async def lockdown_ignore_list(self, ctx: Context) -> Message:
        """View all channels being ignored."""
        if not ctx.settings.lock_ignore:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.lockdown.ignore.list.NO_CHANNELS", ctx)
            )

        paginator = Paginator(
            ctx,
            entries=[
                f"{channel.mention} (`{channel.id}`)"
                for channel in ctx.settings.lock_ignore
            ],
            embed=Embed(
                title=await ctx.bot.get_text("moderation.lockdown.ignore.list.TITLE", ctx)
            ),
        )
        return await paginator.start()

    @hybrid_group(
        aliases=["unlock"],
        invoke_without_command=True,
        example="#general idk why",
    )
    @has_permissions(manage_roles=True)
    async def unlockdown(
        self,
        ctx: Context,
        channel: Optional[TextChannel | Thread],
        *,
        reason: str = "No reason provided",
    ) -> Message:
        """Allow members to send messages."""
        channel = cast(TextChannel | Thread, channel or ctx.channel)
        if not isinstance(channel, (TextChannel | Thread)):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.unlockdown.CHANNEL_TYPE", ctx)
            )

        if (
            isinstance(channel, Thread)
            and not channel.locked
            or isinstance(channel, TextChannel)
            and channel.overwrites_for(ctx.settings.lock_role).send_messages is True
        ):
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.unlockdown.ALREADY_UNLOCKED",
                    ctx,
                    channel=channel.mention
                )
            )

        if isinstance(channel, Thread):
            await channel.edit(
                locked=False,
                reason=f"{ctx.author.name} / {reason}",
            )
        else:
            overwrite = channel.overwrites_for(ctx.settings.lock_role)
            overwrite.send_messages = True
            await channel.set_permissions(
                ctx.settings.lock_role,
                overwrite=overwrite,
                reason=f"{ctx.author.name} / {reason}",
            )

        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.unlockdown.SUCCESS",
                ctx,
                channel=channel.mention
            )
        )

    @unlockdown.command(name="all", example="idk why")
    @has_permissions(manage_roles=True)
    @max_concurrency(1, BucketType.guild)
    @cooldown(1, 30, BucketType.guild)
    async def unlockdown_all(self, ctx: Context, *, reason: str = "No reason provided") -> Message:
        """Allow members to send messages in all channels."""
        with self.bot.tracer.start_span("unlockdown_all") as span:
            try:
                if not ctx.settings.lock_ignore:
                    await ctx.prompt(
                        await ctx.bot.get_text("moderation.unlockdown.all.CONFIRM.TITLE", ctx),
                        await ctx.bot.get_text("moderation.unlockdown.all.CONFIRM.WARNING", ctx),
                    )

                initial_message = await ctx.neutral(
                    await ctx.bot.get_text("moderation.unlockdown.all.PROGRESS", ctx)
                )

                channels_to_unlock = [
                    channel for channel in ctx.guild.text_channels
                    if channel.overwrites_for(ctx.settings.lock_role).send_messages is not True
                    and channel not in ctx.settings.lock_ignore
                ]
                
                batch_size = 10
                for i in range(0, len(channels_to_unlock), batch_size):
                    batch = channels_to_unlock[i:i + batch_size]
                    tasks = []
                    
                    for channel in batch:
                        overwrite = channel.overwrites_for(ctx.settings.lock_role)
                        overwrite.send_messages = True
                        tasks.append(channel.set_permissions(
                            ctx.settings.lock_role,
                            overwrite=overwrite,
                            reason=f"{ctx.author.name} / {reason} (SERVER UNLOCKDOWN)",
                        ))
                    
                    await asyncio.gather(*tasks, return_exceptions=True)

                duration = perf_counter() - start
                CHANNEL_METRICS.labels(
                    action="unlockdown_all",
                    guild_id=str(ctx.guild.id)
                ).inc(len(channels_to_unlock))
                
                span.set_attribute("channels_unlocked", len(channels_to_unlock))
                span.set_attribute("duration_seconds", duration)
                
                return await ctx.approve(
                    await ctx.bot.get_text(
                        "moderation.unlockdown.all.SUCCESS",
                        ctx,
                        count=len(channels_to_unlock),
                        duration=duration
                    ),
                    patch=initial_message,
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @hybrid_command(aliases=["private", "priv"], example="#general idk why")
    @has_permissions(manage_roles=True)
    async def hide(self, ctx: Context, channel: Optional[TextChannel | VoiceChannel],
                  target: Optional[Member | Role], *, reason: str = "No reason provided") -> Message:
        """Hide a channel from a member or role."""
        with self.bot.tracer.start_span("channel_hide") as span:
            try:
                channel = cast(TextChannel, channel or ctx.channel)
                if not isinstance(channel, (TextChannel, VoiceChannel)):
                    return await ctx.warn("You can only hide text & voice channels!")

                target = target or ctx.settings.lock_role
                span.set_attribute("channel_id", str(channel.id))
                span.set_attribute("target_id", str(target.id))

                with CHANNEL_METRICS.labels(action="hide").time():
                    if channel.overwrites_for(target).read_messages is False:
                        return await ctx.warn(
                            f"{channel.mention} is already hidden for {target.mention}!"
                            if target != ctx.settings.lock_role
                            else f"{channel.mention} is already hidden!"
                        )

                    overwrite = channel.overwrites_for(target)
                    overwrite.read_messages = False
                    await channel.set_permissions(
                        target,
                        overwrite=overwrite,
                        reason=f"{ctx.author.name} / {reason}",
                    )

                    CHANNEL_METRICS.labels(
                        action="hide",
                        guild_id=str(ctx.guild.id)
                    ).inc()

                    return await ctx.approve(
                        f"{channel.mention} is now hidden for {target.mention}"
                        if target != ctx.settings.lock_role
                        else f"{channel.mention} is now hidden"
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @hybrid_command(aliases=["unhide", "public"], example="#general")
    @has_permissions(manage_roles=True)
    async def reveal(
        self,
        ctx: Context,
        channel: Optional[TextChannel | VoiceChannel],
        target: Optional[Member | Role],
        *,
        reason: str = "No reason provided",
    ) -> Message:
        """
        Reveal a channel to a member or role.
        """

        channel = cast(TextChannel, channel or ctx.channel)
        if not isinstance(channel, (TextChannel, VoiceChannel)):
            return await ctx.warn("You can only hide text & voice channels!")

        target = target or ctx.settings.lock_role

        if channel.overwrites_for(target).read_messages is True:
            return await ctx.warn(
                f"{channel.mention} is already revealed for {target.mention}!"
                if target != ctx.settings.lock_role
                else f"{channel.mention} is already revealed!"
            )

        overwrite = channel.overwrites_for(target)
        overwrite.read_messages = True
        await channel.set_permissions(
            target,
            overwrite=overwrite,
            reason=f"{ctx.author.name} / {reason}",
        )

        return await ctx.approve(
            f"{channel.mention} is now revealed for {target.mention}"
            if target != ctx.settings.lock_role
            else f"{channel.mention} is now revealed"
        )

    @hybrid_group(
        aliases=["slowmo", "slow"],
        invoke_without_command=True,
        example="#general 5m",
    )
    @has_permissions(manage_channels=True)
    async def slowmode(self, ctx: Context, channel: Optional[TextChannel],
                      delay: timedelta = parameter(converter=Duration(min=timedelta(seconds=0), max=timedelta(hours=6)))) -> Message:
        """Set the slowmode for a channel."""
        with self.bot.tracer.start_span("slowmode_set") as span:
            try:
                channel = cast(TextChannel, channel or ctx.channel)
                if not isinstance(channel, TextChannel):
                    return await ctx.warn("You can only set the slowmode for text channels!")

                span.set_attribute("channel_id", str(channel.id))
                span.set_attribute("delay_seconds", delay.seconds)

                with CHANNEL_METRICS.labels(action="slowmode_set").time():
                    if channel.slowmode_delay == delay.seconds:
                        return await ctx.warn(
                            f"{channel.mention} already has a slowmode of **{precisedelta(delay)}**!"
                        )

                    await channel.edit(slowmode_delay=delay.seconds)
                    
                    CHANNEL_METRICS.labels(
                        action="slowmode_set",
                        guild_id=str(ctx.guild.id)
                    ).inc()

                    return await ctx.approve(
                        f"Set the slowmode for {channel.mention} to **{precisedelta(delay)}**"
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @slowmode.command(
        name="disable",
        aliases=["off"],
        example="#general",
    )
    @has_permissions(manage_channels=True)
    async def slowmode_disable(
        self,
        ctx: Context,
        channel: Optional[TextChannel],
    ) -> Message:
        """
        Disable slowmode for a channel.
        """

        channel = cast(TextChannel, channel or ctx.channel)
        if not isinstance(channel, TextChannel):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.slowmode.disable.TEXT_ONLY", ctx)
            )

        if channel.slowmode_delay == 0:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.slowmode.disable.ALREADY_DISABLED",
                    ctx,
                    channel=channel.mention
                )
            )

        await channel.edit(slowmode_delay=0)
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.slowmode.disable.SUCCESS",
                ctx,
                channel=channel.mention
            )
        )

    @hybrid_command(aliases=["naughty", "sfw"], example="#nsfw")
    @has_permissions(manage_channels=True)
    async def nsfw(
        self,
        ctx: Context,
        channel: Optional[TextChannel],
    ) -> Message:
        """
        Mark a channel as NSFW or SFW.
        """

        channel = cast(TextChannel, channel or ctx.channel)
        if not isinstance(channel, TextChannel):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.nsfw.TEXT_ONLY", ctx)
            )

        await channel.edit(
            nsfw=not channel.is_nsfw(),
            reason=f"Changed by {ctx.author.name} ({ctx.author.id})",
        )
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.nsfw.SUCCESS",
                ctx,
                channel=channel.mention,
                status='NSFW' if channel.is_nsfw() else 'SFW'
            )
        )

    @hybrid_group(invoke_without_command=True, example="#general hi")
    @has_permissions(manage_channels=True)
    async def topic(
        self,
        ctx: Context,
        channel: Optional[TextChannel],
        *,
        text: Range[str, 1, 1024],
    ) -> Message:
        """
        Set a channel's topic.
        """

        channel = cast(TextChannel, channel or ctx.channel)
        if not isinstance(channel, TextChannel):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.topic.TEXT_ONLY", ctx)
            )

        try:
            await channel.edit(
                topic=text,
                reason=f"Changed by {ctx.author.name} ({ctx.author.id})"
            )
        except RateLimited as exc:
            retry_after = timedelta(seconds=exc.retry_after)
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.topic.RATELIMITED",
                    ctx,
                    retry_after=precisedelta(retry_after)
                )
            )
        except HTTPException as exc:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.topic.FAILED", ctx, channel=channel.mention),
                codeblock(exc.text)
            )

        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.topic.SUCCESS",
                ctx,
                channel=channel.mention,
                text=text
            )
        )

    @topic.command(name="remove", aliases=["delete", "del", "rm"], example="#general")
    @has_permissions(manage_channels=True)
    async def topic_remove(self, ctx: Context, channel: Optional[TextChannel]) -> Message:
        """Remove a channel's topic."""
        channel = cast(TextChannel, channel or ctx.channel)
        if not isinstance(channel, TextChannel):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.topic.remove.TEXT_ONLY", ctx)
            )

        if not channel.topic:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.topic.remove.NO_TOPIC",
                    ctx,
                    channel=channel.mention
                )
            )

        try:
            await channel.edit(
                topic="",
                reason=f"Changed by {ctx.author.name} ({ctx.author.id})"
            )
        except RateLimited as exc:
            retry_after = timedelta(seconds=exc.retry_after)
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.topic.remove.RATELIMITED",
                    ctx,
                    retry_after=precisedelta(retry_after)
                )
            )
        except HTTPException as exc:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.topic.remove.FAILED",
                    ctx,
                    channel=channel.mention
                ),
                codeblock(exc.text)
            )

        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.topic.remove.SUCCESS",
                ctx,
                channel=channel.mention
            )
        )

    @hybrid_group(invoke_without_command=True, example="@x #general")
    @has_permissions(manage_channels=True)
    async def drag(self, ctx: Context, *members: Annotated[Member, TouchableMember],
                  channel: Optional[VoiceChannel | StageChannel] = None) -> Message:
        """Drag member(s) to the voice channel."""
        with self.bot.tracer.start_span("drag_members") as span:
            try:
                if not channel:
                    if not ctx.author.voice or not ctx.author.voice.channel:
                        return await ctx.warn(
                            await ctx.bot.get_text("moderation.drag.NO_VOICE", ctx)
                        )
                    channel = ctx.author.voice.channel

                span.set_attribute("target_channel_id", str(channel.id))
                span.set_attribute("member_count", len(members))

                async def move_member(member: Member) -> bool:
                    if member in channel.members:
                        return False
                    try:
                        await member.move_to(
                            channel,
                            reason=f"{ctx.author} dragged member",
                        )
                        return True
                    except HTTPException:
                        return False

                with VOICE_METRICS.labels(action="drag").time():
                    results = await asyncio.gather(*[
                        move_member(member) for member in members
                    ])
                    moved = sum(results)

                VOICE_METRICS.labels(
                    action="drag",
                    guild_id=str(ctx.guild.id)
                ).inc(moved)

                span.set_attribute("members_moved", moved)
                return await ctx.approve(
                    f"Moved `{moved}`/`{len(members)}` member{'s' if moved != 1 else ''} to {channel.mention}"
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @drag.command(name="all", aliases=["everyone"], example="#general")
    @has_permissions(manage_channels=True)
    @max_concurrency(1, BucketType.member)
    @cooldown(1, 10, BucketType.member)
    async def drag_all(self, ctx: Context, *, channel: VoiceChannel | StageChannel) -> Message:
        """Move all members to another voice channel."""
        with self.bot.tracer.start_span("drag_all") as span:
            try:
                if not ctx.author.voice or not ctx.author.voice.channel:
                    return await ctx.warn("You aren't in a voice channel!")
                elif ctx.author.voice.channel == channel:
                    return await ctx.warn(f"You're already connected to {channel.mention}!")

                members = ctx.author.voice.channel.members
                span.set_attribute("member_count", len(members))
                span.set_attribute("source_channel_id", str(ctx.author.voice.channel.id))
                span.set_attribute("target_channel_id", str(channel.id))

                async def move_member(member: Member) -> bool:
                    try:
                        await member.move_to(
                            channel,
                            reason=f"{ctx.author} moved all members",
                        )
                        return True
                    except HTTPException:
                        return False

                with VOICE_METRICS.labels(action="drag_all").time():
                    results = await asyncio.gather(*[
                        move_member(member) for member in members
                    ])
                    moved = sum(results)

                VOICE_METRICS.labels(
                    action="drag_all",
                    guild_id=str(ctx.guild.id)
                ).inc(moved)

                span.set_attribute("members_moved", moved)
                return await ctx.approve(
                    f"Moved `{moved}`/`{len(members)}` member{'s' if moved != 1 else ''} to {channel.mention}"
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @hybrid_command(aliases=["mvall"])
    @has_permissions(manage_channels=True)
    async def moveall(
        self,
        ctx: Context,
        *,
        channel: VoiceChannel | StageChannel,
    ) -> Message:
        """
        Move all members to another voice channel.
        This is an alias for the `drag all` command.
        """

        return await ctx.invoke(self.drag_all, channel=channel)

    @hybrid_command(aliases=["newmembers"], example="15")
    async def newusers(self, ctx: Context, *, amount: Range[int, 5, 100] = 10) -> Message:
        """View a list of the newest members."""
        with self.bot.tracer.start_span("newusers_list") as span:
            try:
                span.set_attribute("amount", amount)
                
                if not ctx.guild.chunked:
                    await ctx.guild.chunk(cache=True)

                def sort_members(members_data):
                    return sorted(
                        members_data,
                        key=lambda x: x[1] or x[2],
                        reverse=True
                    )[:amount]

                members_data = [
                    (member.id, member.joined_at, ctx.guild.created_at)
                    for member in ctx.guild.members
                ]

                sorted_members = await self.bot.loop.run_in_executor(
                    None,
                    self.bot.process_pool.apply,
                    sort_members,
                    (members_data,)
                )

                members = [ctx.guild.get_member(member_id) for member_id, _, _ in sorted_members]
                
                paginator = Paginator(
                    ctx,
                    entries=[
                        f"{member.mention} joined {format_dt(member.joined_at or ctx.guild.created_at, 'R')}"
                        for member in members if member
                    ],
                    embed=Embed(title="New Members"),
                )
                return await paginator.start()

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @command()
    @has_permissions(view_audit_log=True)
    async def audit(self, ctx: Context, user: Optional[Member | User], action: Optional[str]) -> Message:
        """View server audit log entries."""
        with self.bot.tracer.start_span("audit_log_view") as span:
            try:
                span.set_attribute("user_id", str(user.id) if user else None)
                span.set_attribute("action", action)

                _action = (action or "").lower().replace(" ", "_")
                if action and not self.actions.get(_action):
                    return await ctx.warn(f"`{action}` isn't a valid action!")

                entries: List[str] = []
                async with ctx.typing():
                    batch_size = 25
                    entry_count = 0
                    
                    async for entry in ctx.guild.audit_logs(
                        limit=100,
                        user=user or MISSING,
                        action=getattr(AuditLogAction, _action, MISSING),
                    ):
                        target: Optional[str] = await self.process_audit_target(entry)
                        entries.append(
                            f"**{entry.user}** {self.actions.get(entry.action.name, entry.action.name.replace('_', ' '))} "
                            + (f"**{target}**" if target and "`" not in target else target or "")
                        )
                        
                        entry_count += 1
                        if entry_count % batch_size == 0:
                            await asyncio.sleep(0) 

                if not entries:
                    return await ctx.warn(
                        "No **audit log** entries found"
                        + (f" for **{user}**" if user else "")
                        + (f" with action **{action}**" if action else "")
                        + "!"
                    )

                span.set_attribute("entries_found", len(entries))
                
                paginator = Paginator(
                    ctx,
                    entries=entries,
                    embed=Embed(title="Audit Log"),
                )
                return await paginator.start()

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @hybrid_command(aliases=["boot", "k"], example="@x 1d bot owner")
    @has_permissions(kick_members=True)
    @max_concurrency(1, BucketType.member)
    async def kick(self, ctx: Context, member: Annotated[Member, TouchableMember],
                  *, reason: str = "No reason provided") -> Optional[Message]:
        """Kick a member from the server."""
        with self.bot.tracer.start_span("member_kick") as span:
            try:
                span.set_attribute("target_id", str(member.id))
                span.set_attribute("reason", reason)

                if await self.is_immune(ctx, member):
                    return

                with MODERATION_METRICS.labels(action="kick").time():
                    if member.premium_since:
                        await ctx.prompt(
                            await ctx.bot.get_text("moderation.kick.CONFIRM_BOOSTER", ctx, member=member.mention),
                            await ctx.bot.get_text("moderation.kick.BOOSTER_WARNING", ctx)
                        )

                    config = await Settings.fetch(self.bot, ctx.guild)
                    if (config and config.kick and not config.is_whitelisted(ctx.author) and 
                        await config.check_threshold(self.bot, ctx.author, "kick")):
                        await strip_roles(ctx.author, dangerous=True, reason="Antinuke kick threshold reached")
                        return await ctx.warn(
                            await ctx.bot.get_text("moderation.kick.ANTINUKE.THRESHOLD", ctx),
                            await ctx.bot.get_text("moderation.kick.ANTINUKE.REVOKED", ctx)
                        )

                    async with ctx.typing():
                        await asyncio.gather(
                            ModConfig.sendlogs(self.bot, "kick", ctx.author, member, reason),
                            member.kick(reason=f"{ctx.author} / {reason}"),
                            return_exceptions=True
                        )

                    MODERATION_METRICS.labels(
                        action="kick",
                        guild_id=str(ctx.guild.id)
                    ).inc()

                    if ctx.settings.invoke_kick:
                        script = Script(
                            ctx.settings.invoke_kick,
                            [
                                ctx.guild,
                                ctx.channel,
                                member,
                                (reason, "reason"),
                                (ctx.author, "moderator"),
                            ],
                        )
                        with suppress(HTTPException):
                            await script.send(ctx)

                    return await ctx.check()

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @command(aliases=["hb"], example="@x 1d bot owner")
    @has_permissions(ban_members=True)
    async def hardban(self, ctx: Context, user: Member | User,
                     history: Optional[int] = 0, *, reason: str = "No reason provided") -> Optional[Message]:
        """Permanently ban a user from the server."""
        if history > 7:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.hardban.HISTORY_LIMIT", ctx)
            )

        if isinstance(user, Member):
            await TouchableMember().check(ctx, user)

        if isinstance(user, Member) and await self.is_immune(ctx, user):
            return

        config = await Settings.fetch(self.bot, ctx.guild)
        if not config.is_trusted(ctx.author):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.hardban.TRUSTED_ONLY", ctx)
            )

        hardban = await self.bot.db.fetchrow(
            "SELECT * FROM hardban WHERE guild_id = $1 AND user_id = $2",
            ctx.guild.id,
            user.id,
        )

        if hardban:
            await self.bot.db.execute(
                "DELETE FROM hardban WHERE guild_id = $1 AND user_id = $2",
                ctx.guild.id,
                user.id,
            )
            with suppress(NotFound):
                await ctx.guild.unban(user, reason=f"Hard ban removed by {ctx.author} ({ctx.author.id})")

            try:
                await ModConfig.sendlogs(self.bot, "hardunban", ctx.author, user, reason)
            except:
                pass

            return await ctx.approve(
                await ctx.bot.get_text("moderation.hardban.REMOVED", ctx, user=user)
            )

        await self.bot.db.execute(
            "INSERT INTO hardban (guild_id, user_id) VALUES ($1, $2)",
            ctx.guild.id,
            user.id,
        )
        await ModConfig.sendlogs(self.bot, "hardban", ctx.author, user, reason)
        await ctx.guild.ban(user, delete_message_days=history, reason=f"{ctx.author} / {reason}")

        if ctx.settings.invoke_ban:
            script = Script(
                ctx.settings.invoke_ban,
                [ctx.guild, ctx.channel, user, (reason, "reason"), (ctx.author, "moderator")]
            )
            with suppress(HTTPException):
                await script.send(ctx)

        return await ctx.approve(
            await ctx.bot.get_text("moderation.hardban.SUCCESS", ctx, user=user)
        )

    @command(name="hardbanlist", aliases=["ls"])
    @has_permissions(ban_members=True)
    async def hardban_list(self, ctx: Context) -> Message:
        """View all hard banned users."""
        hardban = await self.bot.db.fetch("SELECT user_id FROM hardban WHERE guild_id = $1", ctx.guild.id)

        config = await Settings.fetch(self.bot, ctx.guild)
        if not config.is_trusted(ctx.author):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.hardbanlist.TRUSTED_ONLY", ctx)
            )

        if not hardban:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.hardbanlist.NO_USERS", ctx)
            )
        
        paginator = Paginator(
            ctx,
            entries=[
                f"**{self.bot.get_user(int(user_id['user_id'])) or 'Unknown User'}** (`{user_id['user_id']}`)"
                for user_id in hardban
            ],
            embed=Embed(
                title=await ctx.bot.get_text("moderation.hardbanlist.TITLE", ctx)
            ),
        )
        return await paginator.start()

    @command(aliases=["massb"], example="@x 1d bot owner")
    @has_permissions(ban_members=True)
    async def massban(
        self,
        ctx: Context,
        users: Greedy[Member | User],
        history: Optional[Range[int, 0, 7]] = None,
        *,
        reason: str = "No reason provided",
    ) -> Optional[Message]:
        """
        Ban multiple users from the server.

        This command is limited to 150 users at a time.
        If you want to hard ban users, add `--hardban` to the reason.
        """
        for user in users:
            if isinstance(user, Member) and await self.is_immune(ctx, user):
                return

        config = await Settings.fetch(self.bot, ctx.guild)
        if not config.is_trusted(ctx.author):
            return await ctx.warn(
                "You must be a **trusted administrator** to use this command!"
            )

        elif not users:
            return await ctx.warn("You need to provide at least one user!")

        elif len(users) > 150:
            return await ctx.warn("You can only ban up to **150 users** at a time!")

        elif len(users) > 5:
            await ctx.prompt(f"Are you sure you want to **ban** `{len(users)}` users?")

        if "--hardban" in reason:
            reason = reason.replace("--hardban", "").strip()
            key = self.hardban_key(ctx.guild)
            await self.bot.redis.sadd(key, *[str(user.id) for user in users])

        async with ctx.typing():
            for user in users:
                if isinstance(user, Member):
                    await TouchableMember().check(ctx, user)

                await ctx.guild.ban(
                    user,
                    delete_message_days=history or 0,
                    reason=f"{ctx.author} / {reason} (MASS BAN)",
                )

        try:
            await ModConfig.sendlogs(self.bot, "massban", ctx.author, users, reason)  # type: ignore
        except:
            pass
        return await ctx.check()

    @command(
        example="@x bot owner",
        name="fn",
    )
    @has_permissions(manage_nicknames=True)
    async def fn(
        self,
        ctx: Context,
        member: Annotated[
            Member,
            TouchableMember,
        ],
        *,
        nickname: Range[str, 1, 32],
    ) -> None:
        """
        Force a member's nickname.
        """
        if await self.is_immune(ctx, member):
            return
        
        await self.bot.db.execute(
            "INSERT INTO forcenick (guild_id, user_id, nickname) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET nickname = $3",
            ctx.guild.id, member.id, nickname,
        )

        await member.edit(nick=nickname, reason=f"{ctx.author} ({ctx.author.id})")
        try:
            await ModConfig.sendlogs(self.bot, "forcenick", ctx.author, member, "No reason provided")
        except:
            pass
        return await ctx.check()

    @hybrid_command(
        aliases=["deport", "b"],
        example="@x 1d bot owner",
    )
    @has_permissions(ban_members=True)
    @max_concurrency(1, BucketType.member)
    async def ban(
        self,
        ctx: Context,
        user: Member | User,
        history: Optional[Range[int, 0, 7]] = None,
        *,
        reason: str = "No reason provided",
    ) -> Optional[Message]:
        """
        Ban a user from the server.
        """

        if isinstance(user, Member) and await self.is_immune(ctx, user):
            return

        if isinstance(user, Member):
            await TouchableMember().check(ctx, user)

            if user.premium_since:

                await ctx.prompt(
                    f"Are you sure you want to **ban** {user.mention}?",
                    "They are currently boosting the server!",
                )

        config = await Settings.fetch(self.bot, ctx.guild)
        if (
            config.ban
            and not config.is_whitelisted(ctx.author)
            and await config.check_threshold(self.bot, ctx.author, "ban")
        ):
            await strip_roles(
                ctx.author, dangerous=True, reason="Antinuke ban threshold reached"
            )
            return await ctx.warn(
                "You've exceeded the antinuke threshold for **bans**!",
                "Your **administrative permissions** have been revoked",
            )

        await ctx.guild.ban(
            user,
            delete_message_seconds=history * 86400 if history is not None else 0,
            reason=f"{ctx.author} / {reason}",
        )

        if ctx.settings.invoke_ban is not None:
            script = Script(
                ctx.settings.invoke_ban,
                [
                    ctx.guild,
                    ctx.channel,
                    user,
                    (reason, "reason"),
                    (ctx.author, "moderator"),
                ],
            )
            with suppress(HTTPException):
                await script.send(ctx)
        try:
            await ModConfig.sendlogs(self.bot, "ban", ctx.author, user, reason)  # type: ignore
        except:
            pass
        return await ctx.check()

    @hybrid_command(example="@x 1d bot owner")
    @has_permissions(ban_members=True)
    @max_concurrency(1, BucketType.member)
    async def softban(
        self,
        ctx: Context,
        member: Annotated[
            Member,
            TouchableMember,
        ],
        history: Optional[Range[int, 1, 7]] = None,
        *,
        reason: str = "No reason provided",
    ) -> Optional[Message]:
        """
        Ban then unban a member from the server.

        This is used to cleanup messages from the member.
        """
        if await self.is_immune(ctx, member):
            return

        if member.premium_since:
            await ctx.prompt(
                await ctx.bot.get_text("moderation.softban.BOOSTER_CONFIRM", ctx, member=member.mention),
                await ctx.bot.get_text("moderation.softban.BOOSTER_WARNING", ctx)
            )

        config = await Settings.fetch(self.bot, ctx.guild)
        if config.ban and not config.is_whitelisted(ctx.author) and await config.check_threshold(self.bot, ctx.author, "ban"):
            await strip_roles(ctx.author, dangerous=True, reason="Antinuke ban threshold reached")
            return await ctx.warn(
                await ctx.bot.get_text("moderation.softban.ANTINUKE.THRESHOLD", ctx),
                await ctx.bot.get_text("moderation.softban.ANTINUKE.REVOKED", ctx)
            )

        try:
            await ModConfig.sendlogs(self.bot, "ban", ctx.author, user, reason)  # type: ignore
        except:
            pass
        await ctx.guild.ban(
            member,
            delete_message_days=history or 0,
            reason=f"{ctx.author} / {reason}",
        )
        await ctx.guild.unban(member)
        if ctx.settings.invoke_ban:
            script = Script(
                ctx.settings.invoke_ban,
                [
                    ctx.guild,
                    ctx.channel,
                    member,
                    (reason, "reason"),
                    (ctx.author, "moderator"),
                ],
            )
            with suppress(HTTPException):
                await script.send(ctx)

        return await ctx.check()

    @hybrid_group(
        example="@x bot owner",
        aliases=["pardon", "unb"],
        invoke_without_command=True,
    )
    @has_permissions(ban_members=True)
    async def unban(
        self,
        ctx: Context,
        user: User,
        *,
        reason: str = "No reason provided",
    ):
        """
        Unban a user from the server.
        """
        hardban = await self.bot.db.fetchrow(
            "SELECT * FROM hardban WHERE guild_id = $1 AND user_id = $2",
            ctx.guild.id, user.id
        )
        config = await Settings.fetch(self.bot, ctx.guild)
        if hardban and not config.is_trusted(ctx.author):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.unban.TRUSTED_ONLY", ctx)
            )

        try:
            await ctx.guild.unban(user, reason=f"{ctx.author} / {reason}")
        except NotFound:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.unban.NOT_BANNED", ctx)
            )

        try:
            await ModConfig.sendlogs(self.bot, "unban", ctx.author, user, reason)  # type: ignore
        except:
            pass

        if ctx.settings.invoke_unban:
            script = Script(
                ctx.settings.invoke_unban,
                [
                    ctx.guild,
                    ctx.channel,
                    user,
                    (reason, "reason"),
                    (ctx.author, "moderator"),
                ],
            )
            with suppress(HTTPException):
                await script.send(ctx)

        return await ctx.check()

    @unban.command(name="all")
    @has_permissions(ban_members=True)
    @max_concurrency(1, BucketType.guild)
    async def unban_all(self, ctx: Context) -> Optional[Message]:
        """Unban all banned users from the server."""
        with self.bot.tracer.start_span("unban_all") as span:
            try:
                async with ctx.typing():
                    hardban_query = self.bot.db.fetch(
                        """
                        SELECT user_id FROM 
                        hardban WHERE 
                        guild_id = $1
                        """,
                        ctx.guild.id,
                    )
                    
                    ban_entries = [entry async for entry in ctx.guild.bans()]
                    hardban_records = await hardban_query
                    
                    hardban_ids = {record['user_id'] for record in hardban_records}
                    
                    def filter_users(entries, hardban_set):
                        import dask.bag as db
                        entries_bag = db.from_sequence(entries)
                        return entries_bag.filter(lambda e: e.user.id not in hardban_set).compute()
                    
                    users = await self.bot.loop.run_in_executor(
                        None,
                        filter_users,
                        ban_entries,
                        hardban_ids
                    )

                    if not users:
                        return await ctx.warn(
                            await ctx.bot.get_text("moderation.unban.all.NO_USERS", ctx)
                        )

                    span.set_attribute("users_to_unban", len(users))
                    await ctx.prompt(
                        await ctx.bot.get_text(
                            "moderation.unban.all.CONFIRM",
                            ctx,
                            count=len(users)
                        )
                    )

                    batch_size = 50
                    total_unbanned = 0
                    initial_message = await ctx.neutral(
                        await ctx.bot.get_text(
                            "moderation.unban.all.INITIAL",
                            ctx,
                            count=len(users)
                        )
                    )

                    async with ctx.typing():
                        for i in range(0, len(users), batch_size):
                            batch = users[i:i + batch_size]
                            unban_tasks = []
                            
                            for user in batch:
                                unban_tasks.append(
                                    ctx.guild.unban(
                                        user.user,
                                        reason=f"{ctx.author} ({ctx.author.id}) / UNBAN ALL"
                                    )
                                )
                            
                            results = await asyncio.gather(*unban_tasks, return_exceptions=True)
                            successful = sum(1 for r in results if not isinstance(r, Exception))
                            total_unbanned += successful
                            
                            if i % (batch_size * 2) == 0:
                                await initial_message.edit(
                                    content=await ctx.bot.get_text(
                                        "moderation.unban.all.PROGRESS",
                                        ctx,
                                        current=total_unbanned,
                                        total=len(users)
                                    )
                                )
                            
                            await asyncio.sleep(0.5)

                    MODERATION_METRICS.labels(
                        action="unban_all",
                        guild_id=str(ctx.guild.id)
                    ).inc(total_unbanned)

                    span.set_attribute("users_unbanned", total_unbanned)
                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.unban.all.SUCCESS",
                            ctx,
                            count=total_unbanned
                        ),
                        patch=initial_message
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @hybrid_group(
        example="@x bot owner",
        aliases=["nick", "n"],
        invoke_without_command=True,
    )
    @has_permissions(manage_nicknames=True)
    async def nickname(
        self,
        ctx: Context,
        member: Annotated[
            Member,
            TouchableMember(
                allow_author=True,
            ),
        ],
        *,
        nickname: Range[str, 1, 32],
    ) -> Optional[Message]:
        """
        Change a member's nickname.
        """
        if await self.is_immune(ctx, member):
            return
        
        forcenick = await self.bot.db.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM forcenick 
                WHERE guild_id = $1 
                AND user_id = $2
            )
            """,
            ctx.guild.id,
            member.id,
        )
        if forcenick:
            return await ctx.warn(
                f"{member.mention} has a forced nickname!",
                f"Use `{ctx.prefix}nickname remove {member}` to reset it",
            )

        try:
            await ModConfig.sendlogs(self.bot, "nickname", ctx.author, member, "No reason provided")  # type: ignore
        except:
            pass
        await member.edit(
            nick=nickname,
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.check()

    @nickname.command(
        example="@x",
        name="remove",
        aliases=["reset", "rm"],
    )
    @has_permissions(manage_nicknames=True)
    async def nickname_remove(
        self,
        ctx: Context,
        member: Annotated[
            Member,
            TouchableMember,
        ],
    ) -> None:
        """
        Reset a member's nickname.
        """
        if await self.is_immune(ctx, member):
            return

        await self.bot.db.execute(
            "DELETE FROM forcenick WHERE guild_id = $1 AND user_id = $2",
            ctx.guild.id, member.id
        )

        await member.edit(nick=None, reason=f"{ctx.author} ({ctx.author.id})")
        try:
            await ModConfig.sendlogs(self.bot, "nickname remove", ctx.author, member, "None")
        except:
            pass
        return await ctx.check()

    @nickname.group(
        example="@x bot owner",
        name="force",
        aliases=["lock"],
        invoke_without_command=True,
    )
    @has_permissions(manage_nicknames=True)
    async def nickname_force(
        self,
        ctx: Context,
        member: Annotated[
            Member,
            TouchableMember,
        ],
        *,
        nickname: Range[str, 1, 32],
    ) -> None:
        """
        Force a member's nickname.
        """
        if await self.is_immune(ctx, member):
            return
        
        await self.bot.db.execute(
            "INSERT INTO forcenick (guild_id, user_id, nickname) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET nickname = $3",
            ctx.guild.id, member.id, nickname
        )

        await member.edit(nick=nickname, reason=f"{ctx.author} ({ctx.author.id})")
        try:
            await ModConfig.sendlogs(self.bot, "forcenick", ctx.author, member, "No reason provided")
        except:
            pass
        return await ctx.check()

    @nickname_force.command(
        name="cancel",
        aliases=["stop"],
        example="@x"
    )
    @has_permissions(manage_nicknames=True)
    async def nickname_force_cancel(
        self,
        ctx: Context,
        member: Annotated[
            Member,
            TouchableMember,
        ],
    ) -> Optional[Message]:
        """
        Cancel a member's forced nickname.
        """
        await self.bot.db.execute(
            "DELETE FROM forcenick WHERE guild_id = $1 AND user_id = $2",
            ctx.guild.id, member.id
        )

        await member.edit(nick=None, reason=f"{ctx.author} ({ctx.author.id})")
        try:
            await ModConfig.sendlogs(self.bot, "forcenick remove", ctx.author, member, "No reason provided")  # type: ignore
        except:
            pass
        return await ctx.check()

    @hybrid_group(
        example="@x 1d bot owner",
        aliases=[
            "mute",
            "tmo",
            "to",
        ],
        invoke_without_command=True,
    )
    @has_permissions(moderate_members=True)
    async def timeout(
        self,
        ctx: Context,
        member: Annotated[
            Member,
            TouchableMember,
        ],
        duration: timedelta = parameter(
            converter=Duration(
                min=timedelta(seconds=60),
                max=timedelta(days=27),
            ),
            default=timedelta(minutes=5),
        ),
        *,
        reason: str = "No reason provided",
    ) -> Optional[Message]:
        """
        Timeout a member from the server.
        """
        if await self.is_immune(ctx, member):
            return

        await member.timeout(
            duration,
            reason=f"{ctx.author} / {reason}",
        )
        if ctx.settings.invoke_timeout:
            script = Script(
                ctx.settings.invoke_timeout,
                [
                    ctx.guild,
                    ctx.channel,
                    member,
                    (reason, "reason"),
                    (ctx.author, "moderator"),
                    (format_timespan(duration), "duration"),
                    (format_dt(utcnow() + duration, "R"), "expires"),
                    (str(int((utcnow() + duration).timestamp())), "expires_timestamp"),
                ],
            )
            with suppress(HTTPException):
                await script.send(ctx)

        try:
            await ModConfig.sendlogs(
                self.bot, "timeout", ctx.author, member, reason
            )  # , duration=duratio
        except:
            pass
        return await ctx.check()

    @timeout.command(
        name="list",
        aliases=["ls"],
    )
    @has_permissions(moderate_members=True)
    async def timeout_list(self, ctx: Context) -> Message:
        """
        View all timed out members.
        """

        members = list(
            filter(
                lambda member: member.is_timed_out(),
                ctx.guild.members,
            )
        )
        if not members:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.timeout.list.NO_MEMBERS", ctx)
            )

        paginator = Paginator(
            ctx,
            entries=[
                await ctx.bot.get_text(
                    "moderation.timeout.list.ENTRY",
                    ctx,
                    member=member.mention,
                    expires=format_dt(member.timed_out_until or utcnow(), 'R')
                )
                for member in sorted(
                    members,
                    key=lambda member: member.timed_out_until or utcnow(),
                )
            ],
            embed=Embed(
                title=await ctx.bot.get_text("moderation.timeout.list.TITLE", ctx)
            ),
        )
        return await paginator.start()

    @hybrid_group(
        example="@x bot owner",
        aliases=[
            "unmute",
            "untmo",
            "unto",
            "utmo",
            "uto",
        ],
        invoke_without_command=True,
    )
    @has_permissions(moderate_members=True)
    async def untimeout(
        self,
        ctx: Context,
        member: Annotated[
            Member,
            TouchableMember,
        ],
        *,
        reason: str = "No reason provided",
    ) -> Optional[Message]:
        """
        Lift a member's timeout.
        """

        if not member.is_timed_out():
            return await ctx.warn(
                await ctx.bot.get_text("moderation.untimeout.NOT_TIMED_OUT", ctx)
            )

        await member.timeout(None, reason=f"{ctx.author} / {reason}")
        if ctx.settings.invoke_untimeout:
            script = Script(
                ctx.settings.invoke_untimeout,
                [
                    ctx.guild,
                    ctx.channel,
                    member,
                    (reason, "reason"),
                    (ctx.author, "moderator"),
                ],
            )
            with suppress(HTTPException):
                await script.send(ctx)

        try:
            await ModConfig.sendlogs(self.bot, "untimeout", ctx.author, member, reason)  # type: ignore
        except:
            pass
        return await ctx.check()

    @untimeout.command(name="all")
    @max_concurrency(1, BucketType.guild)
    @has_permissions(moderate_members=True)
    async def untimeout_all(self, ctx: Context) -> Optional[Message]:
        """Lift all timeouts."""
        with self.bot.tracer.start_span("untimeout_all") as span:
            try:
                def filter_timed_out(members_data):
                    import dask.bag as db
                    members_bag = db.from_sequence(members_data)
                    return members_bag.filter(lambda m: m[1]).compute()

                members_data = [
                    (member.id, member.is_timed_out())
                    for member in ctx.guild.members
                ]

                filtered_data = await self.bot.loop.run_in_executor(
                    None,
                    filter_timed_out,
                    members_data
                )

                members = [ctx.guild.get_member(member_id) 
                          for member_id, _ in filtered_data 
                          if ctx.guild.get_member(member_id)]

                if not members:
                    return await ctx.warn("No members are currently timed out!")

                span.set_attribute("members_to_untimeout", len(members))
                initial_message = await ctx.neutral(f"Removing timeout from {len(members)} members...")

                batch_size = 50
                total_removed = 0
                
                async with ctx.typing():
                    for i in range(0, len(members), batch_size):
                        batch = members[i:i + batch_size]
                        tasks = []
                        
                        for member in batch:
                            tasks.append(
                                member.timeout(
                                    None,
                                    reason=f"{ctx.author} ({ctx.author.id}) lifted all timeouts"
                                )
                            )
                        
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        successful = sum(1 for r in results if not isinstance(r, Exception))
                        total_removed += successful

                        if i % (batch_size * 2) == 0:
                            await initial_message.edit(
                                content=f"Removing timeouts... ({total_removed}/{len(members)})"
                            )

                        await asyncio.sleep(0.5)

                MODERATION_METRICS.labels(
                    action="untimeout_all",
                    guild_id=str(ctx.guild.id)
                ).inc(total_removed)

                span.set_attribute("timeouts_removed", total_removed)
                return await ctx.approve(
                    f"Successfully removed timeout from {plural(total_removed, md='`'):member}",
                    patch=initial_message
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @group(
        aliases=["emote", "e", "jumbo"],
        invoke_without_command=True,
    )
    @has_permissions(manage_expressions=True)
    async def emoji(self, ctx: Context, emoji: PartialEmoji | str) -> Message:
        """Various emoji management commands."""
        if isinstance(emoji, str):
            url, name = unicode_emoji(emoji)
        else:
            url, name = emoji.url, emoji.name

        response = await self.bot.session.get(url)
        if not response.ok:
            return await ctx.send_help(ctx.command)

        buffer = await response.read()
        _, suffix = url_to_mime(url)

        image, suffix = await enlarge_emoji(buffer, suffix[1:])
        if not image:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.emoji.DOWNLOAD_ERROR", ctx)
            )

        try:
            return await ctx.send(
                file=File(BytesIO(image), filename=f"{name}.{suffix}")
            )
        except HTTPException:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.emoji.TOO_LARGE", ctx)
            )

    @emoji.command(name="information", aliases=["i", "info", "details"])
    async def emoji_information(self, ctx: Context, emoji: PartialEmoji) -> Message:
        """View detailed information about an emoji."""
        button = Button(
            label=await ctx.bot.get_text("moderation.emoji.information.BUTTON_LABEL", ctx),
            style=discord.ButtonStyle.gray,
            emoji=emoji,
            url=f"{emoji.url}",
        )

        embed = Embed(
            title=await ctx.bot.get_text(
                "moderation.emoji.information.TITLE",
                ctx,
                name=emoji.name
            )
        )
        embed.set_thumbnail(url=emoji.url)
        embed.add_field(
            name=await ctx.bot.get_text("moderation.emoji.information.FIELDS.ID", ctx),
            value=emoji.id,
            inline=False,
        )
        embed.add_field(
            name=await ctx.bot.get_text("moderation.emoji.information.FIELDS.ANIMATED", ctx),
            value=emoji.animated,
            inline=False,
        )
        embed.add_field(
            name=await ctx.bot.get_text("moderation.emoji.information.FIELDS.CODE", ctx),
            value=f"`{emoji}`",
            inline=False
        )
        embed.add_field(
            name=await ctx.bot.get_text("moderation.emoji.information.FIELDS.CREATED_AT", ctx),
            value=format_dt(emoji.created_at, "R"),
            inline=False,
        )
        
        view = View()
        view.add_item(button)
        return await ctx.send(embed=embed, view=view)

    @emoji.command(name="sticker")
    @has_permissions(manage_expressions=True)
    async def emoji_sticker(self, ctx: Context, name: Optional[Range[str, 2, 32]], 
                          emoji: PartialEmoji):
        """Convert a custom emoji to a sticker."""
        if len(ctx.guild.stickers) == ctx.guild.sticker_limit:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.emoji.sticker.STICKER_LIMIT", ctx)
            )
        
        try:
            sticker = await ctx.guild.create_sticker(
                name=name or emoji.name,
                description="ya",
                emoji=str(emoji),
                file=File(BytesIO(await emoji.read())),
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except RateLimited as exc:
            retry_after = timedelta(seconds=exc.retry_after)
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.emoji.sticker.RATELIMITED",
                    ctx,
                    retry_after=precisedelta(retry_after)
                )
            )
        except HTTPException as exc:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.emoji.sticker.FAILED", ctx),
                codeblock(exc.text)
            )

        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.emoji.sticker.SUCCESS",
                ctx,
                name=sticker.name,
                url=sticker.url
            )
        )

    @emoji.group(name="add", aliases=["create", "upload", "steal"],
                invoke_without_command=True)
    @has_permissions(manage_expressions=True)
    async def emoji_add(self, ctx: Context,
                       image: PartialEmoji | PartialAttachment = parameter(
                           default=PartialAttachment.fallback),
                       *, name: Optional[Range[str, 2, 32]]) -> Optional[Message]:
        """Add an emoji to the server."""
        if not image.url:
            return await ctx.send_help(ctx.command)

        elif len(ctx.guild.emojis) == ctx.guild.emoji_limit:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.emoji.add.EMOJI_LIMIT", ctx)
            )

        try:
            await ctx.guild.create_custom_emoji(
                name=name or (image.name if isinstance(image, PartialEmoji) else image.filename),
                image=(await image.read() if isinstance(image, PartialEmoji) else image.buffer),
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except RateLimited as exc:
            retry_after = timedelta(seconds=exc.retry_after)
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.emoji.add.RATELIMITED",
                    ctx,
                    retry_after=precisedelta(retry_after)
                )
            )
        except HTTPException as exc:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.emoji.add.FAILED", ctx),
                codeblock(exc.text)
            )

        return await ctx.check()

    @emoji_add.command(
        name="reactions",
        aliases=[
            "reaction",
            "reacts",
            "react",
        ],
        example="324325345436"
    )
    @has_permissions(manage_expressions=True)
    async def emoji_add_reactions(
        self,
        ctx: Context,
        message: Optional[Message],
    ) -> Message:
        """
        Add emojis from the reactions on a message.
        """

        message = message or ctx.replied_message
        if not message:
            async for _message in ctx.channel.history(limit=25, before=ctx.message):
                if _message.reactions:
                    message = _message
                    break

        if not message:
            return await ctx.send_help(ctx.command)

        elif not message.reactions:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.emoji.add.reactions.NO_REACTIONS", ctx)
            )

        added_emojis: List[Emoji] = []
        async with ctx.typing():
            for reaction in message.reactions:
                if not reaction.is_custom_emoji():
                    continue

                emoji = reaction.emoji
                if isinstance(emoji, str):
                    continue

                try:
                    emoji = await ctx.guild.create_custom_emoji(
                        name=emoji.name,
                        image=await emoji.read(),
                        reason=f"Created by {ctx.author} ({ctx.author.id})",
                    )
                except RateLimited as exc:
                    return await ctx.warn(
                        await ctx.bot.get_text(
                            "moderation.emoji.add.reactions.RATELIMITED",
                            ctx,
                            count=len(added_emojis),
                            retry_after=format_timespan(int(exc.retry_after))
                        ),
                        patch=ctx.response,
                    )

                except HTTPException:
                    if (
                        len(ctx.guild.emojis) + len(added_emojis)
                        > ctx.guild.emoji_limit
                    ):
                        return await ctx.warn(
                            await ctx.bot.get_text("moderation.emoji.add.reactions.EMOJI_LIMIT", ctx),
                            patch=ctx.response,
                        )

                    break

                added_emojis.append(emoji)

        failed_text = (
            f" (`{len(message.reactions) - len(added_emojis)}` failed)"
            if len(added_emojis) < len(message.reactions)
            else ""
        )
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.emoji.add.reactions.SUCCESS",
                ctx,
                count=len(added_emojis),
                failed=failed_text
            )
        )

    @emoji_add.command(name="many", aliases=["bulk", "batch"])
    @has_permissions(manage_expressions=True)
    async def emoji_add_many(self, ctx: Context, *emojis: PartialEmoji) -> Message:
        """Add multiple emojis to the server."""
        with self.bot.tracer.start_span("emoji_add_many") as span:
            try:
                if not emojis:
                    return await ctx.send_help(ctx.command)
                elif len(emojis) > 50:
                    return await ctx.warn("You can only add up to **50 emojis** at a time!")
                elif len(ctx.guild.emojis) + len(emojis) > ctx.guild.emoji_limit:
                    return await ctx.warn("The server doesn't have enough space for all the emojis!")

                span.set_attribute("emoji_count", len(emojis))
                initial_message = await ctx.neutral(f"Adding {len(emojis)} emojis to the server...")
                
                async def fetch_emoji_data(emoji: PartialEmoji):
                    return {
                        'name': emoji.name,
                        'image': await emoji.read(),
                        'original': emoji
                    }

                emoji_data = await asyncio.gather(
                    *[fetch_emoji_data(emoji) for emoji in emojis],
                    return_exceptions=True
                )
                
                emoji_data = [data for data in emoji_data if not isinstance(data, Exception)]
                
                added_emojis: List[Emoji] = []
                failed_emojis: List[Dict] = []
                
                batch_size = 5
                async with ctx.typing():
                    for i in range(0, len(emoji_data), batch_size):
                        batch = emoji_data[i:i + batch_size]
                        
                        async def create_emoji(data: Dict):
                            try:
                                emoji = await ctx.guild.create_custom_emoji(
                                    name=data['name'],
                                    image=data['image'],
                                    reason=f"Created by {ctx.author} ({ctx.author.id})"
                                )
                                return {'success': True, 'emoji': emoji}
                            except RateLimited as exc:
                                return {'success': False, 'error': 'ratelimit', 'retry_after': exc.retry_after}
                            except HTTPException as e:
                                return {'success': False, 'error': 'http', 'details': str(e)}

                        results = await asyncio.gather(
                            *[create_emoji(data) for data in batch],
                            return_exceptions=True
                        )

                        for result, original_data in zip(results, batch):
                            if isinstance(result, Exception):
                                failed_emojis.append(original_data)
                                continue
                                
                            if result['success']:
                                added_emojis.append(result['emoji'])
                            elif result['error'] == 'ratelimit':
                                await initial_message.edit(content=
                                    f"Rate limited after adding {len(added_emojis)} emojis. "
                                    f"Please wait {format_timespan(int(result['retry_after']))}..."
                                )
                                await asyncio.sleep(result['retry_after'])
                            else:
                                failed_emojis.append(original_data)

                        await initial_message.edit(
                            content=f"Added {len(added_emojis)}/{len(emojis)} emojis..."
                        )
                        
                        await asyncio.sleep(1)

                EMOJI_METRICS.labels(
                    action="add_many",
                    guild_id=str(ctx.guild.id)
                ).inc(len(added_emojis))

                span.set_attribute("emojis_added", len(added_emojis))
                span.set_attribute("emojis_failed", len(failed_emojis))

                return await ctx.approve(
                    f"Added {plural(added_emojis, md='`'):emoji} to the server"
                    + (
                        f" (`{len(emojis) - len(added_emojis)}` failed)"
                        if len(added_emojis) < len(emojis)
                        else ""
                    ),
                    patch=initial_message
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @emoji.command(
        name="rename",
        aliases=["name"],
        example="hi"
    )
    @has_permissions(manage_expressions=True)
    async def emoji_rename(
        self,
        ctx: Context,
        emoji: Emoji,
        *,
        name: str,
    ) -> Message:
        """
        Rename an existing emoji.
        """

        if emoji.guild_id != ctx.guild.id:
            return await ctx.warn("That emoji is not in this server!")

        elif len(name) < 2:
            return await ctx.warn(
                "The emoji name must be at least **2 characters** long!"
            )

        name = name[:32].replace(" ", "_")
        await emoji.edit(
            name=name,
            reason=f"Updated by {ctx.author} ({ctx.author.id})",
        )

        return await ctx.approve(f"Renamed the emoji to **{name}**")

    @emoji.command(
        name="delete",
        aliases=["remove", "del"],
    )
    @has_permissions(manage_expressions=True)
    async def emoji_delete(
        self,
        ctx: Context,
        emoji: Emoji,
    ) -> Optional[Message]:
        """
        Delete an existing emoji.
        """

        if emoji.guild_id != ctx.guild.id:
            return await ctx.warn("That emoji is not in this server!")

        await emoji.delete(reason=f"Deleted by {ctx.author} ({ctx.author.id})")
        return await ctx.check()

    @emoji.group(
        name="archive",
        aliases=["zip"],
        invoke_without_command=True,
    )
    @has_permissions(manage_expressions=True)
    @cooldown(1, 30, BucketType.guild)
    async def emoji_archive(self, ctx: Context) -> Message:
        """
        Archive all emojis into a zip file.
        """

        if ctx.guild.premium_tier < 2:
            return await ctx.warn(
                "The server must have at least Level 2 to use this command!"
            )

        await ctx.neutral("Starting the archival process...")

        async with ctx.typing():
            buffer = BytesIO()
            with ZipFile(buffer, "w") as zip:
                for index, emoji in enumerate(ctx.guild.emojis):
                    name = f"{emoji.name}.{emoji.animated and 'gif' or 'png'}"
                    if name in zip.namelist():
                        name = (
                            f"{emoji.name}_{index}.{emoji.animated and 'gif' or 'png'}"
                        )

                    __buffer = await emoji.read()

                    zip.writestr(name, __buffer)

            buffer.seek(0)

        if ctx.response:
            with suppress(HTTPException):
                await ctx.response.delete()

        return await ctx.send(
            file=File(
                buffer,
                filename=f"{ctx.guild.name}_emojis.zip",
            ),
        )

    @emoji_archive.command(name="restore", aliases=["load"])
    @has_permissions(manage_expressions=True)
    @max_concurrency(1, BucketType.guild)
    async def emoji_archive_restore(self, ctx: Context, 
                                  attachment: PartialAttachment = parameter(default=PartialAttachment.fallback)) -> Message:
        """Restore emojis from an archive."""
        with self.bot.tracer.start_span("emoji_archive_restore") as span:
            try:
                if not attachment.is_archive():
                    return await ctx.warn("The attachment must be a zip archive!")

                initial_message = await ctx.neutral("Starting the restoration process...")
                span.set_attribute("archive_name", attachment.filename)

                buffer = BytesIO(attachment.buffer)
                emoji_data = []
                
                with ZipFile(buffer, "r") as zip:
                    valid_files = [name for name in zip.namelist() 
                                 if name.endswith((".png", ".gif"))]
                    
                    if len(valid_files) > (ctx.guild.emoji_limit - len(ctx.guild.emojis)):
                        return await ctx.warn(
                            "The server doesn't have enough space for all the emojis in the archive!",
                            patch=initial_message
                        )

                    def process_zip_contents(zip_data, files):
                        return [
                            {
                                'name': f.split('/')[-1][:-4],  
                                'data': zip_data.read(f),
                                'original_name': f
                            }
                            for f in files
                        ]

                    emoji_data = await self.bot.loop.run_in_executor(
                        None,
                        process_zip_contents,
                        zip,
                        valid_files
                    )

                existing_names = {e.name for e in ctx.guild.emojis}
                emoji_data = [e for e in emoji_data if e['name'] not in existing_names]

                emojis: List[Emoji] = []
                failed_emojis: List[Dict] = []

                batch_size = 5
                total_emojis = len(emoji_data)

                async with ctx.typing():
                    for i in range(0, total_emojis, batch_size):
                        batch = emoji_data[i:i + batch_size]
                        
                        async def create_emoji(data: Dict):
                            try:
                                emoji = await ctx.guild.create_custom_emoji(
                                    name=data['name'],
                                    image=data['data'],
                                    reason=f"Archive loaded by {ctx.author} ({ctx.author.id})"
                                )
                                return {'success': True, 'emoji': emoji, 'data': data}
                            except RateLimited as exc:
                                return {
                                    'success': False, 
                                    'error': 'ratelimit', 
                                    'retry_after': exc.retry_after,
                                    'data': data
                                }
                            except HTTPException as e:
                                return {
                                    'success': False, 
                                    'error': 'http', 
                                    'details': str(e),
                                    'data': data
                                }

                        results = await asyncio.gather(
                            *[create_emoji(data) for data in batch],
                            return_exceptions=True
                        )

                        for result in results:
                            if isinstance(result, Exception):
                                failed_emojis.append(result.data)
                                continue

                            if result['success']:
                                emojis.append(result['emoji'])
                            elif result['error'] == 'ratelimit':
                                await initial_message.edit(content=
                                    f"Rate limited after adding {len(emojis)} emojis. "
                                    f"Please wait {format_timespan(int(result['retry_after']))}..."
                                )
                                await asyncio.sleep(result['retry_after'])
                                emoji_data.extend(batch[results.index(result):])
                                break
                            else:
                                failed_emojis.append(result['data'])

                        await initial_message.edit(
                            content=f"Restored {len(emojis)}/{total_emojis} emojis..."
                        )
                        
                        await asyncio.sleep(1)

                EMOJI_METRICS.labels(
                    action="restore_archive",
                    guild_id=str(ctx.guild.id)
                ).inc(len(emojis))

                span.set_attribute("emojis_restored", len(emojis))
                span.set_attribute("emojis_failed", len(failed_emojis))

                if ctx.response:
                    await quietly_delete(ctx.response)

                return await ctx.approve(
                    f"Restored {plural(emojis, md='`'):emoji} from [`{attachment.filename}`]({attachment.url})"
                    + (f" (`{len(failed_emojis)}` failed)" if failed_emojis else ""),
                    patch=initial_message
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @group(name="sticker", invoke_without_command=True)
    @has_permissions(manage_expressions=True)
    async def sticker(self, ctx: Context) -> Message:
        """
        Various sticker related commands.
        """

        return await ctx.send_help(ctx.command)

    @sticker.command(
        name="add",
        aliases=["create", "upload"],
        example="new sticker"
    )
    @has_permissions(manage_expressions=True)
    async def sticker_add(
        self,
        ctx: Context,
        name: Optional[Range[str, 2, 32]],
    ) -> Optional[Message]:
        """
        Add a sticker to the server.
        """

        if not ctx.message.stickers or not (sticker := ctx.message.stickers[0]):
            return await ctx.send_help(ctx.command)

        if len(ctx.guild.stickers) == ctx.guild.sticker_limit:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.add.STICKER_LIMIT", ctx)
            )

        sticker = await sticker.fetch()
        if not isinstance(sticker, GuildSticker):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.DEFAULT_ERROR", ctx)
            )

        try:
            await ctx.guild.create_sticker(
                name=name or sticker.name,
                description=sticker.description,
                emoji=sticker.emoji,
                file=File(BytesIO(await sticker.read())),
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except RateLimited as exc:
            retry_after = timedelta(seconds=exc.retry_after)
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.sticker.add.RATELIMITED",
                    ctx,
                    retry_after=precisedelta(retry_after)
                )
            )
        except HTTPException as exc:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.add.FAILED", ctx),
                codeblock(exc.text)
            )

        return await ctx.check()

    @sticker.command(name="tag")
    @has_permissions(manage_expressions=True)
    async def sticker_tag(self, ctx: Context):
        """Rename all stickers to the server vanity."""
        with self.bot.tracer.start_span("sticker_tag_all") as span:
            try:
                if not ctx.guild.vanity_url_code:
                    return await ctx.warn("The server doesn't have a vanity URL!")

                stickers_to_update = [
                    sticker for sticker in ctx.guild.stickers
                    if ctx.guild.vanity_url_code not in sticker.name
                ]

                if not stickers_to_update:
                    return await ctx.warn("All stickers already have the vanity tag!")

                span.set_attribute("stickers_to_update", len(stickers_to_update))
                initial_message = await ctx.neutral(f"Updating {len(stickers_to_update)} stickers...")

                batch_size = 5
                updated_stickers = 0
                failed_stickers = []

                async with ctx.typing():
                    for i in range(0, len(stickers_to_update), batch_size):
                        batch = stickers_to_update[i:i + batch_size]
                        
                        async def update_sticker(sticker):
                            try:
                                await sticker.edit(
                                    name=f"{sticker.name} /{ctx.guild.vanity_url_code}",
                                    reason=f"Updated by {ctx.author} ({ctx.author.id})"
                                )
                                return {'success': True, 'sticker': sticker}
                            except RateLimited as exc:
                                return {
                                    'success': False,
                                    'error': 'ratelimit',
                                    'retry_after': exc.retry_after,
                                    'sticker': sticker
                                }
                            except HTTPException as e:
                                return {
                                    'success': False,
                                    'error': 'http',
                                    'details': str(e),
                                    'sticker': sticker
                                }

                        results = await asyncio.gather(
                            *[update_sticker(sticker) for sticker in batch],
                            return_exceptions=True
                        )

                        for result in results:
                            if isinstance(result, Exception):
                                failed_stickers.append(result.sticker)
                                continue

                            if result['success']:
                                updated_stickers += 1
                            elif result['error'] == 'ratelimit':
                                await initial_message.edit(
                                    content=await ctx.bot.get_text(
                                        "moderation.sticker.tag.RATELIMITED",
                                        ctx,
                                        count=updated_stickers,
                                        retry_after=format_timespan(int(result['retry_after']))
                                    )
                                )
                                await asyncio.sleep(result['retry_after'])
                                stickers_to_update.extend(batch[results.index(result):])
                                break
                            else:
                                failed_stickers.append(result['sticker'])
                                span.record_exception(Exception(result['details']))

                        if i % (batch_size * 2) == 0:
                            await initial_message.edit(
                                content=await ctx.bot.get_text(
                                    "moderation.sticker.tag.PROGRESS",
                                    ctx,
                                    current=updated_stickers,
                                    total=len(stickers_to_update)
                                )
                            )
                        
                        await asyncio.sleep(0.5)

                STICKER_METRICS.labels(
                    action="tag_update",
                    guild_id=str(ctx.guild.id)
                ).inc(updated_stickers)

                span.set_attribute("stickers_updated", updated_stickers)
                span.set_attribute("stickers_failed", len(failed_stickers))

                if failed_stickers:
                    return await ctx.warn(
                        await ctx.bot.get_text(
                            "moderation.sticker.tag.FAILED",
                            ctx,
                            failed=len(failed_stickers),
                            success=updated_stickers,
                            total=len(stickers_to_update)
                        ),
                        patch=initial_message
                    )

                return await ctx.approve(
                    await ctx.bot.get_text(
                        "moderation.sticker.tag.SUCCESS",
                        ctx,
                        count=updated_stickers
                    ),
                    patch=initial_message
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    # @sticker.command(
    #     name="steal",
    #     aliases=["grab"],
    #     example="new sticker"
    # )
    # @has_permissions(manage_expressions=True)
    # async def sticker_steal(
    #     self,
    #     ctx: Context,
    #     name: Optional[Range[str, 2, 32]] = None,
    # ) -> Optional[Message]:
    #     """
    #     Steal a sticker from a message.
    #     """

    #     message: Optional[Message] = ctx.replied_message
    #     if not message:
    #         async for _message in ctx.channel.history(limit=25, before=ctx.message):
    #             if _message.stickers:
    #                 message = _message
    #                 break

    #     if not message:
    #         return await ctx.warn(
    #             "I couldn't find a message with a sticker in the past 25 messages!"
    #         )

    #     if not message.stickers:
    #         return await ctx.warn("That message doesn't have any stickers!")

    #     if len(ctx.guild.stickers) == ctx.guild.sticker_limit:
    #         return await ctx.warn(
    #             "The server is at the **maximum** amount of stickers!"
    #         )

    #     sticker = await message.stickers[0].fetch()

    #     if not isinstance(sticker, GuildSticker):
    #         return await ctx.warn("Stickers cannot be default stickers!")

    #     if sticker.guild_id == ctx.guild.id:
    #         return await ctx.warn("That sticker is already in this server!")

    #     try:
    #         await ctx.guild.create_sticker(
    #             name=name or sticker.name,
    #             description=sticker.description,
    #             emoji=sticker.emoji,
    #             file=File(BytesIO(await sticker.read())),
    #             reason=f"Created by {ctx.author} ({ctx.author.id})",
    #         )
    #     except RateLimited as exc:
    #         retry_after = timedelta(seconds=exc.retry_after)
    #         return await ctx.warn(
    #             f"The server is currently ratelimited, try again in **{precisedelta(retry_after)}**!"
    #         )

    #     except HTTPException as exc:
    #         return await ctx.warn("Failed to create the sticker!", codeblock(exc.text))

    #     return await ctx.check()

    @sticker.command(
        name="rename",
        aliases=["name"],
        example="new sticker"
    )
    @has_permissions(manage_expressions=True)
    async def sticker_rename(
        self,
        ctx: Context,
        *,
        name: str,
    ) -> Message:
        """
        Rename an existing sticker.
        """

        sticker = None
        if ctx.message.stickers:
            sticker = ctx.message.stickers[0]
        elif ctx.replied_message and ctx.replied_message.stickers:
            sticker = ctx.replied_message.stickers[0]

        if not sticker:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.rename.NO_STICKER", ctx)
            )

        sticker = await sticker.fetch()
        if not isinstance(sticker, GuildSticker):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.DEFAULT_ERROR", ctx)
            )

        if sticker.guild_id != ctx.guild.id:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.NOT_IN_SERVER", ctx)
            )

        elif len(name) < 2:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.rename.NAME_TOO_SHORT", ctx)
            )

        name = name[:32]
        await sticker.edit(
            name=name,
            reason=f"Updated by {ctx.author} ({ctx.author.id})",
        )

        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.sticker.rename.SUCCESS",
                ctx,
                name=name
            )
        )

    @sticker.command(
        name="delete",
        aliases=["remove", "del"],
    )
    @has_permissions(manage_expressions=True)
    async def sticker_delete(
        self,
        ctx: Context,
    ) -> Optional[Message]:
        """
        Delete an existing sticker.
        """

        if not (sticker := ctx.message.stickers[0]):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.delete.NO_STICKER", ctx)
            )

        sticker = await sticker.fetch()
        if not isinstance(sticker, GuildSticker):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.delete.DEFAULT_ERROR", ctx)
            )

        if sticker.guild_id != ctx.guild.id:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.delete.NOT_IN_SERVER", ctx)
            )

        await sticker.delete(reason=f"Deleted by {ctx.author} ({ctx.author.id})")
        return await ctx.check()

    @sticker.command(
        name="steal",
        aliases=["grab"],
        example="new sticker"
    )
    @has_permissions(manage_expressions=True)
    async def sticker_steal(
        self,
        ctx: Context,
        name: Optional[Range[str, 2, 32]] = None,
    ) -> Optional[Message]:
        """Steal a sticker from a message."""
        message: Optional[Message] = ctx.replied_message
        if not message:
            async for _message in ctx.channel.history(limit=25, before=ctx.message):
                if _message.stickers:
                    message = _message
                    break

        if not message:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.steal.NO_MESSAGE", ctx)
            )

        if not message.stickers:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.steal.NO_STICKERS", ctx)
            )

        if len(ctx.guild.stickers) == ctx.guild.sticker_limit:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.steal.STICKER_LIMIT", ctx)
            )

        sticker = await message.stickers[0].fetch()
        if not isinstance(sticker, GuildSticker):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.steal.DEFAULT_ERROR", ctx)
            )

        if sticker.guild_id == ctx.guild.id:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.steal.ALREADY_EXISTS", ctx)
            )

        try:
            await ctx.guild.create_sticker(
                name=name or sticker.name,
                description=sticker.description,
                emoji=sticker.emoji,
                file=File(BytesIO(await sticker.read())),
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except RateLimited as exc:
            retry_after = timedelta(seconds=exc.retry_after)
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.sticker.steal.RATELIMITED",
                    ctx,
                    retry_after=precisedelta(retry_after)
                )
            )
        except HTTPException as exc:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.steal.FAILED", ctx),
                codeblock(exc.text)
            )

        return await ctx.check()

    @sticker.command(
        name="archive",
        aliases=["zip"],
    )
    @has_permissions(manage_expressions=True)
    @cooldown(1, 30, BucketType.guild)
    async def sticker_archive(self, ctx: Context) -> Message:
        """
        Archive all stickers into a zip file.
        """

        if ctx.guild.premium_tier < 2:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.sticker.archive.PREMIUM_REQUIRED", ctx)
            )

        await ctx.neutral(
            await ctx.bot.get_text("moderation.sticker.archive.STARTING", ctx)
        )

        async with ctx.typing():
            buffer = BytesIO()
            with ZipFile(buffer, "w") as zip:
                for index, sticker in enumerate(ctx.guild.stickers):
                    name = f"{sticker.name}.{sticker.format}"
                    if name in zip.namelist():
                        name = f"{sticker.name}_{index}.{sticker.format}"

                    __buffer = await sticker.read()

                    zip.writestr(name, __buffer)

            buffer.seek(0)

        if ctx.response:
            with suppress(HTTPException):
                await ctx.response.delete()

        return await ctx.send(
            file=File(
                buffer,
                filename=f"{ctx.guild.name}_stickers.zip",
            ),
        )

    @group(
        name="set",
        aliases=["edit"],
        invoke_without_command=True,
    )
    @has_permissions(manage_guild=True)
    async def guild_set(self, ctx: Context) -> Message:
        """
        Various server related commands.
        """
        return await ctx.send_help(ctx.command)

    @guild_set.command(
        name="name",
        aliases=["n"],
        example="new server"
    )
    @has_permissions(manage_guild=True)
    async def guild_set_name(
        self,
        ctx: Context,
        *,
        name: Range[str, 1, 100],
    ) -> Optional[Message]:
        """
        Change the server's name.
        """

        try:
            await ctx.guild.edit(
                name=name,
                reason=f"{ctx.author} ({ctx.author.id})",
            )
        except HTTPException:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.guild_set.name.FAILED", ctx)
            )

        return await ctx.check()

    @guild_set.command(
        name="icon",
        aliases=[
            "pfp",
            "i",
        ],
        example="https://example.com/icon.png",
    )
    @has_permissions(manage_guild=True)
    async def guild_set_icon(
        self,
        ctx: Context,
        attachment: PartialAttachment = parameter(
            default=PartialAttachment.fallback,
        ),
    ) -> Optional[Message]:
        """
        Change the server's icon.
        """

        if not attachment.is_image():
            return await ctx.warn(
                await ctx.bot.get_text("moderation.guild_set.icon.NOT_IMAGE", ctx)
            )

        await ctx.guild.edit(
            icon=attachment.buffer,
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.check()

    @guild_set.command(
        name="splash",
        aliases=["background", "bg"],
        example="https://example.com/splash.png",
    )
    @has_permissions(manage_guild=True)
    async def guild_set_splash(
        self,
        ctx: Context,
        attachment: PartialAttachment = parameter(
            default=PartialAttachment.fallback,
        ),
    ) -> Optional[Message]:
        """
        Change the server's splash.
        """

        if not attachment.is_image():
            return await ctx.warn(
                await ctx.bot.get_text("moderation.guild_set.splash.NOT_IMAGE", ctx)
            )

        await ctx.guild.edit(
            splash=attachment.buffer,
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.check()

    @guild_set.command(
        name="banner",
        aliases=["b"],
        example="https://example.com/banner.png",
    )
    @check(lambda ctx: bool(ctx.guild and ctx.guild.premium_tier >= 2))
    @has_permissions(manage_guild=True)
    async def guild_set_banner(
        self,
        ctx: Context,
        attachment: PartialAttachment = parameter(
            default=PartialAttachment.fallback,
        ),
    ) -> Optional[Message]:
        """
        Change the server's banner.
        """

        if not attachment.is_image():
            return await ctx.warn(
                await ctx.bot.get_text("moderation.guild_set.banner.NOT_IMAGE", ctx)
            )

        await ctx.guild.edit(
            banner=attachment.buffer,
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.check()

    @guild_set.group(
        name="system",
        aliases=["sys"],
        invoke_without_command=True,
        example="#staff",
    )
    @has_permissions(manage_guild=True)
    async def guild_set_system(
        self,
        ctx: Context,
        *,
        channel: TextChannel,
    ) -> None:
        """
        Change the server's system channel.
        """

        await ctx.guild.edit(
            system_channel=channel,
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.check()

    @guild_set_system.group(
        name="welcome",
        aliases=["welc"],
        invoke_without_command=True,
    )
    @has_permissions(manage_guild=True)
    async def guild_set_system_welcome(self, ctx: Context) -> Message:
        """Toggle integrated welcome messages."""
        flags = ctx.guild.system_channel_flags
        flags.join_notifications = not flags.join_notifications

        await ctx.guild.edit(
            system_channel_flags=flags,
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.guild_set.system.welcome.SUCCESS",
                ctx,
                status="Now" if flags.join_notifications else "No longer"
            )
        )

    @guild_set_system_welcome.command(name="sticker", aliases=["stickers", "wave"])
    @has_permissions(manage_guild=True)
    async def guild_set_system_welcome_sticker(self, ctx: Context) -> Message:
        """Toggle replying with a welcome sticker."""
        flags = ctx.guild.system_channel_flags
        flags.join_notification_replies = not flags.join_notification_replies

        await ctx.guild.edit(
            system_channel_flags=flags,
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.guild_set.system.welcome.sticker.SUCCESS",
                ctx,
                status="Now" if flags.join_notification_replies else "No longer"
            )
        )

    @guild_set_system.command(name="boost", aliases=["boosts"])
    @has_permissions(manage_guild=True)
    async def guild_set_system_boost(self, ctx: Context) -> Message:
        """Toggle integrated boost messages."""
        flags = ctx.guild.system_channel_flags
        flags.premium_subscriptions = not flags.premium_subscriptions

        await ctx.guild.edit(
            system_channel_flags=flags,
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.guild_set.system.boost.SUCCESS",
                ctx,
                status="Now" if flags.premium_subscriptions else "No longer"
            )
        )

    @guild_set.command(
        name="notifications",
        aliases=["notis", "noti"],
        example="all",
    )
    @has_permissions(manage_guild=True)
    async def guild_set_notifications(
        self,
        ctx: Context,
        option: Literal["all", "mentions"],
    ) -> None:
        """
        Change the server's default notification settings.
        """

        await ctx.guild.edit(
            default_notifications=(
                NotificationLevel.all_messages
                if option == "all"
                else NotificationLevel.only_mentions
            ),
            reason=f"{ctx.author} ({ctx.author.id})",
        )
        return await ctx.check()

    @command()
    @has_permissions(manage_channels=True)
    async def nuke(self, ctx: Context) -> Message:
        """Clone the current channel."""
        channel = ctx.channel
        if not isinstance(channel, TextChannel):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.nuke.TEXT_ONLY", ctx)
            )

        await ctx.prompt(
            await ctx.bot.get_text("moderation.nuke.CONFIRM.TITLE", ctx),
            await ctx.bot.get_text("moderation.nuke.CONFIRM.DESCRIPTION", ctx),
        )

        new_channel = await channel.clone(
            reason=f"Nuked by {ctx.author} ({ctx.author.id})",
        )
        reconfigured = await self.reconfigure_settings(ctx.guild, channel, new_channel)
        await asyncio.gather(
            *[
                new_channel.edit(position=channel.position),
                channel.delete(reason=f"Nuked by {ctx.author} ({ctx.author.id})"),
            ]
        )

        embed = Embed(
            title=await ctx.bot.get_text("moderation.nuke.SUCCESS.TITLE", ctx),
            description=await ctx.bot.get_text(
                "moderation.nuke.SUCCESS.DESCRIPTION",
                ctx,
                author=ctx.author.mention
            ),
        )
        if reconfigured:
            embed.add_field(
                name=await ctx.bot.get_text("moderation.nuke.SUCCESS.RECONFIGURED", ctx),
                value="" + "\n".join(reconfigured),
            )

        await new_channel.send(embed=embed)
        return await new_channel.send("first")

    @command(example="3423346")
    @has_permissions(manage_messages=True)
    async def pin(self, ctx: Context, message: Optional[Message]) -> Optional[Message]:
        """Pin a specific message."""
        message = message or ctx.replied_message
        if not message:
            async for message in ctx.channel.history(limit=1, before=ctx.message):
                break

        if not message:
            return await ctx.send_help(ctx.command)

        elif message.guild != ctx.guild:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.pin.WRONG_GUILD", ctx)
            )

        elif message.pinned:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.pin.ALREADY_PINNED",
                    ctx,
                    url=message.jump_url
                )
            )

        await message.pin(reason=f"{ctx.author} ({ctx.author.id})")
        return await ctx.check()

    @command(example="232133425")
    @has_permissions(manage_messages=True)
    async def unpin(
        self,
        ctx: Context,
        message: Optional[Message],
    ) -> Optional[Message]:
        """
        Unpin a specific message.
        """

        message = message or ctx.replied_message
        if not message:
            return await ctx.send_help(ctx.command)

        elif message.guild != ctx.guild:
            return await ctx.warn("The message must be in this server!")

        elif not message.pinned:
            return await ctx.warn(
                f"That [`message`]({message.jump_url}) is not pinned!"
            )

        await message.unpin(reason=f"{ctx.author} ({ctx.author.id})")
        return await ctx.check()

    @hybrid_command()
    @has_permissions(administrator=True)
    async def strip(self, ctx: Context, user: Member, *, reason: str = "No reason provided"):
        """
        Strip a member of all dangerous permissions.
        """
        if isinstance(user, Member):
            await TouchableMember().check(ctx, user)

        if await self.is_immune(ctx, user):
            return

        dangerous_permissions = [
            'administrator',
            'manage_guild',
            'manage_roles',
            'manage_channels',
            'manage_webhooks',
            'manage_nicknames',
            'manage_emojis',
            'kick_members',
            'ban_members',
            'mention_everyone'
        ]

        roles_to_remove = []
        for role in user.roles[1:]:  
            for perm, value in role.permissions:
                if perm in dangerous_permissions and value:
                    roles_to_remove.append(role)
                    break

        if not roles_to_remove:
            return await ctx.warn(f"{user.mention} has no dangerous permissions to strip!")

        key = self.restore_key(ctx.guild, user)
        role_ids = [r.id for r in roles_to_remove if r.is_assignable()]
        await self.bot.redis.set(key, role_ids, ex=86400)

        try:
            await user.remove_roles(*roles_to_remove, reason=f"Stripped by {ctx.author} ({ctx.author.id}): {reason}")
        except discord.HTTPException:
            return await ctx.warn("Failed to strip roles from the user!")

        try:
            await ModConfig.sendlogs(self.bot, "strip", ctx.author, user, reason)
        except:
            pass

        return await ctx.check()
    
    @Mod.is_mod_configured()
    @hybrid_command()
    @has_permissions(manage_channels=True)
    async def jail(self, ctx: Context, user: Member, *, reason: str = "No reason provided"):
        """Jail a member."""
        with self.bot.tracer.start_span("member_jail") as span:
            try:
                if isinstance(user, Member):
                    await TouchableMember().check(ctx, user)

                if await self.is_immune(ctx, user):
                    return

                span.set_attribute("target_id", str(user.id))
                span.set_attribute("reason", reason)
                span.set_attribute("role_count", len(user.roles))

                initial_message = None
                if len(user.roles) > 10:
                    initial_message = await ctx.neutral(
                        await ctx.bot.get_text(
                            "moderation.jail.PROCESSING",
                            ctx,
                            user=user.mention,
                            role_count=len(user.roles)
                        )
                    )

                check_task = self.bot.db.fetchrow(
                    """
                    SELECT * FROM jail 
                    WHERE guild_id = $1 
                    AND user_id = $2
                    """,
                    ctx.guild.id,
                    user.id,
                )

                def process_roles(roles):
                    return [
                        r.id for r in roles 
                        if r.name != "@everyone" 
                        and r.is_assignable() 
                        and not r.is_premium_subscriber()
                    ]

                roles_task = self.bot.loop.run_in_executor(
                    None,
                    process_roles,
                    user.roles
                )

                mod_config_task = self.bot.db.fetchrow(
                    """
                    SELECT * FROM mod 
                    WHERE guild_id = $1
                    """,
                    ctx.guild.id,
                )

                check, roles_to_store, mod_config = await asyncio.gather(
                    check_task,
                    roles_task,
                    mod_config_task
                )

                if check:
                    if initial_message:
                        await initial_message.delete()
                    return await ctx.warn(
                        await ctx.bot.get_text(
                            "moderation.jail.ALREADY_JAILED",
                            ctx,
                            user=user.mention
                        )
                    )

                sql_as_text = json.dumps(roles_to_store)
                
                async with self.bot.pool.acquire() as conn:
                    async with conn.transaction():
                        await asyncio.gather(
                            conn.execute(
                                """
                                INSERT INTO jail 
                                (guild_id, user_id, roles) 
                                VALUES ($1, $2, $3)
                                """,
                                ctx.guild.id,
                                user.id,
                                sql_as_text,
                            ),
                        )

                jail_role = ctx.guild.get_role(mod_config["role_id"])
                if not jail_role:
                    raise ValueError(
                        await ctx.bot.get_text("moderation.jail.ROLE_NOT_FOUND", ctx)
                    )

                try:
                    if len(user.roles) > 10:
                        await initial_message.edit(
                            content=await ctx.bot.get_text("moderation.jail.REMOVING_ROLES", ctx)
                        )
                        
                    new_roles = [r for r in user.roles if not r.is_assignable()]
                    new_roles.append(jail_role)

                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            await user.edit(
                                roles=new_roles, 
                                reason=f"Jailed by {ctx.author} - {reason}"
                            )
                            break
                        except HTTPException as e:
                            if attempt == max_retries - 1:
                                raise
                            await asyncio.sleep(1)

                    notification_tasks = []
                    
                    notification_tasks.append(
                        ModConfig.sendlogs(self.bot, "jail", ctx.author, user, reason)
                    )

                    jail_channel = ctx.guild.get_channel(int(mod_config["jail_id"]))
                    if jail_channel:
                        notification_tasks.append(
                            jail_channel.send(
                                await ctx.bot.get_text(
                                    "moderation.jail.NOTIFICATION",
                                    ctx,
                                    user=user.mention
                                )
                            )
                        )

                    await asyncio.gather(*notification_tasks, return_exceptions=True)

                    MODERATION_METRICS.labels(
                        action="jail",
                        guild_id=str(ctx.guild.id)
                    ).inc()

                    span.set_attribute("success", True)
                    return await ctx.approve(
                        await ctx.bot.get_text(
                            "moderation.jail.SUCCESS",
                            ctx,
                            user=user,
                            reason=reason
                        ),
                        patch=initial_message if initial_message else None
                    )

                except Exception as e:
                    span.record_exception(e)
                    span.set_attribute("success", False)
                    return await ctx.warn(
                        await ctx.bot.get_text(
                            "moderation.jail.FAILED",
                            ctx,
                            user=user.mention,
                            error=str(e)
                        ),
                        patch=initial_message if initial_message else None
                    )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    @Mod.is_mod_configured()
    @hybrid_command()
    @has_permissions(manage_channels=True)
    async def unjail(self, ctx: Context, member: discord.Member, *, reason: str = "No reason provided"):
        """Unjail a member."""
        with self.bot.tracer.start_span("member_unjail") as span:
            try:
                span.set_attribute("target_id", str(member.id))
                span.set_attribute("reason", reason)

                initial_message = await ctx.neutral(f"Processing unjail for {member.mention}...")

                jail_query = self.bot.db.fetchrow(
                    """
                    SELECT * FROM jail 
                    WHERE guild_id = $1 
                    AND user_id = $2
                    """,
                    ctx.guild.id,
                    member.id,
                )

                mod_query = self.bot.db.fetchrow(
                    """
                    SELECT * FROM mod 
                    WHERE guild_id = $1
                    """,
                    ctx.guild.id
                )

                jail_entry, mod_config = await asyncio.gather(jail_query, mod_query)

                if not jail_entry:
                    return await ctx.warn(
                        f"**{member.mention}** is not jailed!",
                        patch=initial_message
                    )

                def process_roles(guild, role_ids):
                    return [
                        guild.get_role(role_id)
                        for role_id in json.loads(role_ids)
                        if (guild.get_role(role_id) and 
                            guild.get_role(role_id).is_assignable())
                    ]

                jail_role = ctx.guild.get_role(mod_config["role_id"])
                if not jail_role:
                    return await ctx.warn(
                        "Could not find the jail role!",
                        patch=initial_message
                    )

                roles_to_add = await self.bot.loop.run_in_executor(
                    None,
                    process_roles,
                    ctx.guild,
                    jail_entry["roles"]
                )

                span.set_attribute("roles_to_restore", len(roles_to_add))

                if len(roles_to_add) > 10:
                    await initial_message.edit(
                        content=f"Restoring {len(roles_to_add)} roles for {member.mention}..."
                    )

                async def update_member_roles():
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            await asyncio.gather(
                                member.edit(
                                    roles=roles_to_add,
                                    reason=f"Unjailed by {ctx.author} - {reason}"
                                ),
                                member.remove_roles(
                                    jail_role,
                                    reason=f"Unjailed by {ctx.author}"
                                )
                            )
                            return True
                        except discord.HTTPException as e:
                            if attempt == max_retries - 1:
                                span.record_exception(e)
                                return False
                            await asyncio.sleep(1)

                async with self.bot.pool.acquire() as conn:
                    async with conn.transaction():
                        db_cleanup = conn.execute(
                            """
                            DELETE FROM jail 
                            WHERE user_id = $1 
                            AND guild_id = $2
                            """,
                            member.id,
                            ctx.guild.id
                        )
                        
                        role_update = update_member_roles()
                        
                        results = await asyncio.gather(
                            db_cleanup,
                            role_update,
                            return_exceptions=True
                        )

                        if any(isinstance(r, Exception) for r in results):
                            raise Exception("Failed to complete unjail process")

                notification_tasks = [
                    ModConfig.sendlogs(self.bot, "unjail", ctx.author, member, reason)
                ]

                notification_results = await asyncio.gather(
                    *notification_tasks,
                    return_exceptions=True
                )

                for result in notification_results:
                    if isinstance(result, Exception):
                        span.record_exception(result)
                        await ctx.warn(f"Failed to send logs: {str(result)}")

                MODERATION_METRICS.labels(
                    action="unjail",
                    guild_id=str(ctx.guild.id)
                ).inc()

                span.set_attribute("success", True)
                return await ctx.approve(
                    f"Unjailed **{member.mention}** and restored {len(roles_to_add)} roles",
                    patch=initial_message
                )

            except Exception as e:
                span.record_exception(e)
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                return await ctx.warn(
                    f"Failed to unjail **{member.mention}**: {str(e)}",
                    patch=initial_message
                )

    @hybrid_command()
    @has_permissions(manage_guild=True, manage_channels=True, manage_roles=True, view_channel=True)
    async def setme(self, ctx: Context):
        """Enable the jail system in your server."""
        try:
            try:
                category = await ctx.guild.create_category(name="evict mod")
                role = await ctx.guild.create_role(name="evict-jail")

                for channel in ctx.guild.channels:
                    try:
                        await channel.set_permissions(role, view_channel=False)
                    except discord.Forbidden:
                        continue
                
                overwrite = {
                    role: discord.PermissionOverwrite(view_channel=True),
                    ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    ctx.guild.me: discord.PermissionOverwrite(view_channel=True)
                }

                text = await ctx.guild.create_text_channel(
                    name="mod-logs", 
                    overwrites=overwrite, 
                    category=category
                )

                jai = await ctx.guild.create_text_channel(
                    name="jail", 
                    overwrites=overwrite, 
                    category=category
                )
            except discord.Forbidden:
                return await ctx.warn(
                    await ctx.bot.get_text("moderation.setme.NO_PERMISSIONS", ctx)
                )
            except discord.HTTPException as e:
                return await ctx.warn(
                    await ctx.bot.get_text("moderation.setme.FAILED_CREATE", ctx, error=str(e))
                )

            await self.bot.db.execute(
                """
                INSERT INTO mod 
                VALUES ($1,$2,$3,$4)
                """,
                ctx.guild.id,
                text.id,
                jai.id,
                role.id,
            )
            await self.bot.db.execute("INSERT INTO cases VALUES ($1,$2)", ctx.guild.id, 0)
            
            return await ctx.approve(
                await ctx.bot.get_text("moderation.setme.SUCCESS", ctx)
            )
            
        except Exception as e:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.setme.ERROR", ctx, error=str(e))
            )

    @Mod.is_mod_configured()
    @hybrid_command()
    @has_permissions(manage_guild=True, manage_channels=True, manage_roles=True, view_channel=True)
    async def unsetme(self, ctx: Context):
        """Disable the jail system in your server."""
        check = await self.bot.db.fetchrow(
            "SELECT * FROM mod WHERE guild_id = $1",
            ctx.guild.id,
        )

        if not check:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.unsetme.NOT_ENABLED", ctx)
            )

        view = ClearMod(ctx)
        view.message = await ctx.send(
            view=view,
            embed=Embed(
                description=await ctx.bot.get_text("moderation.unsetme.CONFIRM", ctx)
            ),
        )

    @has_permissions(guild_owner=True)
    @group(invoke_without_command=True, aliases=["fp"])
    async def fakepermissions(self, ctx: Context) -> Message:
        """
        Restrict moderators to only use Evict for moderation.
        """
        return await ctx.send_help(ctx.command)

    @has_permissions(guild_owner=True)
    @fakepermissions.command(
        name="grant",
        aliases=["add"],
        example="@admin administrator"
    )
    async def fakepermissions_grant(
        self, ctx: Context, role: Role, *, permissions: str
    ):
        """
        Add one or more fake permissions to a role.
        > Can use multiple permissions at once if seperated by a comma (,).
        > https://docs.evict.bot/configuration/fakepermissions
        """

        permissions_list = [perm.strip().lower() for perm in permissions.split(",")]

        invalid_permissions = [
            perm
            for perm in permissions_list
            if perm not in map(str.lower, fake_permissions)
        ]
        if invalid_permissions:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.fakepermissions.grant.INVALID_PERMISSIONS",
                    ctx,
                    prefix=ctx.clean_prefix
                )
            )

        check = await self.bot.db.fetchrow(
            f"""
            SELECT permission 
            FROM {FAKE_PERMISSIONS_TABLE} 
            WHERE guild_id = $1 AND role_id = $2
            """,
            ctx.guild.id,
            role.id,
        )

        if check:
            perms = json.loads(check[0])
            already_has = [perm for perm in permissions_list if perm in perms]
            if already_has:
                already_has_str = ", ".join(f"`{perm}`" for perm in already_has)
                return await ctx.warn(
                    await ctx.bot.get_text(
                        "moderation.fakepermissions.grant.ALREADY_HAS",
                        ctx,
                        role=role.mention,
                        permissions=already_has_str
                    )
                )

            perms.extend(permissions_list)
            await self.bot.db.execute(
                f"""
                UPDATE {FAKE_PERMISSIONS_TABLE} 
                SET permission = $1 
                WHERE guild_id = $2 
                AND role_id = $3
                """,
                json.dumps(perms),
                ctx.guild.id,
                role.id,
            )
        else:
            await self.bot.db.execute(
                f"""
                INSERT INTO {FAKE_PERMISSIONS_TABLE} 
                (guild_id, role_id, permission) 
                VALUES ($1, $2, $3)
                """,
                ctx.guild.id,
                role.id,
                json.dumps(permissions_list),
            )

        added_permissions_str = ", ".join(f"`{perm}`" for perm in permissions_list)
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.fakepermissions.grant.SUCCESS",
                ctx,
                permissions=added_permissions_str,
                role=role.mention
            )
        )

    @has_permissions(guild_owner=True)
    @fakepermissions.command(
        name="revoke",
        aliases=["remove"],
        example="@mod administrator"
    )
    async def fakepermissions_remove(
        self, ctx: Context, role: Role, *, permissions: str
    ):
        """
        Remove one or more fake permissions from a role's fake permissions.
        > Can use multiple permissions at once if seperated by a comma (,).
        > https://docs.evict.bot/configuration/fakepermissions
        """

        permissions_list = [perm.strip().lower() for perm in permissions.split(",")]

        invalid_permissions = [
            perm
            for perm in permissions_list
            if perm not in map(str.lower, fake_permissions)
        ]
        if invalid_permissions:
            invalid_permissions_str = "\n".join(f"• `{perm}`" for perm in invalid_permissions)
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.fakepermissions.revoke.INVALID_PERMISSIONS",
                    ctx,
                    permissions=invalid_permissions_str
                )
            )

        check = await self.bot.db.fetchrow(
            f"""
            SELECT permission 
            FROM {FAKE_PERMISSIONS_TABLE} 
            WHERE guild_id = $1 
            AND role_id = $2
            """,
            ctx.guild.id,
            role.id,
        )

        if not check:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.fakepermissions.revoke.NO_PERMISSIONS",
                    ctx,
                    role=role.mention
                )
            )

        perms = json.loads(check[0])
        removed_permissions = []
        for permission in permissions_list:
            if permission in perms:
                perms.remove(permission)
                removed_permissions.append(permission)

        if not removed_permissions:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.fakepermissions.revoke.NOT_FOUND",
                    ctx,
                    role=role.mention
                )
            )

        if perms:
            await self.bot.db.execute(
                f"""
                UPDATE {FAKE_PERMISSIONS_TABLE} 
                SET permission = $1 
                WHERE guild_id = $2 
                AND role_id = $3
                """,
                json.dumps(perms),
                ctx.guild.id,
                role.id,
            )
        else:
            await self.bot.db.execute(
                f"""
                DELETE FROM {FAKE_PERMISSIONS_TABLE} 
                WHERE guild_id = $1 
                AND role_id = $2
                """,
                ctx.guild.id,
                role.id,
            )

        removed_permissions_str = ", ".join(f"`{perm}`" for perm in removed_permissions)
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.fakepermissions.revoke.SUCCESS",
                ctx,
                permissions=removed_permissions_str,
                role=role.mention
            )
        )

    @fakepermissions.command(name="list", aliases=["ls"])
    @has_permissions(guild_owner=True)
    async def fakepermission_list(self, ctx: Context) -> Message:
        """View all fake permissions for the server."""
        records = await self.bot.db.fetch(
            """
            SELECT role_id, permission
            FROM fake_permissions
            WHERE guild_id = $1
            """,
            ctx.guild.id,
        )

        fake_permissions = []
        for record in records:
            role = ctx.guild.get_role(record["role_id"])
            if role:
                permissions = json.loads(record["permission"])
                for perm in permissions:
                    fake_permissions.append(
                        await ctx.bot.get_text(
                            "moderation.fakepermissions.list.ENTRY",
                            ctx,
                            role=role.mention,
                            permission=perm
                        )
                    )

        if not fake_permissions:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.fakepermissions.list.NONE_EXIST", ctx)
            )

        paginator = Paginator(
            ctx,
            entries=fake_permissions,
            embed=Embed(
                title=await ctx.bot.get_text("moderation.fakepermissions.list.TITLE", ctx)
            ),
        )

        return await paginator.start()

    @has_permissions(guild_owner=True)
    @fakepermissions.command(name="permissions", aliases=["perms"])
    async def fakepermissions_permissions(self, ctx: Context):
        """
        Get every valid permission that can be used for fake permissions.
        """

        embed = Embed(title="Valid Fake Permissions")
        embed.description = "\n".join(f"• `{perm}`" for perm in fake_permissions)

        paginator = Paginator(ctx, entries=[embed], embed=embed)

        return await paginator.start()

    @command(example="@x")
    @has_permissions(manage_guild=True)
    async def modhistory(self, ctx: Context, moderator: Member = None):
        """View moderation history for a moderator."""
        moderator = moderator or ctx.author

        cases = await self.bot.db.fetch(
            """
            SELECT * FROM history.moderation 
            WHERE moderator_id = $1 AND guild_id = $2 
            ORDER BY case_id DESC
            """,
            moderator.id,
            ctx.guild.id,
        )

        if not cases:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.modhistory.NO_HISTORY",
                    ctx,
                    moderator=moderator.mention
                )
            )

        entries = []
        for case in cases:
            duration_str = (
                await ctx.bot.get_text(
                    "moderation.modhistory.DURATION",
                    ctx,
                    duration=humanize.naturaldelta(case['duration'])
                )
                if case["duration"]
                else ""
            )
            timestamp = f"<t:{int(case['timestamp'].timestamp())}:f>"

            entries.append(
                await ctx.bot.get_text(
                    "moderation.modhistory.CASE_ENTRY",
                    ctx,
                    case_id=case['case_id'],
                    action=case['action'],
                    user_id=case['user_id'],
                    timestamp=timestamp,
                    reason=case['reason'],
                    duration=duration_str
                )
            )

        embed = Embed(
            title=await ctx.bot.get_text(
                "moderation.modhistory.TITLE",
                ctx,
                moderator=moderator
            )
        )
        embed.set_footer(
            text=await ctx.bot.get_text(
                "moderation.modhistory.FOOTER",
                ctx,
                count=len(cases)
            )
        )

        paginator = Paginator(
            ctx,
            entries=entries,
            per_page=3,
            embed=embed,
            hide_index=True,
        )
        return await paginator.start()

    @command(example="@x")
    @has_permissions(manage_guild=True)
    async def history(self, ctx: Context, user: Member | User = None):
        """View moderation history for a user."""
        user = user or ctx.author

        cases = await self.bot.db.fetch(
            """
            SELECT * FROM history.moderation 
            WHERE user_id = $1 AND guild_id = $2 
            ORDER BY case_id DESC
            """,
            user.id,
            ctx.guild.id,
        )

        if not cases:
            return await ctx.warn(f"No moderation history found for {user.mention}")

        entries = []
        for case in cases:
            duration_str = (
                f"\nDuration: {humanize.naturaldelta(case['duration'])}"
                if case["duration"]
                else ""
            )
            timestamp = f"<t:{int(case['timestamp'].timestamp())}:f>"

            entries.append(
                f"**Case #{case['case_id']}**\n"
                f"Action: {case['action']}\n"
                f"Moderator: `{case['moderator_id']}`\n"
                f"Date: {timestamp}\n"
                f"Reason: {case['reason']}"
                f"{duration_str}"
            )

        embed = Embed(title=f"History for {user}")
        embed.set_footer(text=f"{len(cases)} total cases")

        paginator = Paginator(
            ctx,
            entries=entries,
            per_page=3,
            embed=embed,
            hide_index=True,
        )
        return await paginator.start()

    @command(aliases=["pic", "pictureperms", "picture"], example="#general @x")
    @has_permissions(manage_roles=True)
    async def picperms(self, ctx: Context, channel: Optional[TextChannel], user: Member):
        """Toggle picture permissions for a user."""
        if channel is None:
            channel = ctx.channel

        if isinstance(user, Member):
            await TouchableMember().check(ctx, user)

        perms = channel.permissions_for(user)
        pic_perms = perms.attach_files and perms.embed_links

        if pic_perms:
            await channel.set_permissions(user, attach_files=False, embed_links=False)
            await ctx.approve(
                await ctx.bot.get_text(
                    "moderation.picperms.REVOKED",
                    ctx,
                    user=user.mention,
                    channel=channel.mention
                )
            )
        else:
            await channel.set_permissions(user, attach_files=True, embed_links=True)
            await ctx.approve(
                await ctx.bot.get_text(
                    "moderation.picperms.GRANTED",
                    ctx,
                    user=user.mention,
                    channel=channel.mention
                )
            )

    @group(name="warn", invoke_without_command=True, example="@x annoying")
    @has_permissions(moderate_members=True)
    @Mod.is_mod_configured()
    async def warn(
        self,
        ctx: Context,
        member: Member,
        *,
        reason: str = "No reason provided"
    ) -> Message:
        """Warn a member."""
        
        if isinstance(member, Member):
            await TouchableMember().check(ctx, member)

        if await self.is_immune(ctx, member):
            return
        
        warn_count = await self.bot.db.fetchval(
            """
            SELECT COUNT(*) FROM history.moderation 
            WHERE guild_id = $1 AND user_id = $2 AND action = 'warn'
            """,
            ctx.guild.id, member.id
        )
        
        action = await self.bot.db.fetchrow(
            """
            SELECT action, threshold, duration 
            FROM warn_actions 
            WHERE guild_id = $1 AND threshold = $2
            """,
            ctx.guild.id, warn_count + 1
        )

        await ModConfig.sendlogs(
            self.bot,
            "warn",
            ctx.author,
            member,
            reason
        )

        if not action:
            return await ctx.approve(f"Warned {member.mention}")

        duration = timedelta(seconds=action['duration']) if action['duration'] else None
        
        if action['action'] == 'timeout':
            await member.timeout(duration, reason="Warn threshold reached")
        elif action['action'] == 'jail':
            pass
        elif action['action'] == 'ban':
            await member.ban(reason="Warn threshold reached", delete_message_days=0)
        elif action['action'] == 'softban':
            await member.ban(reason="Warn threshold reached", delete_message_days=7)
            await member.unban(reason="Softban complete")
        elif action['action'] == 'kick':
            await member.kick(reason="Warn threshold reached")

        await ModConfig.sendlogs(
            self.bot,
            action['action'],
            ctx.guild.me,
            member,
            "Warn threshold reached",
            duration
        )

        return await ctx.approve(
            f"Warned {member.mention} (Threshold reached: {action['action']})"
        )

    @warn.group(name="action", invoke_without_command=True)
    @has_permissions(manage_guild=True)
    async def warn_action(self, ctx: Context) -> Message:
        """Manage automated warn actions."""
        return await ctx.send_help(ctx.command)

    @warn_action.command(name="add", example="ban --threshold 3")
    @has_permissions(manage_guild=True)
    async def warn_action_add(
        self,
        ctx: Context,
        action: str,
        *,
        flags: WarnActionFlags
    ) -> Message:
        """Add an automated action for warn thresholds."""
        
        valid_actions = ["timeout", "jail", "ban", "softban", "kick"]
        action = action.lower()
        
        if action not in valid_actions:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.warn_action.add.INVALID_ACTION",
                    ctx,
                    actions=", ".join(valid_actions)
                )
            )
        
        await self.bot.db.execute(
            """
            INSERT INTO warn_actions (guild_id, threshold, action, duration)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, threshold) 
            DO UPDATE SET action = $3, duration = $4
            """,
            ctx.guild.id, flags.threshold, action, 
            int(flags.duration.total_seconds()) if flags.duration else None
        )

        duration_text = f"{humanize.naturaldelta(flags.duration)}" if flags.duration else ""
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.warn_action.add.SUCCESS",
                ctx,
                threshold=flags.threshold,
                action=action,
                duration=duration_text
            )
        )

    @warn_action.command(name="remove", example="3")
    @has_permissions(manage_guild=True)
    async def warn_action_remove(
        self,
        ctx: Context,
        threshold: int
    ) -> Message:
        """Remove an automated action for a warn threshold."""
        
        deleted = await self.bot.db.execute(
            """
            DELETE FROM warn_actions 
            WHERE guild_id = $1 AND threshold = $2
            """,
            ctx.guild.id, threshold
        )
        
        if deleted == "DELETE 0":
            return await ctx.warn(f"No action configured for threshold {threshold}")
        
        return await ctx.approve(f"Removed action for threshold {threshold}")

    @warn_action.command(name="list")
    @has_permissions(manage_guild=True)
    async def warn_action_list(self, ctx: Context) -> Message:
        """List all configured warn actions."""
        
        actions = await self.bot.db.fetch(
            """
            SELECT threshold, action, duration 
            FROM warn_actions 
            WHERE guild_id = $1 
            ORDER BY threshold
            """,
            ctx.guild.id
        )
        
        if not actions:
            return await ctx.warn("No warn actions configured")

        embed = Embed(title="Warn Actions")
        
        for action in actions:
            duration = timedelta(seconds=action['duration']) if action['duration'] else None
            embed.add_field(
                name=f"Threshold: {action['threshold']}",
                value=f"Action: {action['action']}\n" +
                      (f"Duration: {humanize.naturaldelta(duration)}" if duration else ""),
                inline=True
            )

        return await ctx.send(embed=embed)

    @warn.command(name="remove", aliases=["delete", "del"], example="1 Resolved")
    @has_permissions(moderate_members=True)
    async def warn_remove(
        self,
        ctx: Context,
        case_id: Range[int, 1, None],
        *,
        reason: str = "No reason provided"
    ) -> Message:
        """Remove a warning by case ID."""
        
        warn = await self.bot.db.fetchrow(
            """
            DELETE FROM history.moderation 
            WHERE guild_id = $1 AND case_id = $2 AND action = 'warn'
            RETURNING user_id
            """,
            ctx.guild.id, case_id
        )
        
        if not warn:
            return await ctx.warn(f"No warning found with case ID #{case_id}")
            
        try:
            user = await self.bot.fetch_user(warn['user_id'])
            user_text = f"{user.mention} (`{user.id}`)"
        except:
            user_text = f"`{warn['user_id']}`"

        await ModConfig.sendlogs(
            self.bot,
            "warn remove",
            ctx.author,
            user,
            f"#{case_id} removed. {reason}"
        )

        return await ctx.approve(f"Removed warning case #{case_id} from {user_text}")      

    @group(name="chunkban", aliases=["cb"], example="10")
    @has_permissions(ban_members=True)
    async def chunkban(self, ctx: Context, amount: Annotated[int, Range[int, 2, 100]] = 10) -> Optional[Message]:
        """Ban a certain number of newest members from the server."""
        if await self.bot.redis.ratelimited(f"chunkban:{ctx.guild.id}", 1, 180):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.chunkban.RATELIMIT", ctx)
            )

        if not ctx.guild.chunked:
            await ctx.guild.chunk(cache=True)

        config = await Settings.fetch(self.bot, ctx.guild)
        if not config.is_trusted(ctx.author):
            await ctx.warn(
                await ctx.bot.get_text("moderation.chunkban.TRUST_REQUIRED", ctx)
            )
            return False

        members = sorted(
            ctx.guild.members,
            key=lambda member: (member.joined_at or ctx.guild.created_at),
            reverse=True,
        )

        banned = members[:amount]

        if not banned:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.chunkban.NO_MEMBERS", ctx)
            )

        await ctx.prompt(
            await ctx.bot.get_text(
                "moderation.chunkban.CONFIRM",
                ctx,
                amount=amount
            )
        )

        banned_count = 0
        async with ctx.typing():
            for member in banned:
                if member.bot:
                    continue

                try:
                    await member.ban(reason=f"{ctx.author} / Chunkban")
                    await asyncio.sleep(2)
                    banned_count += 1
                except HTTPException:
                    continue

        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.chunkban.SUCCESS",
                ctx,
                banned=banned_count,
                total=len(banned)
            )
        )

    @chunkban.command(name="avatars", aliases=["defaultavatars"])
    @has_permissions(ban_members=True)
    async def chunkban_avatars(self, ctx: Context):
        """
        Ban members with default avatars.
        """
        if await self.bot.redis.ratelimited(f"chunkban:{ctx.guild.id}", 1, 180):
            return await ctx.warn(
                await ctx.bot.get_text("moderation.chunkban.RATELIMIT", ctx)
            )

        if not ctx.guild.chunked:
            await ctx.guild.chunk(cache=True)

        config = await Settings.fetch(self.bot, ctx.guild)
        if not config.is_trusted(ctx.author):
            await ctx.warn(
                await ctx.bot.get_text("moderation.chunkban.TRUST_REQUIRED", ctx)
            )
            return False

        members = [member for member in ctx.guild.members if member.default_avatar]

        if not members:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.chunkban.avatars.NO_MEMBERS", ctx)
            )

        await ctx.prompt(
            await ctx.bot.get_text("moderation.chunkban.avatars.CONFIRM", ctx)
        )

        async with ctx.typing():
            for member in members:
                try:
                    await member.ban(reason=f"{ctx.author} / Chunkban: Default Avatar")
                    await asyncio.sleep(2)
                except HTTPException:
                    continue

        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.chunkban.avatars.SUCCESS",
                ctx,
                count=plural(len(members), md='**')
            )
        )
    
    @group(name="immune", invoke_without_command=True)
    async def immune(self, ctx: Context):
        """
        Make a user or role immune to moderation actions.
        """
        return await ctx.send_help(ctx.command)
    
    @immune.group(name="add", invoke_without_command=True)
    async def immune_add(self, ctx: Context):
        """
        Add user or role to immune list.
        """
        return await ctx.send_help(ctx.command)
    
    @immune_add.command(name="user", aliases=["member"])
    @has_permissions(manage_guild=True)
    async def immune_add_user(self, ctx: Context, member: Member):
        """Add user to immune list."""
        immune = await self.bot.db.fetchrow(
            """
            SELECT * FROM immune 
            WHERE guild_id = $1 
            AND entity_id = $2 
            AND type = 'user'
            """,
            ctx.guild.id, 
            member.id
        )

        if immune:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.immune.add.user.ALREADY_IMMUNE",
                    ctx,
                    name=member.name
                )
            )
        
        await self.bot.db.execute(
            """
            INSERT INTO immune 
            (guild_id, entity_id, type)
            VALUES 
            ($1, $2, 'user')
            """,
            ctx.guild.id, 
            member.id
        )
        
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.immune.add.user.SUCCESS",
                ctx,
                name=member.name
            )
        )
    
    @immune_add.command(name="role")
    @has_permissions(manage_guild=True)
    async def immune_add_role(self, ctx: Context, role: Role):
        """Add role to immune list."""
        immune = await self.bot.db.fetchrow(
            """
            SELECT * FROM immune 
            WHERE guild_id = $1 
            AND role_id = $2 
            AND type = 'role'
            """,
            ctx.guild.id, 
            role.id
        )

        if immune:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.immune.add.role.ALREADY_IMMUNE",
                    ctx,
                    name=role.name
                )
            )

        await self.bot.db.execute(
            """
            INSERT INTO immune 
            (guild_id, entity_id, role_id, type)
            VALUES 
            ($1, $2, $2, 'role')
            """,
            ctx.guild.id, 
            role.id
        )
        
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.immune.add.role.SUCCESS",
                ctx,
                mention=role.mention
            )
        )

    @immune.group(name="remove", invoke_without_command=True)
    async def immune_remove(self, ctx: Context):
        """
        Remove user or role from immune list.
        """
        return await ctx.send_help(ctx.command)
    
    @immune_remove.command(name="user", aliases=["member"])
    @has_permissions(manage_guild=True)
    async def immune_remove_user(self, ctx: Context, member: Member):
        """Remove user from immune list."""
        immune = await self.bot.db.fetchrow(
            """
            SELECT * FROM immune 
            WHERE guild_id = $1 
            AND entity_id = $2 
            AND type = 'user'
            """,
            ctx.guild.id, 
            member.id
        )

        if not immune:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.immune.remove.user.NOT_IMMUNE",
                    ctx,
                    name=member.name
                )
            )

        await self.bot.db.execute(
            """
            DELETE FROM immune 
            WHERE guild_id = $1 
            AND entity_id = $2 
            AND type = 'user'
            """,
            ctx.guild.id, 
            member.id
        )
        
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.immune.remove.user.SUCCESS",
                ctx,
                mention=member.mention
            )
        )
    
    @immune_remove.command(name="role")
    @has_permissions(manage_guild=True)
    async def immune_remove_role(self, ctx: Context, role: Role):
        """Remove role from immune list."""
        immune = await self.bot.db.fetchrow(
            """
            SELECT * FROM immune 
            WHERE guild_id = $1 
            AND role_id = $2 
            AND type = 'role'
            """,
            ctx.guild.id, 
            role.id
        )

        if not immune:
            return await ctx.warn(
                await ctx.bot.get_text(
                    "moderation.immune.remove.role.NOT_IMMUNE",
                    ctx,
                    name=role.name
                )
            )

        await self.bot.db.execute(
            """
            DELETE FROM immune 
            WHERE guild_id = $1 
            AND role_id = $2 
            AND type = 'role'
            """,
            ctx.guild.id, 
            role.id
        )
        
        return await ctx.approve(
            await ctx.bot.get_text(
                "moderation.immune.remove.role.SUCCESS",
                ctx,
                mention=role.mention
            )
        )
    
    @immune.command(name="list")
    @has_permissions(manage_guild=True)
    async def immune_list(self, ctx: Context):
        """List all immune users and roles."""
        immune = await self.bot.db.fetch(
            """
            SELECT entity_id, role_id, type 
            FROM immune 
            WHERE guild_id = $1
            """,
            ctx.guild.id
        )
        
        if not immune:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.immune.list.NONE", ctx)
            )
        
        entries = []
        for record in immune:
            entity_id = record['entity_id']
            role_id = record['role_id']
            type = record['type']
            
            if type == 'user':
                entries.append(f"<@{entity_id}>")
            elif type == 'role' and role_id is not None:
                entries.append(f"<@&{role_id}>")
        
        if not entries:
            return await ctx.warn(
                await ctx.bot.get_text("moderation.immune.list.NONE", ctx)
            )

        paginator = Paginator(
            ctx,
            entries=entries,
            embed=Embed(
                title=await ctx.bot.get_text("moderation.immune.list.TITLE", ctx)
            )
        )
        
        return await paginator.start()