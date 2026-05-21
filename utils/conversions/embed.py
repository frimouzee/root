from __future__ import annotations

import random
import urllib.parse
from datetime import datetime
from functools import lru_cache
from typing import List, Optional, Union, Tuple, Dict, Any

from discord import ButtonStyle, Color, Embed, Member, Message, TextChannel, Webhook
from discord.ext.commands import CommandError, Converter
from discord.ui import Button, View  # type: ignore
from discord.utils import escape_markdown, utcnow
from pydantic import BaseModel, ValidationError
from utils.converters.color import colors
from utils.tools.utilities import tagscript, hidden
from utils.tools.utilities.humanize import comma, ordinal
from core.context import Context
from utils.tools.utilities.managers.regex import IMAGE_URL, URL


# Temporary till we actually implement the welcome system

class EmbedError(Exception):
    """Base exception for embed errors."""
    pass

class EmbedValidationError(EmbedError):
    """Raised when embed validation fails."""
    pass

class EmbedParsingError(EmbedError):
    """Raised when embed parsing fails."""
    pass

class LinkButton(Button):
    """A button that links to a URL."""
    __slots__ = ('url', 'label', 'emoji')

    def __init__(
        self,
        label: str,
        url: str,
        emoji: str,
        style: ButtonStyle = ButtonStyle.link,
    ):
        super().__init__(style=style, label=label, url=url, emoji=emoji)


class LinkView(View):
    """A view containing link buttons."""
    __slots__ = ('links',)

    def __init__(self, links: list[LinkButton]):
        super().__init__(timeout=None)
        self.links = links
        for button in links:
            self.add_item(button)


@lru_cache(maxsize=1000)
def get_color(value: str) -> Optional[Color]:
    """Get a color from a string value with caching."""
    value = value.lower()
    
    if value in {"random", "rand", "r"}:
        return Color.random()
    if value in {"invisible", "invis"}:
        return Color.from_str("#2B2D31")
    if value in {"blurple", "blurp"}:
        return Color.blurple()
    if value in {"black", "negro"}:
        return Color.from_str("#000001")

    try:
        color_value = colors.get(value) or value
        color = Color(int(color_value.replace("#", ""), 16))
        return color if color.value <= 16777215 else None
    except (ValueError, AttributeError):
        return None


