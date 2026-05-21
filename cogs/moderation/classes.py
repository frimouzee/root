from main import Evict
from typing import Union, Optional
from core.context import Context
from discord.ext import commands
from discord.ext.commands.core import has_permissions
from discord import Member, User, Embed
from discord.ui import Modal, TextInput, Button, View
import config

import datetime
import discord
import config
import humanize
from datetime import timedelta
from utils.conversions.embed import EmbedScript
from processors.moderation import process_mod_action, process_dm_script
import psutil
import asyncio

from opentelemetry import trace
from prometheus_client import Counter, Histogram, REGISTRY

_mod_actions: Optional[Counter] = None
_mod_action_duration: Optional[Histogram] = None
_dm_attempts: Optional[Counter] = None

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
            'moderation_actions_created',
            'mod_actions',
            'mod_actions_total',
            'mod_actions_created'
        )
        _mod_actions = Counter(
            'moderation_actions_total',
            'Total number of moderation actions',
            ['action', 'guild_id']
        )
    return _mod_actions

def get_mod_action_duration() -> Histogram:
    global _mod_action_duration
    if _mod_action_duration is None:
        unregister_if_exists(
            'moderation_action_duration_seconds',
            'mod_action_duration_seconds'
        )
        _mod_action_duration = Histogram(
            'moderation_action_duration_seconds',
            'Time taken to process moderation actions',
            ['action']
        )
    return _mod_action_duration

def get_dm_attempts() -> Counter:
    global _dm_attempts
    if _dm_attempts is None:
        unregister_if_exists(
            'moderation_dm_attempts',
            'moderation_dm_attempts_total',
            'moderation_dm_attempts_created',
            'mod_dm_attempts',
            'mod_dm_attempts_total',
            'mod_dm_attempts_created'
        )
        _dm_attempts = Counter(
            'moderation_dm_attempts_total',
            'Total number of moderation DM attempts',
            ['success', 'action']
        )
    return _dm_attempts

# Replace direct assignments with getter functions
MOD_ACTIONS = get_mod_actions()
MOD_ACTION_DURATION = get_mod_action_duration()
DM_ATTEMPTS = get_dm_attempts()

class Mod:
    def is_mod_configured():
        async def predicate(ctx: Context):
            with ctx.bot.tracer.start_span("mod_check") as span:
                span.set_attribute("guild_id", str(ctx.guild.id))
                
                if not ctx.command:
                    return False
                    
                required_perms = []
                for check in ctx.command.checks:
                    if hasattr(check, 'perms'):
                        required_perms.extend(
                            perm for perm, value in check.perms.items() if value
                        )
                
                if required_perms:
                    missing_perms = [
                        perm for perm in required_perms 
                        if not getattr(ctx.author.guild_permissions, perm)
                    ]
                    if missing_perms:
                        perm_name = missing_perms[0].replace('_', ' ').title()
                        await ctx.warn(f"You're missing the **{perm_name}** permission!")
                        span.set_attribute("failed", "missing_permissions")
                        return False

                check = await ctx.bot.db.fetchrow(
                    "SELECT * FROM mod WHERE guild_id = $1", ctx.guild.id
                )

                if not check:
                    await ctx.warn(
                        f"Moderation isn't **enabled** in this server. Enable it using `{ctx.clean_prefix}setme` command"
                    )
                    span.set_attribute("failed", "not_enabled")
                    return False
                    
                span.set_attribute("success", "true")
                return True

        return commands.check(predicate)


