from __future__ import annotations

from discord.ext.commands import FlagConverter as OriginalFlagConverter
from typing_extensions import Self
import re

from .context import Context, Embed
from .database import Database, Settings
from .redis import Redis


class FlagConverter(OriginalFlagConverter, case_insensitive=True, prefix="--", delimiter=" "):
    _FLAG_PATTERN = re.compile(r'--(\w+)\s+([^-][^--]*?)(?=\s+--|$)')
    _EM_DASH_PATTERN = re.compile(r'—')
    
    @property
    def values(self) -> list:
        """Get all flag values."""
        return list(self.get_flags().values())

    async def convert(self, ctx: Context, argument: str) -> Self:
        """Convert flags in the argument string."""
        argument = self._EM_DASH_PATTERN.sub('--', argument)
        return await super().convert(ctx, argument)

    async def find(self, ctx: Context, argument: str, *, remove: bool = True) -> tuple[str, Self]:
        """
        Run the conversion and return the result with the remaining string.
        
        Parameters
        ----------
        ctx: Context
            The context for conversion
        argument: str
            The argument string to parse
        remove: bool
            Whether to remove the flags from the original string
            
        Returns
        -------
        tuple[str, Self]
            The remaining string and the converted flags
        """
        argument = self._EM_DASH_PATTERN.sub('--', argument)
        flags = await self.convert(ctx, argument)

        if remove:
            flag_dict = flags.parse_flags(argument)
            
            all_flag_names = set()
            for key, flag in self.get_flags().items():
                all_flag_names.add(key)
                all_flag_names.update(getattr(flag, 'aliases', []))

            for key, values in flag_dict.items():
                value_str = ' '.join(values)
                for flag_name in all_flag_names:
                    flag_pattern = f"--{flag_name} {value_str}"
                    argument = argument.replace(flag_pattern, '')

        return argument.strip(), flags


__all__ = (
    "FlagConverter",
    "Context", 
    "Database",
    "Settings",
    "Redis",
    "Embed"
)