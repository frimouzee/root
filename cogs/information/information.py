from __future__ import annotations

from itertools import groupby
from typing import Optional, Literal
import aiohttp
import json
from uuid import UUID
from datetime import datetime, timezone, timedelta
import asyncio

from discord import (
    ActivityType,
    Colour,
    Embed,
    TextChannel,
    CategoryChannel,
    VoiceChannel,
    Guild,
    Invite,
    ButtonStyle,
    Member,
    Message,
    PartialInviteGuild,
    Permissions,
    Role,
    Spotify,
    Status,
    Streaming,
    User,
    app_commands,
    Interaction,
    TextStyle,
    SelectOption
)
from discord.ext.commands import (
    hybrid_command,
    hybrid_group,
    command,
    Cog,
    Group,
    has_permissions,
    parameter,
    flag
)
from discord.ui import Button, View, Modal, TextInput, Select
from discord.utils import format_dt, oauth_url, utcnow
from humanfriendly import format_size
from humanize import ordinal
from psutil import Process
from typing import Union
from typing import Annotated
from json import dumps

import config
import git
import discord
import time
from pathlib import Path

from main import Evict

from utils.tools import dominant_color
from core.context import Context
from core import FlagConverter
from utils.formatter import human_join, plural, short_timespan
from utils.converters.basic import Location
from managers.paginator import Paginator

import logging
logger = logging.getLogger(__name__)

REPO_PATH = "/root/evict.new/.git"

class PollFlags(FlagConverter):
    title: str = flag(
        description="The title of the poll"
    )
    description: str = flag(
        description="The description/question of the poll"
    )
    duration: Optional[str] = flag(
        default=None,
        description="How long the poll should last (e.g. 1h, 1d)"
    )
    anonymous: bool = flag(
        default=False,
        description="Whether votes should be anonymous"
    )
    multiple_choice: bool = flag(
        default=False,
        description="Whether multiple options can be selected"
    )