class ModConfig:
    @staticmethod
    async def sendlogs(
        bot: Evict,
        action: str,
        author: Member,
        victim: Union[Member, User],
        reason: str,
        duration: Union[timedelta, int, None] = None,
        role: discord.Role = None
    ):
        with bot.tracer.start_span("mod_action") as span:
            span.set_attribute("action", action)
            span.set_attribute("guild_id", str(author.guild.id))
            span.set_attribute("moderator_id", str(author.id))
            span.set_attribute("target_id", str(victim.id))
            
            with MOD_ACTION_DURATION.labels(action=action).time():
                try:
                    MOD_ACTIONS.labels(
                        action=action,
                        guild_id=str(author.guild.id)
                    ).inc()

                    action_data = {'action': action, 'duration': duration}
                    processed_action = await bot.loop.run_in_executor(
                        None,
                        bot.process_pool.apply,
                        process_mod_action,
                        (action_data,)
                    )

                    settings = await bot.db.fetchrow(
                        "SELECT * FROM mod WHERE guild_id = $1",
                        author.guild.id
                    )
                    
                    if not settings:
                        return

                    res = await bot.db.fetchrow(
                        "SELECT count FROM cases WHERE guild_id = $1", author.guild.id
                    )
                    
                    if not res:
                        await bot.db.execute(
                            "INSERT INTO cases (guild_id, count) VALUES ($1, $2)",
                            author.guild.id, 0
                        )
                        case = 1
                    else:
                        case = int(res["count"]) + 1

                    await bot.db.execute(
                        "UPDATE cases SET count = $1 WHERE guild_id = $2", case, author.guild.id
                    )

                    duration_value = (
                        int(duration.total_seconds())
                        if isinstance(duration, timedelta)
                        else duration
                    )

                    await bot.db.execute(
                        """
                        INSERT INTO history.moderation 
                        (guild_id, case_id, user_id, moderator_id, action, reason, duration, role_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        author.guild.id,
                        case,
                        victim.id,
                        author.id,
                        action,
                        reason,
                        duration_value,
                        role.id if role else None
                    )

                    if settings.get("channel_id"):
                        embed = Embed(
                            timestamp=datetime.datetime.now(),
                            color=(
                                discord.Color.green() if action in ['role_add', 'unban', 'untimeout', 'unjail']
                                else discord.Color.red() if action in ['ban', 'kick', 'timeout', 'jail']
                                else discord.Color.green() if action == 'role_add'
                                else discord.Color.red() if action == 'role_remove'
                                else discord.Color.blurple()
                            )
                        )
                        embed.set_author(name="Modlog Entry", icon_url=author.display_avatar)
                        
                        if action in ['role_add', 'role_remove']:
                            embed.add_field(
                                name="Information",
                                value=f"**Case #{case}** | {action}\n**User**: {victim} (`{victim.id}`)\n**Moderator**: {author} (`{author.id}`)\n**Role**: {role.mention}\n**Reason**: {reason}",
                            )
                        else:
                            duration_text = f"\n**Duration**: {humanize.naturaldelta(duration)}" if duration else ""
                            embed.add_field(
                                name="Information",
                                value=f"**Case #{case}** | {action}\n**User**: {victim} (`{victim.id}`)\n**Moderator**: {author} (`{author.id}`)\n**Reason**: {reason}{duration_text}",
                            )

                        try:
                            await author.guild.get_channel(int(settings["channel_id"])).send(embed=embed)
                        except:
                            pass

                    if processed_action['should_dm'] and settings.get('dm_enabled'):
                        try:
                            mutual_guilds = [g for g in bot.guilds if victim in g.members]
                            if not mutual_guilds and action not in ['ban', 'kick', 'hardban']:
                                return

                            if action in ['ban', 'kick']:
                                try:
                                    script = settings.get(f"dm_{action.lower()}")
                                    
                                    if script and script.lower() != 'true':
                                        script_obj = EmbedScript(script)
                                        await script_obj.send(
                                            victim,
                                            guild=author.guild,
                                            moderator=author,
                                            reason=reason,
                                            duration=duration,
                                            role=role
                                        )
                                    else:
                                        if action in ['role_add', 'role_remove']:
                                            embed = Embed(
                                                title=f"Role {'Added' if action == 'role_add' else 'Removed'}",
                                                color=discord.Color.green() if action == 'role_add' else discord.Color.red(),
                                                timestamp=datetime.datetime.now()
                                            )
                                            embed.add_field(
                                                name="Server",
                                                value=author.guild.name,
                                                inline=True
                                            )
                                            embed.add_field(
                                                name="Role",
                                                value=role.name,
                                                inline=True
                                            )
                                            embed.add_field(
                                                name="Moderator",
                                                value=str(author),
                                                inline=True
                                            )
                                            if reason:
                                                embed.add_field(
                                                    name="Reason",
                                                    value=reason,
                                                    inline=False
                                                )
                                        else:
                                            duration_text = f"{humanize.naturaldelta(duration)}" if duration else ""
                                            embed = Embed(
                                                title=processed_action['title'].title(),
                                                description=duration_text if duration else "",
                                                color=discord.Color.green() if processed_action['is_unaction'] else discord.Color.red(),
                                                timestamp=datetime.datetime.now()
                                            )
                                            embed.add_field(
                                                name=f"You have been {processed_action['title']} in",
                                                value=author.guild.name,
                                                inline=True
                                            )
                                            embed.add_field(
                                                name="Moderator",
                                                value=str(author),
                                                inline=True
                                            )
                                            embed.add_field(
                                                name="Reason",
                                                value=reason,
                                                inline=True
                                            )

                                        appeal_config = await bot.db.fetchrow(
                                            "SELECT * FROM appeal_config WHERE guild_id = $1",
                                            author.guild.id
                                        )

                                        view = View()
                                        if appeal_config:
                                            if appeal_config.get('direct_appeal', False) or action in ['timeout', 'jail']:
                                                view = View()
                                                view.add_item(AppealButton(modal=True, action_type=action, guild_id=author.guild.id))
                                            elif action in ['ban', 'kick']:
                                                appeal_server = bot.get_guild(appeal_config['appeal_server_id'])
                                                if appeal_server and not appeal_config.get('direct_appeal', False):
                                                    appeal_channel = appeal_server.get_channel(appeal_config['appeal_channel_id'])
                                                    if appeal_channel:
                                                        try:
                                                            invite = await appeal_channel.create_invite(max_uses=1)
                                                            view = View()
                                                            view.add_item(Button(
                                                                label="Join Appeal Server",
                                                                url=invite.url,
                                                                style=discord.ButtonStyle.link
                                                            ))
                                                            embed.add_field(
                                                                name="Appeal",
                                                                value=f"You can appeal this action in our appeal server",
                                                                inline=False
                                                            )
                                                        except:
                                                            embed.add_field(
                                                                name="Appeal",
                                                                value="To appeal, please join discord.gg/evict to get mutual server access with the bot",
                                                                inline=False
                                                            )

                                        await victim.send(embed=embed, view=view)

                                except Exception as e:
                                    import traceback
                                    span.set_attribute("dm_error", str(e))
                                    span.set_status(trace.Status(trace.StatusCode.ERROR))
                                    raise
                            else:
                                asyncio.create_task(
                                    send_non_critical_dm(
                                        bot, settings, action, author, victim, 
                                        reason, duration, role, processed_action
                                    )
                                )

                        except Exception as e:
                            span.set_attribute("dm_error", str(e))
                            span.set_status(trace.Status(trace.StatusCode.ERROR))
                            raise

                except Exception as e:
                    span.set_attribute("error", str(e))
                    span.set_status(trace.Status(trace.StatusCode.ERROR))
                    raise

async def send_non_critical_dm(bot, settings, action, author, victim, reason, duration, role, processed_action):
    """Handles sending DMs for non-critical moderation actions"""
    try:
        script = settings.get(f"dm_{action.lower()}")
        
        if script and script.lower() != 'true':
            script_obj = EmbedScript(script)
            await script_obj.send(
                victim,
                guild=author.guild,
                moderator=author,
                reason=reason,
                duration=duration,
                role=role
            )
        else:
            if action in ['role_add', 'role_remove']:
                embed = Embed(
                    title=f"Role {'Added' if action == 'role_add' else 'Removed'}",
                    color=discord.Color.green() if action == 'role_add' else discord.Color.red(),
                    timestamp=datetime.datetime.now()
                )
                embed.add_field(name="Server", value=author.guild.name, inline=True)
                embed.add_field(name="Role", value=role.name, inline=True)
                embed.add_field(name="Moderator", value=str(author), inline=True)
                if reason:
                    embed.add_field(name="Reason", value=reason, inline=False)
            else:
                duration_text = f"{humanize.naturaldelta(duration)}" if duration else ""
                embed = Embed(
                    title=processed_action['title'].title(),
                    description=duration_text if duration else "",
                    color=discord.Color.green() if processed_action['is_unaction'] else discord.Color.red(),
                    timestamp=datetime.datetime.now()
                )
                embed.add_field(name=f"You have been {processed_action['title']} in", value=author.guild.name, inline=True)
                embed.add_field(name="Moderator", value=str(author), inline=True)
                embed.add_field(name="Reason", value=reason, inline=True)

            appeal_config = await bot.db.fetchrow(
                "SELECT * FROM appeal_config WHERE guild_id = $1",
                author.guild.id
            )

            view = View()
            if appeal_config:
                if appeal_config.get('direct_appeal', False) or action in ['timeout', 'jail']:
                    view = View()
                    view.add_item(AppealButton(modal=True, action_type=action, guild_id=author.guild.id))
                elif action in ['ban', 'kick']:
                    appeal_server = bot.get_guild(appeal_config['appeal_server_id'])
                    if appeal_server and not appeal_config.get('direct_appeal', False):
                        appeal_channel = appeal_server.get_channel(appeal_config['appeal_channel_id'])
                        if appeal_channel:
                            try:
                                invite = await appeal_channel.create_invite(max_uses=1)
                                view = View()
                                view.add_item(Button(
                                    label="Join Appeal Server",
                                    url=invite.url,
                                    style=discord.ButtonStyle.link
                                ))
                                embed.add_field(
                                    name="Appeal",
                                    value=f"You can appeal this action in our appeal server",
                                    inline=False
                                )
                            except:
                                embed.add_field(
                                    name="Appeal",
                                    value="To appeal, please join discord.gg/evict to get mutual server access with the bot",
                                    inline=False
                                )

            await victim.send(embed=embed, view=view)

    except Exception as e:
        pass

class ClearMod(discord.ui.View):
    def __init__(self, ctx: Context):
        super().__init__()
        self.ctx = ctx
        self.status = False

    @discord.ui.button(emoji=config.EMOJIS.CONTEXT.APPROVE)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.ctx.author.id:
            return await interaction.warn("You are not the author of this embed")

        check = await interaction.client.db.fetchrow(
            "SELECT * FROM mod WHERE guild_id = $1", interaction.guild.id
        )

        channelid = check["channel_id"]
        roleid = check["role_id"]
        logsid = check["jail_id"]

        channel = interaction.guild.get_channel(channelid)
        role = interaction.guild.get_role(roleid)
        logs = interaction.guild.get_channel(logsid)

        try:
            await channel.delete()

        except:
            pass

        try:
            await role.delete()

        except:
            pass

        try:
            await logs.delete()

        except:
            pass

        await interaction.client.db.execute(
            "DELETE FROM mod WHERE guild_id = $1", interaction.guild.id
        )

        self.status = True

        return await interaction.response.edit_message(
            view=None,
            embed=Embed(
                description=f"I have **disabled** the jail system.",
            ),
        )

    @discord.ui.button(emoji=config.EMOJIS.CONTEXT.DENY)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.ctx.author.id:

            return await interaction.warn("You are not the author of this embed")

        await interaction.response.edit_message(
            embed=Embed(description="Aborting action"), view=None
        )
        self.status = True

    async def on_timeout(self) -> None:
        if self.status == False:
            for item in self.children:
                item.disabled = True

            await self.message.edit(view=self)

class InviteButton(discord.ui.View):
    def __init__(self, url: str):
        super().__init__()
        self.add_item(discord.ui.Button(
            emoji=config.EMOJIS.SOCIAL.WEBSITE,
            url=url,
            style=discord.ButtonStyle.link
        ))

class AppealButton(Button):
    def __init__(self, modal: bool = False, action_type: str = None, guild_id: int = None):
        super().__init__(
            label="Appeal",
            style=discord.ButtonStyle.primary,
            custom_id="appeal_button"
        )
        self.guild_id = guild_id
        self.modal = modal
        self.action_type = action_type

    async def callback(self, interaction: discord.Interaction):
        if self.modal:
            config_cog = interaction.client.get_cog("Config")
            if not config_cog:
                await interaction.response.send_message("Appeal system unavailable", ephemeral=True)
                return

            try:
                appeal_config = await interaction.client.db.fetchrow(
                    "SELECT * FROM appeal_config WHERE guild_id = $1",
                    self.guild_id
                )
                
                if not appeal_config:
                    await interaction.response.send_message("Appeal system not configured", ephemeral=True)
                    return

                appeal_modal = await config_cog.get_appeal_modal(
                    self.guild_id,
                    self.action_type
                )
                await interaction.response.send_modal(appeal_modal)
            except Exception as e:
                await interaction.response.send_message("Error creating appeal form", ephemeral=True)