class EmbedScript(BaseModel):
    """Handles embed script parsing and generation."""
    script: str
    is_voicemaster: bool = False
    _type: str = "text"
    objects: Dict[str, Any] = {
        "content": None,
        "embed": Embed(),
        "embeds": [],
        "button": []
    }
    parser: Any = None
    _MAX_TITLE_LENGTH: int = 256
    _MAX_DESCRIPTION_LENGTH: int = 4096
    _MAX_FIELD_NAME_LENGTH: int = 256
    _MAX_FIELD_VALUE_LENGTH: int = 1024
    _MAX_FIELDS: int = 25
    _MAX_FOOTER_LENGTH: int = 2048

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, script: str, is_voicemaster: bool = False):
        super().__init__(script=script, is_voicemaster=is_voicemaster)
        try:
            self.parser = tagscript.Parser()
            self._setup_parsers()
        except Exception as e:
            raise EmbedValidationError(f"Failed to initialize embed: {str(e)}")

    def _setup_parsers(self) -> None:
        """Setup basic parser methods."""
        @self.parser.method(name="lower", usage="(value)")
        async def lower(_: None, value: str) -> str:
            return value.lower()

        @self.parser.method(name="upper", usage="(value)")
        async def upper(_: None, value: str) -> str:
            return value.upper()

        @self.parser.method(name="hidden", usage="(value)")
        async def _hidden(_: None, value: str) -> str:
            return hidden(value)

        @self.parser.method(name="quote", usage="(value)")
        async def quote(_: None, value: str) -> str:
            return urllib.parse.quote(value, safe="")

        @self.parser.method(name="len", usage="(value)")
        async def length(_: None, value: str) -> int:
            if ", " in value:
                return len(value.split(", "))
            if "," in value:
                value = value.replace(",", "")
                if value.isnumeric():
                    return int(value)
            return len(value)

        @self.parser.method(name="strip", usage="(text) (removal)")
        async def _strip(_: None, text: str, removal: str) -> str:
            return text.replace(removal, "")

        @self.parser.method(name="random", usage="(items)")
        async def _random(_: None, *items) -> str:
            return random.choice(items)

    @lru_cache(maxsize=100)
    def _format_timestamp(self, dt: datetime) -> str:
        """Format timestamp with caching."""
        return dt.strftime("%m/%d/%Y, %I:%M %p")

    async def resolve_variables(self, **kwargs) -> str:
        """Format variables in the script with optimized replacements."""
        script = self.script

        if guild := kwargs.get("guild"):
            replacements = {
                "{guild}": str(guild),
                "{guild.id}": str(guild.id),
                "{guild.name}": str(guild.name),
                "{guild.icon}": str(guild.icon or "https://cdn.discordapp.com/embed/avatars/1.png"),
                "{guild.banner}": str(guild.banner or "No banner"),
                "{guild.splash}": str(guild.splash or "No splash"),
                "{guild.discovery_splash}": str(guild.discovery_splash or "No discovery splash"),
                "{guild.owner}": str(guild.owner),
                "{guild.owner_id}": str(guild.owner_id),
                "{guild.count}": str(comma(len(guild.members))),
                "{guild.members}": str(comma(len(guild.members))),
                "{len(guild.members)}": str(comma(len(guild.members))),
                "{guild.channels}": str(comma(len(guild.channels))),
                "{guild.channel_count}": str(comma(len(guild.channels))),
                "{guild.category_channels}": str(comma(len(guild.categories))),
                "{guild.category_channel_count}": str(comma(len(guild.categories))),
                "{guild.text_channels}": str(comma(len(guild.text_channels))),
                "{guild.text_channel_count}": str(comma(len(guild.text_channels))),
                "{guild.voice_channels}": str(comma(len(guild.voice_channels))),
                "{guild.voice_channel_count}": str(comma(len(guild.voice_channels))),
                "{guild.roles}": str(comma(len(guild.roles))),
                "{guild.role_count}": str(comma(len(guild.roles))),
                "{guild.emojis}": str(comma(len(guild.emojis))),
                "{guild.emoji_count}": str(comma(len(guild.emojis))),
                "{guild.created_at}": self._format_timestamp(guild.created_at),
                "{unix(guild.created_at)}": str(int(guild.created_at.timestamp())),
                "{guild.boost_count}": str(comma(guild.premium_subscription_count))
            }
            for key, value in replacements.items():
                script = script.replace(key, value)

        if channel := kwargs.get("channel"):
            if isinstance(channel, TextChannel):
                script = (
                    script.replace("{channel}", str(channel))
                    .replace("{channel.id}", str(channel.id))
                    .replace("{channel.type}", str(channel.type))
                    .replace("{channel.position}", str(channel.position))
                    .replace("{channel.category}", str(channel.category))
                    .replace("{channel.category.id}", str(channel.category.id))
                    .replace("{channel.category.name}", str(channel.category.name))
                    .replace("{channel.slowmode_delay}", str(channel.slowmode_delay))
                    .replace("{channel.mention}", str(channel.mention))
                    .replace("{channel.name}", str(channel.name))
                    .replace("{channel.topic}", str(channel.topic))
                    .replace("{channel.created_at}", str(channel.created_at))
                    .replace(
                        "{channel.created_at}",
                        str(channel.created_at.strftime("%m/%d/%Y, %I:%M %p")),
                    )
                    .replace(
                        "{unix(channel.created_at)}",
                        str(int(channel.created_at.timestamp())),
                    )
                )
        if role := kwargs.get("role"):
            script = (
                script.replace("{role}", str(role))
                .replace("{role.id}", str(role.id))
                .replace("{role.mention}", str(role.mention))
                .replace("{role.name}", str(role.name))
                .replace("{role.color}", str(role.color))
                .replace("{role.created_at}", str(role.created_at))
                .replace("{role.position}", str(role.position))
                .replace(
                    "{role.created_at}",
                    str(role.created_at.strftime("%m/%d/%Y, %I:%M %p")),
                )
                .replace(
                    "{unix(role.created_at)}",
                    str(int(role.created_at.timestamp())),
                )
            )
        if roles := kwargs.get("roles"):
            script = script.replace(
                "{roles}", " ".join([str(role) for role in roles])
            )
        if user := kwargs.get("user"):
            script = script.replace("{member", "{user")
            script = (
                script.replace("{user}", str(user))
                .replace("{user.id}", str(user.id))
                .replace("{user.mention}", str(user.mention))
                .replace("{user.name}", str(user.name))
                .replace("{user.bot}", "Yes" if user.bot else "No")
                .replace("{user.color}", str(user.color))
                .replace("{user.avatar}", str(user.display_avatar))
                .replace("{user.nickname}", str(user.display_name))
                .replace("{user.nick}", str(user.display_name))
                .replace(
                    "{user.created_at}",
                    str(user.created_at.strftime("%m/%d/%Y, %I:%M %p")),
                )
                .replace(
                    "{unix(user.created_at)}",
                    str(int(user.created_at.timestamp())),
                )
            )
            if isinstance(user, Member):
                script = (
                    script.replace(
                        "{user.joined_at}",
                        str(user.joined_at.strftime("%m/%d/%Y, %I:%M %p")),
                    )
                    .replace("{user.boost}", "Yes" if user.premium_since else "No")
                    .replace(
                        "{user.boosted_at}",
                        (
                            str(user.premium_since.strftime("%m/%d/%Y, %I:%M %p"))
                            if user.premium_since
                            else "Never"
                        ),
                    )
                    .replace(
                        "{unix(user.boosted_at)}",
                        (
                            str(int(user.premium_since.timestamp()))
                            if user.premium_since
                            else "Never"
                        ),
                    )
                    .replace(
                        "{user.boost_since}",
                        (
                            str(user.premium_since.strftime("%m/%d/%Y, %I:%M %p"))
                            if user.premium_since
                            else "Never"
                        ),
                    )
                    .replace(
                        "{unix(user.boost_since)}",
                        (
                            str(int(user.premium_since.timestamp()))
                            if user.premium_since
                            else "Never"
                        ),
                    )
                )
        if moderator := kwargs.get("moderator"):
            script = (
                script.replace("{moderator}", str(moderator))
                .replace("{moderator.id}", str(moderator.id))
                .replace("{moderator.mention}", str(moderator.mention))
                .replace("{moderator.name}", str(moderator.name))
                .replace("{moderator.bot}", "Yes" if moderator.bot else "No")
                .replace("{moderator.color}", str(moderator.color))
                .replace("{moderator.avatar}", str(moderator.display_avatar))
                .replace("{moderator.nickname}", str(moderator.display_name))
                .replace("{moderator.nick}", str(moderator.display_name))
                .replace(
                    "{moderator.created_at}",
                    str(moderator.created_at.strftime("%m/%d/%Y, %I:%M %p")),
                )
                .replace(
                    "{unix(moderator.created_at)}",
                    str(int(moderator.created_at.timestamp())),
                )
            )
            if isinstance(moderator, Member):
                script = (
                    script.replace(
                        "{moderator.joined_at}",
                        str(moderator.joined_at.strftime("%m/%d/%Y, %I:%M %p")),
                    )
                    .replace(
                        "{unix(moderator.joined_at)}",
                        str(int(moderator.joined_at.timestamp())),
                    )
                    .replace(
                        "{moderator.join_position}",
                        str(
                            sorted(guild.members, key=lambda m: m.joined_at).index(
                                moderator
                            )
                            + 1
                        ),
                    )
                    .replace(
                        "{suffix(moderator.join_position)}",
                        str(
                            ordinal(
                                sorted(guild.members, key=lambda m: m.joined_at).index(
                                    moderator
                                )
                                + 1
                            )
                        ),
                    )
                    .replace(
                        "{moderator.boost}",
                        "Yes" if moderator.premium_since else "No",
                    )
                    .replace(
                        "{moderator.boosted_at}",
                        (
                            str(moderator.premium_since.strftime("%m/%d/%Y, %I:%M %p"))
                            if moderator.premium_since
                            else "Never"
                        ),
                    )
                    .replace(
                        "{unix(moderator.boosted_at)}",
                        (
                            str(int(moderator.premium_since.timestamp()))
                            if moderator.premium_since
                            else "Never"
                        ),
                    )
                    .replace(
                        "{moderator.boost_since}",
                        (
                            str(moderator.premium_since.strftime("%m/%d/%Y, %I:%M %p"))
                            if moderator.premium_since
                            else "Never"
                        ),
                    )
                    .replace(
                        "{unix(moderator.boost_since)}",
                        (
                            str(int(moderator.premium_since.timestamp()))
                            if moderator.premium_since
                            else "Never"
                        ),
                    )
                )
        if case_id := kwargs.get("case_id"):
            script = (
                script.replace("{case.id}", str(case_id))
                .replace("{case}", str(case_id))
                .replace("{case_id}", str(case_id))
            )
        if reason := kwargs.get("reason"):
            script = script.replace("{reason}", str(reason))
        if duration := kwargs.get("duration"):
            script = script.replace("{duration}", str(duration))
        if image := kwargs.get("image"):
            script = script.replace("{image}", str(image))
        if option := kwargs.get("option"):
            script = script.replace("{option}", str(option))
        if text := kwargs.get("text"):
            script = script.replace("{text}", str(text))
        if emoji := kwargs.get("emoji"):
            script = (
                script.replace("{emoji}", str(emoji))
                .replace("{emoji.id}", str(emoji.id))
                .replace("{emoji.name}", str(emoji.name))
                .replace("{emoji.animated}", "Yes" if emoji.animated else "No")
                .replace("{emoji.url}", str(emoji.url))
            )
        if emojis := kwargs.get("emojis"):
            script = script.replace("{emojis}", str(emojis))
        if sticker := kwargs.get("sticker"):
            script = (
                script.replace("{sticker}", str(sticker))
                .replace("{sticker.id}", str(sticker.id))
                .replace("{sticker.name}", str(sticker.name))
                .replace("{sticker.animated}", "Yes" if sticker.animated else "No")
                .replace("{sticker.url}", str(sticker.url))
            )
        if color := kwargs.get("color"):
            script = script.replace("{color}", str(color)).replace(
                "{colour}", str(color)
            )
        if name := kwargs.get("name"):
            script = script.replace("{name}", str(name))
        if "hoist" in kwargs:
            hoist = kwargs.get("hoist")
            script = script.replace("{hoisted}", "Yes" if hoist else "No")
            script = script.replace("{hoist}", "Yes" if hoist else "No")
        if "mentionable" in kwargs:
            mentionable = kwargs.get("mentionable")
            script = script.replace(
                "{mentionable}", "Yes" if mentionable else "No"
            )
        if lastfm := kwargs.get("lastfm"):
            script = (
                script.replace("{lastfm}", lastfm.name)
                .replace("{lastfm.name}", lastfm.name)
                .replace("{lastfm.url}", lastfm.url)
                .replace("{lastfm.avatar}", lastfm.avatar or "")
                .replace("{lastfm.plays}", str(comma(lastfm.scrobbles)))
                .replace("{lastfm.scrobbles}", str(comma(lastfm.scrobbles)))
                .replace("{lastfm.library}", str(comma(lastfm.scrobbles)))
                .replace("{lastfm.library.artists}", str(comma(lastfm.artists)))
                .replace("{lastfm.library.albums}", str(comma(lastfm.albums)))
                .replace("{lastfm.library.tracks}", str(comma(lastfm.tracks)))
            )

        if artist := kwargs.get("artist"):
            script = (
                script.replace("{artist}", escape_markdown(artist.name))
                .replace("{artist.name}", escape_markdown(artist.name))
                .replace("{artist.url}", artist.url)
                .replace("{artist.image}", artist.image or "")
                .replace("{artist.plays}", str(comma(artist.scrobbles)))
                .replace("{lower(artist)}", escape_markdown(artist.name.lower()))
                .replace("{lower(artist.name)}", escape_markdown(artist.name.lower()))
                .replace("{upper(artist)}", escape_markdown(artist.name.upper()))
                .replace("{upper(artist.name)}", escape_markdown(artist.name.upper()))
                .replace("{title(artist)}", escape_markdown(artist.name.title()))
                .replace("{title(artist.name)}", escape_markdown(artist.name.title()))
            )

        if album := kwargs.get("album"):
            script = (
                script.replace("{album}", escape_markdown(album.name))
                .replace("{album.name}", escape_markdown(album.name))
                .replace("{album.url}", album.url)
                .replace("{album.image}", album.cover or "")
                .replace("{album.cover}", album.cover or "")
                .replace("{lower(album)}", escape_markdown(album.name.lower()))
                .replace("{lower(album.name)}", escape_markdown(album.name.lower()))
                .replace("{upper(album)}", escape_markdown(album.name.upper()))
                .replace("{upper(album.name)}", escape_markdown(album.name.upper()))
                .replace("{title(album)}", escape_markdown(album.name.title()))
                .replace("{title(album.name)}", escape_markdown(album.name.title()))
            )

        if track := kwargs.get("track"):
            script = (
                script.replace("{track}", escape_markdown(track.name))
                .replace("{track.name}", escape_markdown(track.name))
                .replace("{track.url}", track.url)
                .replace("{track.image}", track.image or "")
                .replace("{track.cover}", track.image or "")
                .replace("{track.plays}", str(comma(track.scrobbles)))
                .replace("{lower(track)}", escape_markdown(track.name.lower()))
                .replace("{lower(track.name)}", escape_markdown(track.name.lower()))
                .replace("{upper(track)}", escape_markdown(track.name.upper()))
                .replace("{upper(track.name)}", escape_markdown(track.name.upper()))
                .replace("{title(track)}", escape_markdown(track.name.title()))
                .replace("{title(track.name)}", escape_markdown(track.name.title()))
            )

        return script

    async def validate_embed(self) -> None:
        """Validate embed constraints."""
        embed = self.objects["embed"]
        errors = []

        if embed.title and len(embed.title) > self._MAX_TITLE_LENGTH:
            errors.append(f"Title exceeds {self._MAX_TITLE_LENGTH} characters")
        
        if embed.description and len(embed.description) > self._MAX_DESCRIPTION_LENGTH:
            errors.append(f"Description exceeds {self._MAX_DESCRIPTION_LENGTH} characters")
        
        if len(embed.fields) > self._MAX_FIELDS:
            errors.append(f"Too many fields (max {self._MAX_FIELDS})")
        
        for idx, field in enumerate(embed.fields, 1):
            if len(field.name) > self._MAX_FIELD_NAME_LENGTH:
                errors.append(f"Field {idx} name exceeds {self._MAX_FIELD_NAME_LENGTH} characters")
            if len(field.value) > self._MAX_FIELD_VALUE_LENGTH:
                errors.append(f"Field {idx} value exceeds {self._MAX_FIELD_VALUE_LENGTH} characters")
        
        if embed.footer and len(embed.footer.text) > self._MAX_FOOTER_LENGTH:
            errors.append(f"Footer exceeds {self._MAX_FOOTER_LENGTH} characters")

        if errors:
            raise EmbedValidationError("\n".join(errors))

    async def safe_parse(self, **kwargs) -> Tuple[Optional[str], Optional[Embed], Optional[List[Button]]]:
        """Safely parse the embed with error handling."""
        try:
            script = await self.resolve_variables(**kwargs)
            await self.resolve_objects(**kwargs)
            await self.validate_embed()
            
            buttons = [
                LinkButton(
                    label=button.get("label", "Click here"),
                    url=button["url"],
                    emoji=button.get("emoji", "🔗")
                )
                for button in self.objects["button"]
                if URL.match(button["url"])
            ]
            
            return self.objects["content"], self.objects["embed"], buttons
        except EmbedValidationError as e:
            return str(e), None, None
        except EmbedParsingError as e:
            return f"Error parsing embed: {str(e)}", None, None
        except Exception as e:
            return f"An unexpected error occurred: {str(e)}", None, None

    async def resolve_objects(self, **kwargs) -> None:
        """Resolve embed objects with error handling."""
        try:
            if self.parser.tags:
                return

            @self.parser.method(
                name="lower",
                usage="(value)",
                aliases=["lowercase", "lowercase"],
            )
            async def lower(_: None, value: str):
                """Convert the value to lowercase"""
                return value.lower()

            @self.parser.method(
                name="if",
                usage="(condition) (value if true) (value if false)",
                aliases=["%"],
            )
            async def if_statement(_: None, condition, output, err=""):
                """If the condition is true, return the output, else return the error"""
                condition, output, err = str(condition), str(output), str(err)
                if output.startswith("{") and not output.endswith("}"):
                    output += "}"
                if err.startswith("{") and not err.endswith("}"):
                    err += "}"

                match True:
                    case _ if "==" in condition:
                        left, right = map(str.lower, map(str.strip, condition.split("==")))
                        return output if left == right else err
                    case _ if "!=" in condition:
                        left, right = map(str.lower, map(str.strip, condition.split("!=")))
                        return output if left != right else err
                    case _ if ">=" in condition:
                        left, right = self._prepare_numeric_comparison(condition, ">=")
                        return output if left >= right else err
                    case _ if "<=" in condition:
                        left, right = self._prepare_numeric_comparison(condition, "<=")
                        return output if left <= right else err
                    case _ if ">" in condition:
                        left, right = self._prepare_numeric_comparison(condition, ">")
                        return output if left > right else err
                    case _ if "<" in condition:
                        left, right = self._prepare_numeric_comparison(condition, "<")
                        return output if left < right else err
                    case _:
                        return output if condition.lower().strip() not in (
                            "null", "no", "false", "none", ""
                        ) else err

            @self.parser.method(
                name="hidden",
                usage="(value)",
                aliases=["hide"],
            )
            async def hidden(_: None, value: str) -> str:
                """Hide the value"""
                return hidden(value)

            @self.parser.method(
                name="quote",
                usage="(value)",
                aliases=["urlencode"],
            )
            async def quote(_: None, value: str) -> str:
                """URL encode the value"""
                return urllib.parse.quote(value, safe="")

            @self.parser.method(
                name="len",
                usage="(value)",
                aliases=["length"],
            )
            async def length(_: None, value: str) -> int:
                """Return the length of the value"""
                if ", " in value:
                    return len(value.split(", "))
                if "," in value:
                    value = value.replace(",", "")
                    if value.isnumeric():
                        return int(value)
                return len(value)

            @self.parser.method(
                name="strip",
                usage="(text) (removal)",
                aliases=["remove"],
            )
            async def strip(_: None, text: str, removal: str) -> str:
                """Strip the text of the specified removal"""
                return text.replace(removal, "")

            @self.parser.method(
                name="random",
                usage="(items)",
                aliases=["choice"],
            )
            async def random(_: None, *items) -> str:
                """Return a random item from the list"""
                return random.choice(items)

        except Exception as e:
            raise EmbedParsingError(f"Failed to parse embed: {str(e)}")

    @staticmethod
    def _prepare_numeric_comparison(condition: str, operator: str) -> tuple[int, int]:
        """Helper method to prepare numeric comparisons."""
        left, right = condition.split(operator)
        left = left.replace(",", "").strip()
        right = right.replace(",", "").strip()
        return int(left), int(right)

    @staticmethod
    def _clean_url(url: str) -> Optional[str]:
        """Validate and clean URL."""
        if not url:
            return None
        url = url.strip()
        return url if URL.match(url) else None

    @staticmethod
    def _clean_image_url(url: str) -> Optional[str]:
        """Validate and clean image URL."""
        if not url:
            return None
        url = url.strip()
        return url if IMAGE_URL.match(url) else None

    async def compile(self, **kwargs):
        """Attempt to compile the script into an object"""
        await self.resolve_variables(**kwargs)
        await self.resolve_objects(**kwargs)
        try:
            self.script = await self.parser.parse(self.script)
            for script in self.script.split("{embed}"):
                if script := script.strip():
                    self.objects["embed"] = Embed()
                    await self.parser.parse(script)
                    if embed := self.objects.pop("embed", None):
                        self.objects["embeds"].append(embed)
            self.objects.pop("embed", None)
        except Exception as error:
            if kwargs.get("validate"):
                if type(error) is TypeError:
                    function = [
                        tag
                        for tag in self.parser.tags
                        if tag.callback.__name__ == error.args[0].split("(")[0]
                    ][0].name
                    parameters = str(error).split("'")[1].split(", ")
                    raise CommandError(
                        f"The **{function}** method requires the `{parameters[0]}` parameter"
                    ) from error
                raise error

        validation = any(self.objects.values())
        if not validation:
            self.objects["content"] = self.script
        if kwargs.get("validate"):
            if self.objects.get("embeds"):
                self._type = "embed"
            self.objects: dict = dict(content=None, embeds=[], stickers=[])
            self.script = self._script

        return validation

    async def send(self, bound: TextChannel, **kwargs):
        """Attempt to send the embed to the channel"""
        compiled = await self.compile(**kwargs)
        if not compiled and not self.script:
            self.objects["content"] = self.script
        if embed := self.objects.pop("embed", None):
            self.objects["embeds"].append(embed)
        if button := self.objects.pop("button", None):
            self.objects["view"] = LinkView(
                links=[LinkButton(**data) for data in button]
            )
        if delete_after := kwargs.get("delete_after"):
            self.objects["delete_after"] = delete_after
        if allowed_mentions := kwargs.get("allowed_mentions"):
            self.objects["allowed_mentions"] = allowed_mentions
        if reference := kwargs.get("reference"):
            self.objects["reference"] = reference
        if isinstance(bound, Webhook) and (ephemeral := kwargs.get("ephemeral")):
            self.objects["ephemeral"] = ephemeral

        return await getattr(bound, ("edit" if isinstance(bound, Message) else "send"))(
            **self.objects,
        )

    def replace(self, key: str, value: str):
        """Replace a key word in the script"""
        self.script = self.script.replace(key, value)
        return self

    def strip(self):
        """Strip the script"""
        self.script = self.script.strip()
        return self

    def type(self, suffix: bool = True, bold: bool = True):
        """Return the script type"""
        if self._type == "embed":
            return (
                "embed"
                if not suffix
                else "an **embed message**" if bold else "an embed"
            )
        return "text" if not suffix else "a **text message**" if bold else "a text"

    def __str__(self):
        return self.script

    def __repr__(self):
        return f"<length={len(self.script)}>"


class EmbedScriptValidator(Converter):
    @staticmethod
    async def convert(ctx: Context, argument: str):
        script = EmbedScript(argument)
        await script.compile(validate=True)
        return script

async def send_embed(ctx: Context, script: str, **kwargs) -> Optional[Message]:
    """Helper function to send an embed with error handling."""
    embed_script = EmbedScript(script)
    content, embed, buttons = await embed_script.safe_parse(**kwargs)
    
    if not embed:
        await ctx.send(
            f"❌ Failed to create embed:\n```\n{content}\n```",
            delete_after=10
        )
        return None
        
    try:
        view = LinkView(buttons) if buttons else None
        return await ctx.send(content=content, embed=embed, view=view)
    except Exception as e:
        await ctx.send(
            f"❌ Failed to send embed: {str(e)}",
            delete_after=10
        )
        return None