class Information(Cog):
    def __init__(self, bot: Evict):
        self.bot = bot
        self.process = Process()
        self.weather_key = "64581e6f1d7d49ae834142709230804"
        self.description = "View information on various things."
        self._cached_commit = None
        self._cached_lines = None
        self._cached_files = None
        self._cached_imports = None
        self._cached_functions = None
        self._cached_commands = None
        self._cached_member_count = None
        self._last_cache_time = 0
        
        self._cached_cluster_stats = None
        self._last_cluster_stats_time = 0

    async def _update_cluster_stats(self):
        """Update cached cluster stats if needed"""
        current_time = time.time()
        if current_time - self._last_cluster_stats_time < 300:
            return self._cached_cluster_stats

        try:
            responses = await self.bot.ipc.broadcast("get_cluster_stats")
            logger.info(f"Raw responses: {responses}")
            
            if not responses:
                logger.warning("No responses received from clusters")
                stats = {
                    'total_guilds': len(self.bot.guilds),
                    'total_members': self._cached_member_count
                }
            else:
                stats = {
                    'total_guilds': sum(response['guild_count'] for response in responses.values()),
                    'total_members': sum(response['member_count'] for response in responses.values())
                }
                logger.info(f"Calculated totals - Guilds: {stats['total_guilds']}, Members: {stats['total_members']}")

            self._cached_cluster_stats = stats
            self._last_cluster_stats_time = current_time
            return stats

        except Exception as e:
            logger.warning(f"Failed to get cluster stats: {e}", exc_info=True)
            return {
                'total_guilds': len(self.bot.guilds),
                'total_members': self._cached_member_count
            }

    async def _update_cache(self):
        current_time = time.time()
        if (current_time - self._last_cache_time < 7200 and 
            all(x is not None for x in [
                self._cached_lines,
                self._cached_files, 
                self._cached_imports,
                self._cached_functions,
                self._cached_commit,
                self._cached_commands,
                self._cached_member_count
            ])):
            return
        
        try:
            repo = git.Repo(REPO_PATH)
            commit = repo.head.commit
            self._cached_commit = commit.hexsha[:7]
            
            self._cached_lines = sum(
                len(open(p, encoding='utf-8').readlines()) 
                for p in Path('.').rglob('*.py') 
                if not any(x in str(p) for x in [
                    '.venv', '.git', '__pycache__', 
                    '.pytest_cache', 'build', 'dist', 
                    '.eggs', '*.egg-info'
                ])
            )
            
            self._cached_files = len([
                p for p in Path('.').rglob('*.py') 
                if not any(x in str(p) for x in [
                    '.venv', '.git', '__pycache__', 
                    '.pytest_cache', 'build', 'dist', 
                    '.eggs', '*.egg-info'
                ])
            ])
            
            self._cached_imports = len(set(sum([list(mod.__dict__.keys()) for mod in [discord, config]], [])))
            
            self._cached_functions = len([f for f in dir(self.bot) if callable(getattr(self.bot, f)) and not f.startswith('_')])
            
            self._cached_commands = len([cmd for cmd in self.bot.walk_commands() 
                                      if cmd.cog_name not in ('Jishaku', 'Owner')])
            
            self._cached_member_count = sum(g.member_count for g in self.bot.guilds)
            
            self._last_cache_time = current_time
        except Exception as e:
            print(f"[Cache] Error updating cache: {str(e)}")

    @Cog.listener("on_guild_update")
    async def guild_name_listener(self, before: Guild, after: Guild):
        if before.name != after.name:
            await self.bot.db.execute(
                """
                INSERT INTO gnames (guild_id, name, changed_at) 
                VALUES ($1, $2, $3)
                """, 
                before.id, 
                before.name,
                datetime.now()
            )

    @Cog.listener("on_user_update")
    async def name_history_listener(self, before: User, after: User) -> None:
        if before.name == after.name and before.global_name == after.global_name:
            return

        await self.bot.db.execute(
            """
            INSERT INTO name_history (user_id, username)
            VALUES ($1, $2)
            """,
            after.id,
            (
                before.name
                if after.name != before.name
                else (before.global_name or before.name)
            ),
        )

    @Cog.listener()
    async def on_member_unboost(self, member: Member) -> None:
        if not member.premium_since:
            return

        await self.bot.db.execute(
            """
            INSERT INTO boosters_lost (guild_id, user_id, lasted_for)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET lasted_for = EXCLUDED.lasted_for
            """,
            member.guild.id,
            member.id,
            utcnow() - member.premium_since,
        )

    @hybrid_command(aliases=["beep"])
    async def ping(self, ctx: Context) -> None:
        """View the bot's latency."""
        latency = round(self.bot.latency * 1000)
        start_time = time.time()
        message = await ctx.neutral(
            await self.bot.get_text("information.ping.INITIAL_EMBED.description", ctx)
        )
        end_time = time.time()
        edit_latency = round((end_time - start_time) * 1000)
        
        return await ctx.neutral(
            message=await self.bot.get_text(
                "information.ping.UPDATED_EMBED.description",
                ctx,
                ping=latency,
                edit_ping=edit_latency
            ),
            patch=message
        )

    @hybrid_command()
    async def shards(self, ctx: Context):
        """
        View the bot shard latency.
        """

        embed = Embed(title=f"Total shards [{self.bot.shard_count}]")

        for shard in self.bot.shards:
            guilds = [g for g in self.bot.guilds if g.shard_id == shard]
            users = sum([g.member_count for g in guilds])
            shard_indicator = "<:connection:1300775066933530755>" if ctx.guild.shard_id == shard else ""
            embed.add_field(
                name=f"Shard {shard} {shard_indicator}",
                value=f"**ping**: ``{round(self.bot.shards.get(shard).latency * 1000)}ms``\n**guilds**: ``{len(guilds)}``\n**users**: ``{users:,}``",
                inline=True,
            )
            embed.set_footer(text=f"You are on Shard {ctx.guild.shard_id}", icon_url=f"{self.bot.user.display_avatar.url}")

        await ctx.send(embed=embed)

    @command(aliases=["inv"])
    async def invite(self, ctx: Context) -> Message:
        """
        Get an invite link for the bot.
        """

        view = View()
        view.add_item(
            Button(
                url=config.CLIENT.INVITE_URL,
                style=ButtonStyle.link,
                emoji=config.EMOJIS.SOCIAL.WEBSITE,
            )
        )

        return await ctx.send(view=view)

    @command(aliases=["discord"])
    async def support(self, ctx: Context) -> Message:
        """
        Get an invite link for the bot's support server.
        """

        view = View()
        view.add_item(
            Button(
                url=config.CLIENT.SUPPORT_URL,
                style=ButtonStyle.link,
                emoji=config.EMOJIS.SOCIAL.DISCORD,
            )
        )

        return await ctx.send(view=view)

    @hybrid_command(name="about", aliases=["botinfo", "bi"])
    async def about(self, ctx: Context) -> Message:
        """View information about the bot."""
        await asyncio.gather(
            self._update_cache(),
            self._update_cluster_stats()
        )

        regular_cogs = len(self.bot.cogs)
        extensions = len(self.bot.extensions) 
        total_modules = regular_cogs + extensions

        stats = self._cached_cluster_stats or {
            'total_guilds': len(self.bot.guilds),
            'total_members': self._cached_member_count
        }

        embed = Embed(
            description=await self.bot.get_text(
                "information.about.EMBED.DESCRIPTION_LINE_ONE",
                ctx
            ) + "\n" + await self.bot.get_text(
                "information.about.EMBED.DESCRIPTION_LINE_TWO",
                ctx,
                cached_commands=self._cached_commands,
                total_cogs=len(self.bot.cogs),
                extended_cogs=total_modules
            )
        )
        embed.set_author(
            name=self.bot.user.name,
            icon_url=self.bot.user.display_avatar.url,
            url=config.CLIENT.SUPPORT_URL
            or oauth_url(self.bot.user.id, permissions=Permissions(permissions=8)),
        )

        uptime_dt = datetime.fromtimestamp(self.bot.uptime2)

        embed.add_field(
            name="**Bot**",
            value="\n".join([
                f"**Users:** `{stats['total_members']:,}`",
                f"**Servers:** `{stats['total_guilds']:,}`",
                f"**Created:** <t:{int(self.bot.user.created_at.timestamp())}:R>"
            ]),
            inline=True
        )

        embed.add_field(
            name="**System**",
            value="\n".join([
                f"**CPU:** `{self.process.cpu_percent()}%`",
                f"**Memory:** `{format_size(self.process.memory_info().rss)}`",
                f"**Launched:** {format_dt(uptime_dt, 'R')}"
            ]),
            inline=True
        )

        embed.add_field(
            name="**Code**",
            value="\n".join([
                f"**Lines:** `{self._cached_lines:,}`",
                f"**Files:** `{self._cached_files:,}`",
                f"**Imports:** `{self._cached_imports:,}`",
                f"**Functions:** `{self._cached_functions:,}`"
            ]),
            inline=True
        )

        button1 = Button(
            label="GitHub",
            style=discord.ButtonStyle.gray,
            emoji=config.EMOJIS.SOCIAL.GITHUB,
            url="https://github.com/x32u",
        )

        button2 = Button(
            label="Support",
            style=discord.ButtonStyle.gray,
            emoji=config.EMOJIS.SOCIAL.DISCORD,
            url="https://discord.gg/evict",
        )

        button3 = Button(
            label="Website",
            style=discord.ButtonStyle.gray,
            emoji=config.EMOJIS.SOCIAL.WEBSITE,
            url="https://evict.bot",
        )

        view = discord.ui.View()
        view.add_item(button1)
        view.add_item(button2)
        view.add_item(button3)

        embed.set_footer(
            text=await self.bot.get_text(
                "information.about.EMBED.FOOTER",
                ctx,
                version=self.bot.version,
                latest_commit=self._cached_commit
            )
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        return await ctx.send(embed=embed, view=view)

    @app_commands.command(name='botinfo')
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def botinfo_slash(self, interaction: Interaction):
        """View information about the bot."""
        ctx = await Context.from_interaction(interaction)
        await self.about(ctx)

    @app_commands.command(name='botinfo')
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def botinfo_slash(self, interaction: Interaction):
        """View information about the bot."""
        ctx = await Context.from_interaction(interaction)
        await self.about(ctx)

    @command(example="evict", aliases=["ii"])
    async def inviteinfo(self, ctx: Context, *, invite: Invite) -> Message:
        """View information about an invite."""
        guild = invite.guild
        embed = Embed(
            description=f"{format_dt(guild.created_at)} ({format_dt(guild.created_at, 'R')})"
        )
        embed.set_author(
            name=await self.bot.get_text(
                "information.inviteinfo.EMBED.AUTHOR",
                ctx,
                guild_name=guild.name,
                guild_id=guild.id
            ),
            url=invite.url,
            icon_url=guild.icon,
        )
        if guild.icon:
            buffer = await guild.icon.read()
            embed.color = await dominant_color(buffer)

        embed.add_field(
            name=await self.bot.get_text("information.inviteinfo.EMBED.FIELDS.INFORMATION.NAME", ctx),
            value=await self.bot.get_text(
                "information.inviteinfo.EMBED.FIELDS.INFORMATION.VALUE",
                ctx,
                inviter=invite.inviter or 'Vanity URL',
                channel=invite.channel or 'Unknown',
                created_at=format_dt(invite.created_at or guild.created_at)
            ),
        )
        embed.add_field(
            name=await self.bot.get_text("information.inviteinfo.EMBED.FIELDS.GUILD.NAME", ctx),
            value=await self.bot.get_text(
                "information.inviteinfo.EMBED.FIELDS.GUILD.VALUE",
                ctx,
                member_count=invite.approximate_member_count,
                online_count=invite.approximate_presence_count,
                verification_level=guild.verification_level.name.title()
            ),
        )

        return await ctx.send(embed=embed)

    @command(example="evict", aliases=["sbanner"])
    async def serverbanner(
        self,
        ctx: Context,
        *,
        invite: Optional[Invite],
    ) -> Message:
        """
        View a server's banner if one is present.
        """

        guild = (
            invite.guild
            if isinstance(invite, Invite)
            and isinstance(invite.guild, PartialInviteGuild)
            else ctx.guild
        )
        if not guild.banner:
            return await ctx.warn(await self.bot.get_text("information.server.BANNER_MISSING", ctx, guild=guild))

        embed = Embed(
            url=guild.banner,
            title=f"{guild}'s banner",
        )
        embed.set_image(url=guild.banner)

        return await ctx.send(embed=embed)

    @command(example="evict", aliases=["sicon"])
    async def servericon(
        self,
        ctx: Context,
        *,
        invite: Optional[Invite],
    ) -> Message:
        """
        View a server's icon if one is present.
        """

        guild = (
            invite.guild
            if isinstance(invite, Invite)
            and isinstance(invite.guild, PartialInviteGuild)
            else ctx.guild
        )
        if not guild.icon:
            return await ctx.warn(await self.bot.get_text("information.server.ICON_MISSING", ctx, guild=guild))

        embed = Embed(
            url=guild.icon,
            title=f"{guild}'s icon",
        )
        embed.set_image(url=guild.icon)

        return await ctx.send(embed=embed)

    @hybrid_command(
        aliases=[
            "pfp",
            "avi",
            "av",
        ],
        example="@x",
        with_app_command=True,
        brief="View a user's avatar.",
        fallback="view"
    )
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @discord.app_commands.default_permissions(use_application_commands=True)
    async def avatar(
        self,
        ctx: Context,
        *,
        user: Member | User = parameter(
            default=lambda ctx: ctx.author,
        ),
    ) -> Message:
        """
        View a user's avatar.
        """

        embed = Embed(
            url=user.avatar or user.default_avatar,
            title=await self.bot.get_text(
                "information.avatar.TITLE.SELF" if user == ctx.author 
                else "information.avatar.TITLE.OTHER",
                ctx,
                user_name=user.name
            ),
        )
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        embed.set_image(url=user.avatar or user.default_avatar)

        return await ctx.send(embed=embed)

    @hybrid_command(
        aliases=[
            "spfp",
            "savi",
            "sav",
        ],
        example="@x",
    )
    async def serveravatar(
        self,
        ctx: Context,
        *,
        member: Member = parameter(
            default=lambda ctx: ctx.author,
        ),
    ) -> Message:
        """
        View a user's avatar.
        """

        member = member or ctx.author
        if not member.guild_avatar:
            return await ctx.warn(
                await self.bot.get_text(
                    "information.serveravatar.MISSING.SELF" if member == ctx.author
                    else "information.serveravatar.MISSING.OTHER",
                    ctx,
                    member=member
                )
            )

        embed = Embed(
            url=member.guild_avatar,
            title=await self.bot.get_text(
                "information.serveravatar.TITLE.SELF" if member == ctx.author
                else "information.serveravatar.TITLE.OTHER",
                ctx,
                member_name=member.name
            ),
        )
        embed.set_image(url=member.guild_avatar)

        return await ctx.send(embed=embed)

    @hybrid_command(
        aliases=[
            "mb",
        ],
        example="@x",
    )
    async def memberbanner(
        self,
        ctx: Context,
        *,
        member: Member = parameter(
            default=lambda ctx: ctx.author,
        ),
    ) -> Message:
        """
        View a user's server banner.
        """

        member = member or ctx.author
        if not member.guild_banner:
            return await ctx.warn(
                await self.bot.get_text(
                    "information.memberbanner.MISSING.SELF" if member == ctx.author
                    else "information.memberbanner.MISSING.OTHER",
                    ctx,
                    member=member
                )
            )

        embed = Embed(
            url=member.guild_banner,
            title=await self.bot.get_text(
                "information.memberbanner.TITLE.SELF" if member == ctx.author
                else "information.memberbanner.TITLE.OTHER",
                ctx,
                member_name=member.name
            ),
        )
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        embed.set_image(url=member.guild_banner)

        return await ctx.send(embed=embed)

    @hybrid_command(aliases=["userbanner", "ub"], example="@x", with_app_command=True, brief="View a user's banner.", fallback="view")
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @discord.app_commands.default_permissions(use_application_commands=True)
    async def banner(
        self,
        ctx: Context,
        *,
        user: Member | User = parameter(
            default=lambda ctx: ctx.author,
        ),
    ) -> Message:
        """View a user's banner if one is present."""

        fetched_user = await self.bot.fetch_user(user.id)

        if not fetched_user.banner:
            return await ctx.warn(
                await self.bot.get_text(
                    "information.banner.MISSING.SELF" if user == ctx.author
                    else "information.banner.MISSING.OTHER",
                    ctx,
                    user=user
                )
            )

        embed = Embed(
            url=fetched_user.banner.url,
            title="Your banner" if user == ctx.author else f"{user.name}'s banner",
        )
        embed.set_image(url=fetched_user.banner.url)
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

        return await ctx.send(embed=embed)

    @command(aliases=["mc"], example="evict")
    async def membercount(
        self,
        ctx: Context,
        *,
        guild: Optional[Guild],
    ) -> Message:
        """
        View the member count of a server.
        """

        guild = guild or ctx.guild
        embed = Embed()
        embed.set_author(
            name=guild,
            icon_url=guild.icon,
        )

        humans = list(list(filter(lambda member: not member.bot, guild.members)))
        bots = list(list(filter(lambda member: member.bot, guild.members)))

        embed.add_field(
            name=await self.bot.get_text("information.membercount.FIELDS.MEMBERS", ctx), 
            value=f"{len(guild.members):,}"
        )
        embed.add_field(
            name=await self.bot.get_text("information.membercount.FIELDS.HUMANS", ctx), 
            value=f"{len(humans):,}"
        )
        embed.add_field(
            name=await self.bot.get_text("information.membercount.FIELDS.BOTS", ctx), 
            value=f"{len(bots):,}"
        )

        return await ctx.send(embed=embed)

    @hybrid_command(aliases=["sinfo", "si"], example="evict", with_app_command=True, brief="View server information.", fallback="view")
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @discord.app_commands.default_permissions(use_application_commands=True)
    async def serverinfo(
        self,
        ctx: Context,
        *,
        guild: str = None,
    ) -> Message:
        """
        View information about the server.
        """
        if guild:
            try:
                guild_id = int(guild)
                target_guild = self.bot.get_guild(guild_id)
            except ValueError:
                guild = guild.lower().strip()
                
                if 'discord.gg/' in guild:
                    invite_code = guild.split('discord.gg/')[-1]
                else:
                    invite_code = guild
                    
                try:
                    invite = await self.bot.fetch_invite(invite_code)
                    target_guild = invite.guild
                except (discord.NotFound, discord.HTTPException):
                    target_guild = discord.utils.get(self.bot.guilds, name=guild)
        else:
            target_guild = (
                self.bot.get_guild(892675627373699072)  
                if isinstance(ctx.channel, discord.DMChannel)
                else ctx.guild
            )

        if not target_guild:
            return await ctx.warn(await self.bot.get_text("information.serverinfo.ERRORS.NOT_FOUND", ctx))

        embed = Embed(
            description=f"{format_dt(target_guild.created_at)} ({format_dt(target_guild.created_at, 'R')})"
        )
        embed.set_author(
            name=f"{target_guild.name} ({target_guild.id})",
            url=target_guild.vanity_url,
            icon_url=target_guild.icon,
        )
        if target_guild.icon:
            buffer = await target_guild.icon.read()
            embed.color = await dominant_color(buffer)

        embed.add_field(
            name=await self.bot.get_text("information.serverinfo.EMBED.FIELDS.INFORMATION.NAME", ctx),
            value=await self.bot.get_text(
                "information.serverinfo.EMBED.FIELDS.INFORMATION.VALUE",
                ctx,
                owner=target_guild.owner or target_guild.owner_id,
                verification=target_guild.verification_level.name.title(),
                boost_count=target_guild.premium_subscription_count,
                boost_level=target_guild.premium_tier
            ),
        )
        embed.add_field(
            name=await self.bot.get_text("information.serverinfo.EMBED.FIELDS.STATISTICS.NAME", ctx),
            value=await self.bot.get_text(
                "information.serverinfo.EMBED.FIELDS.STATISTICS.VALUE",
                ctx,
                member_count=target_guild.member_count,
                text_channels=len(target_guild.text_channels),
                voice_channels=len(target_guild.voice_channels)
            ),
        )

        if target_guild == ctx.guild and (roles := target_guild.roles[1:]):
            roles = list(reversed(roles))

            embed.add_field(
                name=await self.bot.get_text(
                    "information.serverinfo.EMBED.FIELDS.ROLES.NAME",
                    ctx,
                    role_count=len(roles)
                ),
                value=(
                    ""
                    + ", ".join(role.mention for role in roles[:5])
                    + (f" (+{len(roles) - 5})" if len(roles) > 5 else "")
                ),
                inline=False,
            )

        return await ctx.send(embed=embed)

    @hybrid_command(aliases=["uinfo", "ui"], example="@x", with_app_command=True, brief="View information about a user.", fallback="view")
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @discord.app_commands.default_permissions(use_application_commands=True)
    async def userinfo(self, ctx: Context, *, user: Member | User = parameter(default=lambda ctx: ctx.author)) -> Message:
        embed = Embed(color=user.color if user.color != Colour.default() else ctx.color)
        embed.title = await self.bot.get_text("information.userinfo.TITLE", ctx, user=user, bot_tag="[BOT]" if user.bot else "")
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        embed.description = ""

        if isinstance(user, Member):
            support_guild = self.bot.get_guild(892675627373699072)
            if support_guild:
                if not support_guild.chunked:
                    await support_guild.chunk()
                support_member = support_guild.get_member(user.id)
            else:
                support_member = None
            
            badges = []
            staff_eligible = False

            if user is ctx.guild.owner:
                badges.append(f"{config.EMOJIS.BADGES.SERVER_OWNER}")
            
            if support_member:  
                if any(role.id == 1265473601755414528 for role in support_member.roles):
                    badges.extend([f"{config.EMOJIS.STAFF.DEVELOPER}", f"{config.EMOJIS.STAFF.OWNER}"])
                    staff_eligible = True
                    
                if any(role.id == 1264110559989862406 for role in support_member.roles):
                    badges.append(f"{config.EMOJIS.STAFF.SUPPORT}")
                    staff_eligible = True
                    
                if any(role.id == 1323255508609663098 for role in support_member.roles):
                    badges.append(f"{config.EMOJIS.STAFF.TRIAL}")
                    staff_eligible = True

                if any(role.id == 1325007612797784144 for role in support_member.roles):
                    badges.append(f"{config.EMOJIS.STAFF.MODERATOR}")
                    staff_eligible = True

                if any(role.id == 1318054098666389534 for role in support_member.roles):
                    badges.append(f"{config.EMOJIS.STAFF.DONOR}")
                    
                if any(role.id == 1320428924215496704 for role in support_member.roles):
                    badges.append(f"{config.EMOJIS.STAFF.INSTANCE}")
                
            if badges:
                if staff_eligible:
                    badges.append(f"{config.EMOJIS.STAFF.STAFF}")
                embed.description = f"{' '.join(badges)}"

        embed.set_thumbnail(url=user.display_avatar)
        embed.set_footer(text=await self.bot.get_text("information.userinfo.FOOTER", ctx, count=len(user.mutual_guilds)))

        embed.add_field(
            name=await self.bot.get_text("information.userinfo.FIELDS.CREATED", ctx),
            value=f"{format_dt(user.created_at, 'D')}\n> {format_dt(user.created_at, 'R')}",
        )

        if isinstance(user, Member) and user.joined_at:
            join_pos = sorted(user.guild.members, key=lambda member: member.joined_at or utcnow()).index(user)
            embed.add_field(
                name=await self.bot.get_text("information.userinfo.FIELDS.JOINED", ctx, position=ordinal(join_pos + 1)),
                value=f"{format_dt(user.joined_at, 'D')}\n> {format_dt(user.joined_at, 'R')}",
            )

            if user.premium_since:
                embed.add_field(
                    name=await self.bot.get_text("information.userinfo.FIELDS.BOOSTED", ctx),
                    value=f"{format_dt(user.premium_since, 'D')}\n> {format_dt(user.premium_since, 'R')}",
                )

            if roles := user.roles[1:]:
                embed.add_field(
                    name=await self.bot.get_text("information.userinfo.FIELDS.ROLES", ctx),
                    value=", ".join(role.mention for role in list(reversed(roles))[:5])
                    + (f" (+{len(roles) - 5})" if len(roles) > 5 else ""),
                    inline=False,
                )

            if (voice := user.voice) and voice.channel:
                members = len(voice.channel.members) - 1
                with_others = (
                    await self.bot.get_text("information.userinfo.VOICE.WITH_OTHERS", ctx, count=members)
                    if members else await self.bot.get_text("information.userinfo.VOICE.ALONE", ctx)
                )
                embed.description += await self.bot.get_text(
                    "information.userinfo.VOICE.STREAMING" if voice.self_stream else "information.userinfo.VOICE.NORMAL",
                    ctx,
                    channel=voice.channel.mention,
                    with_others=with_others
                )

            for activity_type, activities in groupby(user.activities, key=lambda activity: activity.type):
                activities = list(activities)
                if isinstance(activities[0], Spotify):
                    activity = activities[0]
                    embed.description += "\n" + await self.bot.get_text(
                        "information.userinfo.ACTIVITIES.SPOTIFY",
                        ctx,
                        title=activity.title,
                        url=activity.track_url,
                        artist=activity.artists[0]
                    )
                elif isinstance(activities[0], Streaming):
                    embed.description += "\n" + await self.bot.get_text(
                        "information.userinfo.ACTIVITIES.STREAMING",
                        ctx,
                        activities=human_join([f"[**{activity.name}**]({activity.url})" for activity in activities], final="and")
                    )
                elif activity_type == ActivityType.playing:
                    embed.description += "\n" + await self.bot.get_text(
                        "information.userinfo.ACTIVITIES.PLAYING",
                        ctx,
                        activities=human_join([f"**{activity.name}**" for activity in activities], final="and")
                    )
                elif activity_type == ActivityType.watching:
                    embed.description += "\n" + await self.bot.get_text(
                        "information.userinfo.ACTIVITIES.WATCHING",
                        ctx,
                        activities=human_join([f"**{activity.name}**" for activity in activities], final="and")
                    )

                elif activity_type == ActivityType.competing:
                    embed.description += "\n🏆 Competing in " + human_join(
                        [f"**{activity.name}**" for activity in activities],
                        final="and",
                    )

            embed.title += " "

        return await ctx.send(embed=embed)

    @hybrid_group(
        aliases=["names", "nh"],
        invoke_without_command=True,
        example="@x",
    )
    async def namehistory(
        self,
        ctx: Context,
        *,
        user: Member | User = parameter(
            default=lambda ctx: ctx.author,
        ),
    ) -> Message:
        """
        View a user's name history.
        """

        names = await self.bot.db.fetch(
            """
            SELECT *
            FROM name_history
            WHERE user_id = $1
            """
            + ("" if ctx.author.id in self.bot.owner_ids else "\nAND is_hidden = FALSE")
            + "\nORDER BY changed_at DESC",
            user.id,
        )
        if not names:
            return await ctx.warn(await self.bot.get_text("information.namehistory.NO_HISTORY", ctx, user=user))

        paginator = Paginator(
            ctx,
            entries=[
                f"**{record['username']}** ({format_dt(record['changed_at'], 'R')})"
                for record in names
            ],
            embed=Embed(title=await self.bot.get_text("information.namehistory.TITLE", ctx)),
        )
        return await paginator.start()

    @namehistory.command(
        name="clear",
        aliases=["clean", "reset"],
    )
    async def namehistory_clear(self, ctx: Context) -> Message:
        """
        Remove all your name history.
        """

        await self.bot.db.execute(
            """
            UPDATE name_history
            SET is_hidden = TRUE
            WHERE user_id = $1
            """,
            ctx.author.id,
        )

        return await ctx.approve(await self.bot.get_text("information.namehistory.clear.SUCCESS", ctx))

    @hybrid_command(
        aliases=[
            "device",
            "presence",
        ],
        example="@x",
    )
    async def devices(
        self,
        ctx: Context,
        *,
        member: Member = parameter(
            default=lambda ctx: ctx.author,
        ),
    ) -> Message:
        """
        View a member's platforms.
        """

        member = member or ctx.author
        if member.status == Status.offline:
            return await ctx.warn(
                await self.bot.get_text(
                    "information.devices.OFFLINE.SELF" if member == ctx.author 
                    else "information.devices.OFFLINE.OTHER",
                    ctx,
                    member=member
                )
            )

        emojis = {
            Status.offline: "⚪️",
            Status.online: "🟢",
            Status.idle: "🟡",
            Status.dnd: "🔴",
        }

        embed = Embed(
            title=await self.bot.get_text(
                "information.devices.TITLE.SELF" if member == ctx.author
                else "information.devices.TITLE.OTHER",
                ctx,
                member_name=member.name
            )
        )
        embed.description = ""

        for activity_type, activities in groupby(member.activities, key=lambda activity: activity.type):
            activities = list(activities)
            if isinstance(activities[0], Spotify):
                activity = activities[0]
                embed.description += "\n" + await self.bot.get_text(
                    "information.devices.ACTIVITIES.SPOTIFY",
                    ctx,
                    title=activity.title,
                    url=activity.track_url,
                    artist=activity.artists[0]
                )
            elif isinstance(activities[0], Streaming):
                embed.description += "\n" + await self.bot.get_text(
                    "information.devices.ACTIVITIES.STREAMING",
                    ctx,
                    activities=human_join([f"[**{activity.name}**]({activity.url})" for activity in activities], final="and")
                )
            elif activity_type == ActivityType.playing:
                embed.description += "\n" + await self.bot.get_text(
                    "information.devices.ACTIVITIES.PLAYING",
                    ctx,
                    activities=human_join([f"**{activity.name}**" for activity in activities], final="and")
                )

            elif activity_type == ActivityType.watching:
                embed.description += "\n" + await self.bot.get_text(
                    "information.devices.ACTIVITIES.WATCHING",
                    ctx,
                    activities=human_join([f"**{activity.name}**" for activity in activities], final="and")
                )

            elif activity_type == ActivityType.competing:
                embed.description += "\n" + await self.bot.get_text(
                    "information.devices.ACTIVITIES.COMPETING",
                    ctx,
                    activities=human_join([f"**{activity.name}**" for activity in activities], final="and")
                )

        embed.description += "\n" + "\n".join(
            [
                f"{emojis[status]} **{device}**"
                for device, status in {
                    "Mobile": member.mobile_status,
                    "Desktop": member.desktop_status,
                    "Browser": member.web_status,
                }.items()
                if status != Status.offline
            ]
        )

        return await ctx.send(embed=embed)

    @hybrid_command()
    async def roles(self, ctx: Context) -> Message:
        """
        View the server roles.
        """

        roles = reversed(ctx.guild.roles[1:])
        if not roles:
            return await ctx.warn(await self.bot.get_text("information.roles.NO_ROLES", ctx, guild=ctx.guild))

        paginator = Paginator(
            ctx,
            entries=[f"{role.mention} (`{role.id}`)" for role in roles],
            embed=Embed(title=await self.bot.get_text("information.roles.TITLE", ctx, guild=ctx.guild)),
        )
        return await paginator.start()

    @hybrid_command(example="@member")
    async def inrole(self, ctx: Context, *, role: Role) -> Message:
        members = role.members
        if not members:
            return await ctx.warn(await self.bot.get_text("information.inrole.NO_MEMBERS", ctx, role=role.mention))

        paginator = Paginator(
            ctx,
            entries=[f"{member.mention} (`{member.id}`)" for member in members],
            embed=Embed(title=await self.bot.get_text("information.inrole.TITLE", ctx, role=role)),
        )
        return await paginator.start()

    @hybrid_group(invoke_without_command=True)
    async def boosters(self, ctx: Context) -> Message:
        """
        View server boosters.
        """

        members = list(
            filter(
                lambda member: member.premium_since is not None,
                ctx.guild.members,
            )
        )
        if not members:
            return await ctx.warn(await self.bot.get_text("information.boosters.NO_BOOSTERS", ctx))

        paginator = Paginator(
            ctx,
            entries=[
                f"{member.mention} - boosted {format_dt(member.premium_since or utcnow(), 'R')}"
                for member in sorted(members, key=lambda member: member.premium_since or utcnow(), reverse=True)
            ],
            embed=Embed(title=await self.bot.get_text("information.boosters.TITLE", ctx)),
        )
        return await paginator.start()

    @boosters.command(name="lost")
    async def boosters_lost(self, ctx: Context) -> Message:
        """
        View all lost boosters.
        """
        users = [
            f"{user.mention} stopped {format_dt(record['ended_at'], 'R')} (lasted {short_timespan(record['lasted_for'])})"
            for record in await self.bot.db.fetch(
                "SELECT * FROM boosters_lost WHERE guild_id = $1 ORDER BY ended_at DESC",
                ctx.guild.id,
            )
            if (user := self.bot.get_user(record["user_id"]))
        ]
        if not users:
            return await ctx.warn(await self.bot.get_text("information.boosters.lost.NO_LOST", ctx))

        paginator = Paginator(
            ctx,
            entries=users,
            embed=Embed(title=await self.bot.get_text("information.boosters.lost.TITLE", ctx)),
        )
        return await paginator.start()

    @hybrid_command()
    async def bots(self, ctx: Context) -> Message:
        """
        View all bots in the server.
        """
        members = list(
            filter(
                lambda member: member.bot,
                ctx.guild.members,
            )
        )
        if not members:
            return await ctx.warn(await self.bot.get_text("information.bots.NO_BOTS", ctx, guild=ctx.guild))

        paginator = Paginator(
            ctx,
            entries=[
                f"{member.mention} (`{member.id}`)"
                for member in sorted(members, key=lambda member: member.joined_at or utcnow(), reverse=True)
            ],
            embed=Embed(title=await self.bot.get_text("information.bots.TITLE", ctx, guild=ctx.guild)),
        )
        return await paginator.start()
    
    @command(aliases=["bans"])
    @has_permissions(ban_members=True)
    async def banlist(self, ctx: Context) -> Message:
        """
        View all banned members.
        """
        bans = []
        async for ban in ctx.guild.bans():
            bans.append(ban)

        if not bans:
            return await ctx.warn(await self.bot.get_text("information.banlist.NO_BANS", ctx))

        paginator = Paginator(
            ctx,
            entries=[f"{ban.user.mention} (`{ban.user.id}`) - {ban.reason or 'No reason'}" for ban in bans],
            embed=Embed(title=await self.bot.get_text("information.banlist.TITLE", ctx, count=len(bans))),
        )
        
        return await paginator.start()

    @hybrid_command(aliases=["gi"])
    @has_permissions(manage_guild=True)
    async def guildinvites(self, ctx: Context) -> Message:
        """
        View all server invites.
        """
        invites = await ctx.guild.invites()
        if not invites:
            return await ctx.warn(await self.bot.get_text("information.guildinvites.NO_INVITES", ctx))

        paginator = Paginator(
            ctx,
            entries=[
                f"[{invite.code}]({invite.url}) by {invite.inviter.mention if invite.inviter else '**Unknown**'} expires {format_dt(invite.expires_at, 'R') if invite.expires_at else '**Never**'}"
                for invite in sorted(invites, key=lambda invite: invite.created_at or utcnow(), reverse=True)
            ],
            embed=Embed(title=await self.bot.get_text("information.guildinvites.TITLE", ctx, guild=ctx.guild)),
        )
        return await paginator.start()

    @hybrid_command(aliases=["emotes"])
    async def emojis(self, ctx: Context) -> Message:
        """
        View all server emojis.
        """

        emojis = ctx.guild.emojis
        if not emojis:
            return await ctx.warn(await self.bot.get_text("information.emojis.NO_EMOJIS", ctx, guild=ctx.guild))

        paginator = Paginator(
            ctx,
            entries=[f"{emoji} ([`{emoji.id}`]({emoji.url}))" for emoji in emojis],
            embed=Embed(title=await self.bot.get_text("information.emojis.TITLE", ctx, guild=ctx.guild)),
        )
        return await paginator.start()

    @hybrid_command()
    async def stickers(self, ctx: Context) -> Message:
        """
        View all server stickers.
        """
        stickers = ctx.guild.stickers
        if not stickers:
            return await ctx.warn(await self.bot.get_text("information.stickers.NO_STICKERS", ctx, guild=ctx.guild))

        paginator = Paginator(
            ctx,
            entries=[f"[{sticker.name}]({sticker.url}) (`{sticker.id}`)" for sticker in stickers],
            embed=Embed(title=await self.bot.get_text("information.stickers.TITLE", ctx, guild=ctx.guild)),
        )
        return await paginator.start()

    @hybrid_command(aliases=["firstmsg"])
    async def firstmessage(self, ctx: Context) -> Message:
        """
        View the first message sent.
        """
        message = [message async for message in ctx.channel.history(limit=1, oldest_first=True)][0]
        return await ctx.neutral(
            await self.bot.get_text("information.firstmessage.RESPONSE", ctx, url=message.jump_url, author=message.author)
        )

    @hybrid_command(aliases=["pos"], example="@x")
    async def position(self, ctx: Context, *, member: Member = None):
        """
        Check member join position.
        """
        if member is None:
            member = ctx.author

        pos = (
            sum(
                1
                for m in ctx.guild.members
                if m.joined_at is not None and m.joined_at < member.joined_at
            )
            + 1
        )

        embed = Embed(description=await self.bot.get_text("information.position.RESPONSE", ctx, member=member.mention, pos=pos))
        await ctx.send(embed=embed)

    @command(example="#general", aliases=["chinfo", "cinfo", "ci"])
    async def channelinfo(
        self,
        ctx: Context,
        channel: Optional[Union[TextChannel, VoiceChannel, CategoryChannel]],
    ):
        """
        View information about a channel.
        """
        if channel is None:
            channel = ctx.channel

        embed = Embed(title=await self.bot.get_text("information.channelinfo.TITLE", ctx, channel=channel.name))
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

        embed.add_field(
            name=await self.bot.get_text("information.channelinfo.FIELDS.ID", ctx),
            value=f"``{channel.id}``",
            inline=False
        )

        embed.add_field(name="Channel ID", value=f"``{channel.id}``", inline=False)

        embed.add_field(
            name=await self.bot.get_text("information.channelinfo.FIELDS.TYPE", ctx),
            value=f"``{str(channel.type).lower()}``",
            inline=False
        )

        if isinstance(channel, (TextChannel, VoiceChannel)):
            category = channel.category

            if category:
                embed.add_field(
                    name=await self.bot.get_text("information.channelinfo.FIELDS.CATEGORY", ctx),
                    value=f"``{category.name}`` (``{category.id}``)",
                    inline=False
                )
            else:
                embed.add_field(name="Category", value="No category", inline=False)

        if isinstance(channel, TextChannel):
            embed.add_field(
                name=await self.bot.get_text("information.channelinfo.FIELDS.TOPIC", ctx),
                value=channel.topic or await self.bot.get_text("information.channelinfo.NO_TOPIC", ctx),
                inline=False
            )

        elif isinstance(channel, CategoryChannel):
            child_channels = [child.name for child in channel.channels]
            if child_channels:
                embed.add_field(
                    name=f"{len(child_channels)} Children",
                    value=", ".join(child_channels),
                    inline=False,
                )

        embed.add_field(
            name="Created On",
        )
            
        embed.add_field(
            name=await self.bot.get_text("information.channelinfo.FIELDS.CREATED", ctx),
            value=f"{format_dt(channel.created_at)} ({format_dt(channel.created_at, 'R')})",
            inline=False
        )

        await ctx.send(embed=embed)

    @command(example="@owner", aliases=["rinfo"])
    async def roleinfo(self, ctx: Context, role: Optional[Role]):
        """
        View information about a role.
        """
        if role is None:
            role = ctx.author.top_role

        embed = Embed(title=await self.bot.get_text("information.roleinfo.TITLE", ctx, role=role.name))
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        
        embed.add_field(
            name=await self.bot.get_text("information.roleinfo.FIELDS.ID", ctx), 
            value=f"``{role.id}``", 
            inline=False
        )
        embed.add_field(
            name=await self.bot.get_text("information.roleinfo.FIELDS.COLOR", ctx), 
            value=f"``{role.color}``", 
            inline=False
        )

        specific_permissions = [
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
            "moderate_members",
        ]

        granted_permissions = [
            perm for perm in specific_permissions if getattr(role.permissions, perm)
        ]
        
        if granted_permissions:
            embed.add_field(
                name=await self.bot.get_text("information.roleinfo.FIELDS.PERMISSIONS.NAME", ctx),
                value=", ".join(granted_permissions) if len(granted_permissions) > 1 else granted_permissions[0],
                inline=False
            )
        else:
            embed.add_field(
                name=await self.bot.get_text("information.roleinfo.FIELDS.PERMISSIONS.NAME", ctx),
                value=await self.bot.get_text("information.roleinfo.FIELDS.PERMISSIONS.NONE", ctx),
                inline=False
            )

        members_with_role = role.members
        member_names = [member.name for member in members_with_role][:5]

        if member_names:
            embed.add_field(
                name=await self.bot.get_text("information.roleinfo.FIELDS.MEMBERS.NAME", ctx, count=len(role.members)),
                value=", ".join(member_names) if len(member_names) > 1 else member_names[0],
                inline=False
            )
        else:
            embed.add_field(
                name=await self.bot.get_text("information.roleinfo.FIELDS.MEMBERS.NAME", ctx, count=0),
                value=await self.bot.get_text("information.roleinfo.FIELDS.MEMBERS.NONE", ctx),
                inline=False
            )

        if role.icon:
            embed.set_thumbnail(url=role.icon.url)

        if granted_permissions:
            embed.set_footer(
                text=await self.bot.get_text("information.roleinfo.FOOTER.DANGEROUS", ctx),
                icon_url="https://cdn.discordapp.com/emojis/1308023743565529138.webp?size=64",
            )

        await ctx.send(embed=embed)

    @command(example="1203514684326805524", aliases=["gbi"])
    async def getbotinvite(self, ctx: Context, *, user: User):
        """
        Get a bots invite by providing the bots ID.
        """
        if not user.bot:
            return await ctx.warn(await self.bot.get_text("information.getbotinvite.NOT_BOT", ctx))

        button = Button(
            style=ButtonStyle.link,
            label=await self.bot.get_text("information.getbotinvite.BUTTON", ctx, name=user.name),
            url=f"https://discord.com/api/oauth2/authorize?client_id={user.id}&permissions=8&scope=bot%20applications.commands",
        )

        view = View()
        view.add_item(button)

        await ctx.send(view=view)

    @command(example="892675627373699072")
    async def gnames(self, ctx: Context, guild: Optional[Guild]):
        """
        View a guild's name history.
        """
        if not guild:
            guild = ctx.guild

        names = await self.bot.db.fetch(
            """
            SELECT name, changed_at
            FROM gnames
            WHERE guild_id = $1
            ORDER BY changed_at DESC
            """,
            guild.id,
        )
        
        if not names:
            return await ctx.warn(await self.bot.get_text("information.gnames.NO_HISTORY", ctx, guild=guild))

        paginator = Paginator(
            ctx,
            entries=[f"**{record['name']}** ({format_dt(record['changed_at'], 'R')})" for record in names],
            embed=Embed(title=await self.bot.get_text("information.gnames.TITLE", ctx, guild_name=guild.name)),
        )
        
        return await paginator.start()

    @command()
    @has_permissions(manage_guild=True)
    async def cleargnames(self, ctx: Context):
        await self.bot.db.execute("DELETE FROM gnames WHERE guild_id = $1", ctx.guild.id)
        await ctx.approve(await self.bot.get_text("information.cleargnames.SUCCESS", ctx))

    @hybrid_command(name="weather", with_app_command=True, brief="Get the current weather for a city/country", fallback="view")
    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @discord.app_commands.default_permissions(use_application_commands=True)
    async def weather(self, ctx: Context, *, location: str):
        """Get the current weather for a city/country"""
        
        location = location.replace(" ", "+")
        
        async with aiohttp.ClientSession() as session:
            try:
                url = f"https://wttr.in/{location}?format=j1"
                async with session.get(url) as response:
                    if response.status != 200:
                        return await ctx.warn(
                            await self.bot.get_text("information.weather.ERROR.NOT_FOUND", ctx, location=location)
                        )
                    
                    data = await response.json()
                    current = data['current_condition'][0]
                    
                    weather_emojis = {
                        'Sunny': '☀️',
                        'Clear': '🌙',
                        'Partly cloudy': '⛅',
                        'Cloudy': '☁️',
                        'Overcast': '☁️',
                        'Mist': '🌫️',
                        'Patchy rain': '🌦️',
                        'Light rain': '🌧️',
                        'Moderate rain': '🌧️',
                        'Heavy rain': '⛈️',
                        'Light snow': '🌨️',
                        'Moderate snow': '🌨️',
                        'Heavy snow': '❄️',
                        'Thunder': '⛈️'
                    }
                    
                    weather_desc = current['weatherDesc'][0]['value']
                    weather_emoji = weather_emojis.get(weather_desc, '🌡️')

                    embed = discord.Embed(
                        title=await self.bot.get_text(
                            "information.weather.EMBED.TITLE",
                            ctx,
                            location=data['nearest_area'][0]['areaName'][0]['value']
                        ),
                        description=f"{weather_emoji} {weather_desc}",
                        color=discord.Color.blue(),
                        timestamp=datetime.utcnow()
                    )
                    
                    embed.add_field(
                        name=await self.bot.get_text("information.weather.EMBED.FIELDS.TEMPERATURE.NAME", ctx),
                        value=await self.bot.get_text(
                            "information.weather.EMBED.FIELDS.TEMPERATURE.VALUE",
                            ctx,
                            temp_c=current['temp_C'],
                            temp_f=current['temp_F'],
                            feels_c=current['FeelsLikeC'],
                            feels_f=current['FeelsLikeF']
                        ),
                        inline=True
                    )
                    
                    embed.add_field(
                        name=await self.bot.get_text("information.weather.EMBED.FIELDS.HUMIDITY.NAME", ctx),
                        value=await self.bot.get_text(
                            "information.weather.EMBED.FIELDS.HUMIDITY.VALUE",
                            ctx,
                            humidity=current['humidity']
                        ),
                        inline=True
                    )
                    
                    embed.add_field(
                        name=await self.bot.get_text("information.weather.EMBED.FIELDS.WIND.NAME", ctx),
                        value=await self.bot.get_text(
                            "information.weather.EMBED.FIELDS.WIND.VALUE",
                            ctx,
                            speed=current['windspeedKmph']
                        ),
                        inline=True
                    )
                    
                    if 'cloudcover' in current:
                        embed.add_field(
                            name=await self.bot.get_text("information.weather.EMBED.FIELDS.CLOUD_COVER.NAME", ctx),
                            value=await self.bot.get_text(
                                "information.weather.EMBED.FIELDS.CLOUD_COVER.VALUE",
                                ctx,
                                cover=current['cloudcover']
                            ),
                            inline=True
                        )
                    
                    if 'visibility' in current:
                        embed.add_field(
                            name=await self.bot.get_text("information.weather.EMBED.FIELDS.VISIBILITY.NAME", ctx),
                            value=await self.bot.get_text(
                                "information.weather.EMBED.FIELDS.VISIBILITY.VALUE",
                                ctx,
                                distance=current['visibility']
                            ),
                            inline=True
                        )
                    
                    if 'precipMM' in current:
                        embed.add_field(
                            name=await self.bot.get_text("information.weather.EMBED.FIELDS.PRECIPITATION.NAME", ctx),
                            value=await self.bot.get_text(
                                "information.weather.EMBED.FIELDS.PRECIPITATION.VALUE",
                                ctx,
                                amount=current['precipMM']
                            ),
                            inline=True
                        )
                    
                    embed.set_thumbnail(url=f"https://wttr.in/{location}_0pq.png")
                    
                    await ctx.send(embed=embed)
                    
            except Exception as e:
                await ctx.warn(
                    await self.bot.get_text("information.weather.ERROR.GENERIC", ctx, error=str(e))
                )

    @hybrid_group(name="poll", invoke_without_command=True)
    @has_permissions(manage_messages=True)
    async def poll(self, ctx: Context) -> Message:
        """Poll management commands"""
        return await ctx.send_help(ctx.command)

    @poll.command(name="create")
    @has_permissions(manage_messages=True)
    async def poll_create(self, ctx: Context, *, flags: PollFlags) -> Message:
        """Create a new poll using flags"""
        async with ctx.dask.acquire():
            options = ["Yes", "No"] 
            
            if ctx.interaction: 
                modal = PollChoicesModal(title=flags.title)
                await ctx.interaction.response.send_modal(modal)
                await modal.wait()
                
                if not modal.choices:
                    return await ctx.warn(self.bot.get_text("information.poll.create.CANCELLED"))

            ends_at = None
            if flags.duration:
                try:
                    duration_seconds = parse_duration(flags.duration)
                    ends_at = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
                except ValueError:
                    return await ctx.warn(self.bot.get_text("information.poll.create.INVALID_DURATION"))

            settings = {
                "anonymous": flags.anonymous,
                "multiple_choice": flags.multiple_choice,
                "required_role": None,
                "show_voters": not flags.anonymous,
                "live_results": True
            }

            poll_id = await self.bot.db.fetchval("""
                INSERT INTO polls (
                    guild_id, channel_id, message_id, creator_id,
                    title, description, choices, settings, ends_at
                )
                VALUES ($1, $2, 0, $3, $4, $5, $6, $7, $8)
                RETURNING poll_id
            """, ctx.guild.id, ctx.channel.id, ctx.author.id,
                flags.title, flags.description, json.dumps(options),
                json.dumps(settings), ends_at)

            embed = await self.create_poll_embed(poll_id)
            view = PollView(poll_id)

            msg = await ctx.send(embed=embed, view=view)

            await self.bot.db.execute("""
                UPDATE polls 
                SET message_id = $1 
                WHERE poll_id = $2
            """, msg.id, poll_id)
            
            return msg

    async def create_poll_embed(self, poll_id: UUID) -> Embed:
        """Create the poll embed"""
        poll_data = await self.bot.db.fetchrow("""
            SELECT * FROM polls WHERE poll_id = $1
        """, poll_id)

        choices = json.loads(poll_data['choices'])
        settings = json.loads(poll_data['settings'])

        embed = Embed(
            title=poll_data['title'],
            description=poll_data['description'],
            color=config.COLORS.NEUTRAL,
            timestamp=poll_data['created_at']
        )

        for i, choice in enumerate(choices, 1):
            votes = await self.bot.db.fetchval("""
                SELECT COUNT(*) FROM poll_votes 
                WHERE poll_id = $1 AND $2 = ANY(choice_ids)
            """, poll_id, i)
            
            embed.add_field(
                name=f"Option {i}",
                value=f"{choice}\nVotes: {votes}",
                inline=True
            )

        footer_text = []
        if poll_data['ends_at']:
            footer_text.append(f"Ends {format_dt(poll_data['ends_at'], 'R')}")
        if settings['multiple_choice']:
            footer_text.append("Multiple choice enabled")
        if settings['anonymous']:
            footer_text.append("Anonymous voting")

        embed.set_footer(text=" • ".join(footer_text))

        return embed

    @poll.command(name="quick")
    @has_permissions(manage_messages=True)
    async def poll_quick(
        self,
        ctx: Context,
        question: str,
    ) -> Message:
        """Create a quick poll with simple yes/no or custom options
        """
        if not ctx.interaction:
            options = ["Yes", "No"]
        else:
            modal = PollChoicesModal("Quick Poll")
            await ctx.interaction.response.send_modal(modal)
            await modal.wait()

            if not modal.choices:
                options = ["Yes", "No"]
            else:
                options = modal.choices

        settings = {
            "anonymous": False,
            "multiple_choice": False,
            "required_role": None,
            "show_voters": True,
            "live_results": True
        }

        poll_id = await self.bot.db.fetchval("""
            INSERT INTO polls (
                guild_id, channel_id, message_id, creator_id,
                title, description, choices, settings
            )
            VALUES ($1, $2, 0, $3, $4, $5, $6, $7)
            RETURNING poll_id
        """, ctx.guild.id, ctx.channel.id, ctx.author.id,
            "Quick Poll", question, json.dumps(options),
            json.dumps(settings))

        embed = await self.create_poll_embed(poll_id)
        view = PollView(poll_id)
        
        msg = await ctx.send(embed=embed, view=view)
        
        await self.bot.db.execute("""
            UPDATE polls SET message_id = $1 WHERE poll_id = $2
        """, msg.id, poll_id)
        
        return msg

    @poll.command(name="list")
    async def poll_list(
        self,
        ctx: Context,
        creator: Optional[discord.Member] = None,
        sort_by: str = "time"
    ) -> Message:
        """
        List all active polls in the server
        """
        async with ctx.dask.acquire():
            query = """
                SELECT p.*, COUNT(v.vote_id) as vote_count
                FROM polls p
                LEFT JOIN poll_votes v ON p.poll_id = v.poll_id
                WHERE p.guild_id = $1 AND p.is_active = true
            """
            params = [ctx.guild.id]
            
            if creator:
                query += " AND p.creator_id = $2"
                params.append(creator.id)
                
            query += " GROUP BY p.poll_id"
            
            if sort_by.lower() == "votes":
                query += " ORDER BY vote_count DESC"
            else:
                query += " ORDER BY p.created_at DESC"
                
            polls = await self.bot.db.fetch(query, *params)
            
            if not polls:
                return await ctx.warn(
                    await self.bot.get_text(
                        "information.poll.list.NO_POLLS.FILTERED" if creator else "information.poll.list.NO_POLLS.DEFAULT",
                        ctx,
                        creator=creator.mention if creator else None
                    )
                )

            entries = []
            for poll in polls:
                vote_count = await self.bot.db.fetchval(
                    "SELECT COUNT(*) FROM poll_votes WHERE poll_id = $1",
                    poll['poll_id']
                )
                
                entry = (
                    await self.bot.get_text("information.poll.list.ENTRY.TITLE", ctx, title=poll['title']) + "\n" +
                    await self.bot.get_text("information.poll.list.ENTRY.CREATOR", ctx,
                        creator=ctx.guild.get_member(poll['creator_id']).mention) + "\n" +
                    await self.bot.get_text("information.poll.list.ENTRY.VOTES", ctx, count=vote_count) + "\n" +
                    await self.bot.get_text("information.poll.list.ENTRY.ID", ctx, id=poll['poll_id']) + "\n" +
                    await self.bot.get_text("information.poll.list.ENTRY.CREATED", ctx,
                        time=format_dt(poll['created_at'], 'R'))
                )
                if poll['ends_at']:
                    entry += "\n" + await self.bot.get_text("information.poll.list.ENTRY.ENDS", ctx,
                        time=format_dt(poll['ends_at'], 'R'))
                entries.append(entry)

            paginator = Paginator(
                ctx,
                entries=entries,
                embed=Embed(title=await self.bot.get_text("information.poll.list.EMBED_TITLE", ctx, guild=ctx.guild)),
            )
            return await paginator.start()

    @poll.command(name="end")
    async def poll_end(self, ctx: Context, poll_id: str) -> Message:
        """
        End a poll early
        """
        async with ctx.dask.acquire():
            try:
                poll_id = UUID(poll_id)
            except ValueError:
                return await ctx.warn(await self.bot.get_text("information.poll.end.INVALID_ID", ctx))

            poll = await self.bot.db.fetchrow(
                "SELECT * FROM polls WHERE poll_id = $1 AND guild_id = $2 AND is_active = true",
                poll_id, ctx.guild.id
            )
            
            if not poll:
                return await ctx.warn(await self.bot.get_text("information.poll.end.NOT_FOUND", ctx))
            
            if not (ctx.author.id == poll['creator_id'] or ctx.author.guild_permissions.manage_guild):
                return await ctx.warn(await self.bot.get_text("information.poll.end.NO_PERMISSION", ctx))
            
            await self.bot.db.execute(
                "UPDATE polls SET is_active = false WHERE poll_id = $1",
                poll_id
            )
            
            try:
                channel = ctx.guild.get_channel(poll['channel_id'])
                message = await channel.fetch_message(poll['message_id'])
                embed = await self.create_poll_embed(poll_id)
                await message.edit(embed=embed, view=None)
            except:
                pass
            
            return await ctx.approve(await self.bot.get_text("information.poll.end.SUCCESS", ctx))

    @poll.command(name="results")
    async def poll_results(self, ctx: Context, poll_id: str) -> Message:
        """
        View detailed results of a poll
        """
        async with ctx.dask.acquire():
            try:
                poll_id = UUID(poll_id)
            except ValueError:
                return await ctx.warn(await self.bot.get_text("information.poll.results.INVALID_ID", ctx))

            results_view = PollResultsView(poll_id)
            results_embed = await results_view.generate_results(ctx)
            
            if not results_embed:
                return await ctx.warn(await self.bot.get_text("information.poll.results.NOT_FOUND", ctx))
            
            return await ctx.send(embed=results_embed, view=results_view)

    @command()
    async def status(self, ctx: Context):
        """
        Check shard status.
        """
        await ctx.neutral(
            self.bot.get_text(
                "information.status.MESSAGE",
                url="https://evict.bot/status",
                shard_id=ctx.guild.shard_id
            )
        )

    @hybrid_command(name="language", aliases=["lang"])
    async def language(self, ctx: Context) -> Message:
        """Change your preferred language for bot interactions."""
        available_languages = []
        
        system_dir = Path("langs/system")
        if system_dir.exists():
            for lang_file in system_dir.glob("*/*.json"):
                try:
                    with lang_file.open(encoding='utf-8') as f:
                        data = json.load(f)
                        if all(key in data for key in ['language', 'language_code', 'language_local']):
                            available_languages.append({
                                'name': data['language'],
                                'code': data['language_code'],
                                'local': data['language_local']
                            })
                except Exception as e:
                    continue

        available_languages = {lang['code']: lang for lang in available_languages}.values()
        
        select = Select(
            placeholder="Choose a language...",
            options=[
                SelectOption(
                    label=lang['name'],
                    value=lang['local'],
                    description=f"Set language to {lang['name']}"
                )
                for lang in available_languages
            ]
        )

        async def select_callback(interaction: Interaction):
            if interaction.user.id != ctx.author.id:
                await interaction.warn(self.bot.get_text("system.help.CANNOT_INTERACT"))
                return

            selected_lang = interaction.data["values"][0]
            
            await self.bot.db.execute(
                """
                INSERT INTO user_settings (user_id, language)
                VALUES ($1, $2)
                ON CONFLICT (user_id) 
                DO UPDATE SET language = $2
                """,
                ctx.author.id,
                selected_lang
            )

            embed = Embed(
                description=f"✅ Your language has been set to `{selected_lang}`",
                color=config.COLORS.APPROVE
            )
            await interaction.response.edit_message(embed=embed, view=None)

        select.callback = select_callback
        view = View(timeout=180)
        view.add_item(select)

        current_lang = await self.bot.db.fetchval(
            "SELECT language FROM user_settings WHERE user_id = $1",
            ctx.author.id
        ) or "en-US"

        embed = Embed(
            description=f"Select your preferred language from the dropdown menu below.\nCurrent language: `{current_lang}`",
            color=config.COLORS.APPROVE
        )
        
        return await ctx.send(embed=embed, view=view)
    
class PollVoteSelect(Select):
    def __init__(self, poll_id: UUID, choices: list[str], multiple: bool = False):
        self.poll_id = poll_id
        
        options = [
            SelectOption(
                label=f"Option {i+1}",
                description=choice[:100],  
                value=str(i+1)
            )
            for i, choice in enumerate(choices)
        ]
        
        super().__init__(
            placeholder="Select your choice(s)...",
            options=options,
            min_values=1,
            max_values=len(options) if multiple else 1,
            custom_id=f"poll_vote_{poll_id}"
        )

    async def callback(self, interaction: Interaction):
        try:
            poll_data = await interaction.client.db.fetchrow("""
                SELECT * FROM polls WHERE poll_id = $1 AND is_active = true
            """, self.poll_id)
            
            if not poll_data:
                return await interaction.response.send_message(
                    self.bot.get_text("information.poll.UI.VOTE.ENDED"),
                    ephemeral=True
                )
                
            settings = json.loads(poll_data['settings'])
            
            if settings['required_role']:
                role = interaction.guild.get_role(settings['required_role'])
                if role and role not in interaction.user.roles:
                    return await interaction.response.send_message(
                        self.bot.get_text("information.poll.UI.VOTE.ROLE_REQUIRED", role=role.mention),
                        ephemeral=True
                    )

            choice_ids = [int(value) for value in self.values]
            
            await interaction.client.db.execute("""
                INSERT INTO poll_votes (poll_id, user_id, choice_ids)
                VALUES ($1, $2, $3)
                ON CONFLICT (poll_id, user_id) 
                DO UPDATE SET choice_ids = $3
            """, self.poll_id, interaction.user.id, choice_ids)
            
            try:
                embed = await interaction.client.get_cog('Information').create_poll_embed(
                    self.poll_id
                )
                await interaction.message.edit(embed=embed)
            except discord.NotFound:
                pass 
            
            await interaction.response.send_message(
                self.bot.get_text("information.poll.UI.VOTE.SUCCESS"),
                ephemeral=True
            )
            
        except Exception as e:
            await interaction.response.send_message(
                self.bot.get_text("information.poll.UI.VOTE.ERROR"),
                ephemeral=True
            )

class PollResultsView(View):
    def __init__(self, poll_id: UUID):
        super().__init__(timeout=None)
        self.poll_id = poll_id

    async def generate_results(self, interaction: Interaction) -> Embed:
        poll_data = await interaction.client.db.fetchrow("""
            SELECT * FROM polls WHERE poll_id = $1
        """, self.poll_id)
        
        choices = json.loads(poll_data['choices'])
        settings = json.loads(poll_data['settings'])
        
        embed = Embed(
            title=f"Results: {poll_data['title']}",
            color=config.COLORS.NEUTRAL
        )
        
        total_votes = await interaction.client.db.fetchval("""
            SELECT COUNT(*) FROM poll_votes WHERE poll_id = $1
        """, self.poll_id)
        
        for i, choice in enumerate(choices, 1):
            votes = await interaction.client.db.fetchval("""
                SELECT COUNT(*) FROM poll_votes 
                WHERE poll_id = $1 AND $2 = ANY(choice_ids)
            """, self.poll_id, i)
            
            percentage = (votes / total_votes * 100) if total_votes > 0 else 0
            
            bar_length = 8
            filled_bars = int(percentage / 10)
            
            bar = ""
            if filled_bars > 0:
                bar += "<:evict_blr:1263759792439169115>"
                
                if filled_bars > 1:
                    bar += "<:evict_sqaure:1263759807417028649>" * (filled_bars - 2)
                
                if filled_bars > 1:
                    bar += "<:evict_brr:1263759798751461377>"
            
            empty_bars = bar_length - filled_bars
            if empty_bars > 0:
                if filled_bars == 0:
                    bar += "<:white_left_rounded:1263743905120387172>"
                    if empty_bars > 2:
                        bar += "<:white:1263743898145001517>" * (empty_bars - 2)
                    bar += "<:white_right_rounded:1263743912221216862>"
                else:
                    bar += "<:white:1263743898145001517>" * empty_bars
            
            voters = []
            if settings['show_voters']:
                voter_records = await interaction.client.db.fetch("""
                    SELECT user_id FROM poll_votes 
                    WHERE poll_id = $1 AND $2 = ANY(choice_ids)
                """, self.poll_id, i)
                voters = [
                    interaction.guild.get_member(record['user_id']).mention
                    for record in voter_records
                    if interaction.guild.get_member(record['user_id'])
                ]
            
            embed.add_field(
                name=f"Option {i}: {choice}",
                value=(
                    f"{bar} {percentage:.1f}% ({votes} votes)\n"
                    + (f"Voters: {', '.join(voters)}\n" if voters else "")
                ),
                inline=True
            )
            
        if poll_data['ends_at']:
            if poll_data['ends_at'] > datetime.now(timezone.utc):
                embed.set_footer(text=f"Poll ends {format_dt(poll_data['ends_at'], 'R')}")
            else:
                embed.set_footer(text="Poll has ended")
                
        return embed

    @discord.ui.button(
        label="information.poll.UI.RESULTS.BACK_BUTTON",
        style=ButtonStyle.secondary
    )
    async def back_button(self, interaction: Interaction, button: Button):
        poll_embed = await interaction.client.get_cog('Information').create_poll_embed(
            self.poll_id
        )
        poll_view = PollView(self.poll_id)
        await interaction.message.edit(embed=poll_embed, view=poll_view)
        await interaction.response.defer()

class PollView(View):
    def __init__(self, poll_id: UUID):
        super().__init__(timeout=None)
        self.poll_id = poll_id

    async def setup_vote_select(self, interaction: Interaction) -> Optional[PollVoteSelect]:
        poll_data = await interaction.client.db.fetchrow("""
            SELECT * FROM polls WHERE poll_id = $1 AND is_active = true
        """, self.poll_id)
        
        if not poll_data:
            return None
            
        choices = json.loads(poll_data['choices'])
        settings = json.loads(poll_data['settings'])
        
        return PollVoteSelect(
            self.poll_id,
            choices,
            multiple=settings['multiple_choice']
        )

    @discord.ui.button(
        label="information.poll.UI.VOTE.BUTTON",
        style=ButtonStyle.primary,
        custom_id="vote"
    )
    async def vote_button(self, interaction: Interaction, button: Button):
        select = await self.setup_vote_select(interaction)
        if not select:
            return await interaction.response.send_message(
                self.bot.get_text("information.poll.UI.VOTE.ENDED"),
                ephemeral=True
            )
            
        view = View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            self.bot.get_text("information.poll.UI.VOTE.SELECT_PROMPT"),
            view=view,
            ephemeral=True
        )

    @discord.ui.button(
        label="information.poll.UI.RESULTS.BUTTON",
        style=ButtonStyle.secondary,
        custom_id="results"
    )
    async def results_button(self, interaction: Interaction, button: Button):
        results_view = PollResultsView(self.poll_id)
        results_embed = await results_view.generate_results(interaction)
        await interaction.message.edit(embed=results_embed, view=results_view)
        await interaction.response.defer()

class PollChoicesModal(Modal):
    def __init__(self, title: str):
        super().__init__(title="Poll Choices")
        self.poll_title = title
        self.choices = []

        for i in range(1, 6):  
            self.add_item(TextInput(
                label=f"Choice {i}",
                placeholder="Enter a choice...",
                required=i <= 2,
                max_length=100,
                style=TextStyle.short
            ))

    async def on_submit(self, interaction: Interaction):
        self.choices = [item.value for item in self.children if item.value]
        if len(self.choices) < 2:
            await interaction.response.send_message(
                "You must provide at least 2 choices.", ephemeral=True
            )
            return
        await interaction.response.defer()

def parse_duration(duration: str) -> int:
    """Convert duration string to seconds"""
    units = {
        's': 1,
        'm': 60,
        'h': 3600,
        'd': 86400,
        'w': 604800
    }
    
    amount = int(duration[:-1])
    unit = duration[-1].lower()
    
    if unit not in units:
        raise ValueError("Invalid duration unit")
        
    return amount * units[unit]

async def setup(bot: Evict):
    """Load the Information cog."""
    log.info(f"Setting up Information cog on cluster {bot.cluster_id}")
    cog = Information(bot)
    await bot.add_cog(cog)