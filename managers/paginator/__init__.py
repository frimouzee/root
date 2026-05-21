from __future__ import annotations

import asyncio
from contextlib import suppress
from math import ceil
from typing import TYPE_CHECKING, List, Optional, cast

from discord import ButtonStyle, HTTPException, Interaction, Message
from discord.utils import as_chunks

import config
from utils.tools import Button, View, Embed

if TYPE_CHECKING:
    from core.context import Context


class Paginator(View):
    __slots__ = ("ctx", "entries", "message", "index")

    def __init__(
        self,
        ctx: Context,
        *,
        entries: List[str] | List[dict] | List[Embed],
        embed: Optional[Embed] = None,
        per_page: int = 10,
        counter: bool = True,
        hide_index: bool = False,
    ):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.entries = self.prepare_entries(
            entries, embed, per_page, counter, hide_index
        )
        self.message: Optional[Message] = None
        self.index: int = 0
        self.add_buttons()

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user != self.ctx.author:
            await interaction.warn("You cannot interact with this paginator!")
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message:
            with suppress(HTTPException):
                await self.message.edit(view=None)
        await super().on_timeout()

    def add_buttons(self) -> None:
        buttons = (
            ("previous", config.EMOJIS.CONTEXT.LEFT or "⬅"),
            ("navigation", config.EMOJIS.CONTEXT.FILTER or "🔢"),
            ("next", config.EMOJIS.CONTEXT.RIGHT or "➡"),
            ("cancel", config.EMOJIS.CONTEXT.DENY or "⏹"),
        )
        
        for custom_id, emoji in buttons:
            self.add_item(
                Button(
                    custom_id=custom_id,
                    style=ButtonStyle.secondary,
                    emoji=emoji,
                )
            )

    @staticmethod
    def _format_footer(footer, compiled_len: int, pages: int) -> tuple[str, str | None]:
        if not footer or not footer.text:
            return f"Page {compiled_len + 1} of {pages:,}", None
            
        return " • ".join([
            footer.text,
            f"Page {compiled_len + 1} of {pages:,}"
        ]), footer.icon_url

    def prepare_entries(
        self,
        entries: List[str] | List[dict] | List[Embed],
        embed: Optional[Embed],
        per_page: int,
        counter: bool,
        hide_index: bool = False,
    ) -> List[str] | List[Embed]:
        if not entries and embed:
            return [embed]

        compiled: List[str | Embed] = []
        pages = ceil(len(entries) / per_page)

        if not embed:
            if isinstance(entries[0], str):
                entries = cast(List[str], entries)
                return [
                    f"({i + 1}/{len(entries)}) {entry}" if "page" not in entry and counter and not hide_index 
                    else entry.format(page=i + 1, pages=len(entries))
                    for i, entry in enumerate(entries)
                ]

        elif isinstance(entries[0], str):
            offset = 0
            for chunk in as_chunks(entries, per_page):
                entry = embed.copy()
                if not entry.color:
                    entry.color = self.ctx.color

                entry.description = f"{entry.description or ''}\n\n"
                for value in chunk:
                    offset += 1
                    entry.description += (
                        f"`{offset}` {value}\n" if counter and not hide_index
                        else f"{value}\n"
                    )

                if pages > 1:
                    text, icon_url = self._format_footer(entry.footer, len(compiled), pages)
                    entry.set_footer(text=text, icon_url=icon_url)

                compiled.append(entry)

        elif isinstance(entries[0], dict):
            entries = cast(List[dict], entries)
            for chunk in as_chunks(entries, per_page):
                entry = embed.copy()
                if not entry.color:
                    entry.color = self.ctx.color

                for field in chunk:
                    entry.add_field(**field)

                if pages > 1:
                    text, icon_url = self._format_footer(entry.footer, len(compiled), pages)
                    entry.set_footer(text=text, icon_url=icon_url)

                compiled.append(entry)

        elif isinstance(entries[0], Embed):
            for entry in entries:
                entry = cast(Embed, entry)
                if not entry.color:
                    entry.color = self.ctx.color

                if len(entries) > 1:
                    text, icon_url = self._format_footer(entry.footer, len(compiled), len(entries))
                    entry.set_footer(text=text, icon_url=icon_url)

                compiled.append(entry)

        return compiled

    async def start(self, content: str = None) -> Message:
        if not self.entries:
            raise ValueError("no entries were provided")

        page = self.entries[self.index]
        is_embed = isinstance(page, Embed)

        if len(self.entries) == 1:
            self.message = await self.ctx.send(
                content=content or (None if is_embed else page),
                embed=page if is_embed else None
            )
        else:
            self.message = await self.ctx.send(
                content=content or (None if is_embed else page),
                embed=page if is_embed else None,
                view=self
            )

        return self.message

    async def callback(self, interaction: Interaction, button: Button) -> None:
        await interaction.response.defer()

        match button.custom_id:
            case "previous":
                self.index = len(self.entries) - 1 if self.index <= 0 else self.index - 1
            case "next":
                self.index = 0 if self.index >= (len(self.entries) - 1) else self.index + 1
            case "navigation":
                await self._handle_navigation(interaction)
                return
            case "cancel":
                with suppress(HTTPException):
                    await self.message.delete()
                    await self.ctx.message.delete()
                self.stop()
                return

        page = self.entries[self.index]
        with suppress(HTTPException):
            await self.message.edit(
                content=None if isinstance(page, Embed) else page,
                embed=page if isinstance(page, Embed) else None,
                view=self
            )

    async def _handle_navigation(self, interaction: Interaction) -> None:
        await self.disable_buttons()
        await self.message.edit(view=self)

        embed = Embed(
            title="Page Navigation",
            description="Reply with the page to skip to",
            color=config.COLORS.NEUTRAL
        )
        prompt = await interaction.followup.send(
            embed=embed, ephemeral=True, wait=True
        )
        
        try:
            response = await self.ctx.bot.wait_for(
                "message",
                timeout=6,
                check=lambda m: (
                    m.author == interaction.user
                    and m.channel == interaction.channel
                    and m.content.isdigit()
                    and int(m.content) <= len(self.entries)
                ),
            )
        except asyncio.TimeoutError:
            pass
        else:
            self.index = int(response.content) - 1
            with suppress(HTTPException):
                await response.delete()
        finally:
            for child in self.children:
                child.disabled = False  # type: ignore
            with suppress(HTTPException):
                await prompt.delete() 