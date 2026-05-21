from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union, cast, TypeAlias, Final

from discord import (
    Asset,
    Color,
    Guild,
    Member,
    Role,
    Status,
    TextChannel,
    Thread,
    User,
    VoiceChannel,
)
from humanfriendly import format_timespan
from pydantic import BaseModel

TARGET: TypeAlias = Union[
    Member, User, Role, Guild, VoiceChannel, TextChannel, Thread, BaseModel, str
]

VARIABLE: Final = re.compile(r"(?<!\\)\{([a-zA-Z0-9_.]+)\}")
CACHE_SIZE: Final = 100 

class VariableCache:
    """LRU Cache for variable dictionaries"""
    def __init__(self, maxsize: int = CACHE_SIZE):
        self.cache: Dict[int, Dict[str, str]] = {}
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0

    def get(self, target: TARGET, key: Optional[str] = None) -> Optional[Dict[str, str]]:
        cache_key = hash((id(target), key))
        if cache_key in self.cache:
            self.hits += 1
            return self.cache[cache_key]
        self.misses += 1
        return None

    def set(self, target: TARGET, data: Dict[str, str], key: Optional[str] = None) -> None:
        cache_key = hash((id(target), key))
        if len(self.cache) >= self.maxsize:
            self.cache.pop(next(iter(self.cache)))
        self.cache[cache_key] = data

_variable_cache = VariableCache()

def to_dict(
    target: TARGET,
    _key: Optional[str] = None,
) -> Dict[str, str]:
    """
    Compile a dictionary of safe attributes.
    
    Args:
        target: The target object to extract variables from
        _key: Optional key prefix for the variables
        
    Returns:
        Dictionary of variable names to their string values
    """
    if cached := _variable_cache.get(target, _key):
        return cached.copy() 

    origin = target.__class__.__name__.lower()
    key = _key or getattr(target, "_variable", origin)
    key = "user" if key == "member" else "channel" if "channel" in key else key

    data: Dict[str, str] = {
        key: str(target),
    }

    for name in dir(target):
        if name.startswith("_"):
            continue

        try:
            value = getattr(target, name)
        except (ValueError, AttributeError):
            continue

        if callable(value):
            continue

        var_key = f"{key}.{name}"
        
        match value:
            case datetime():
                data[var_key] = str(int(value.timestamp()))
            case timedelta():
                data[var_key] = format_timespan(value)
            case int():
                data[var_key] = (
                    format(value, ",")
                    if not name.endswith(("id", "duration"))
                    else str(value)
                )
            case str() | bool() | Status() | Asset() | Color():
                data[var_key] = str(value)
            case BaseModel():
                base_model_data = to_dict(value)
                data.update({f"{key}.{k}": v for k, v in base_model_data.items()})

    _variable_cache.set(target, data, _key)
    return data

def parse(string: str, targets: List[TARGET | Tuple[TARGET, str]]) -> str:
    """
    Parse a string with a given environment.
    
    Args:
        string: The string template to parse
        targets: List of targets or (target, key) tuples to extract variables from
        
    Returns:
        The parsed string with variables replaced
    """
    variables: Dict[str, str] = {}
    
    for target in targets:
        if isinstance(target, tuple):
            variables.update(to_dict(*target))
        else:
            variables.update(to_dict(target))

    def replace(match: re.Match) -> str:
        name = cast(str, match[1])
        return variables.get(name, name)

    return VARIABLE.sub(replace, string)

def clear_cache() -> None:
    """Clear the variable cache"""
    global _variable_cache
    _variable_cache = VariableCache()

def get_cache_stats() -> Tuple[int, int]:
    """Get cache hit/miss statistics"""
    return _variable_cache.hits, _variable_cache.misses