from logging import getLogger
from typing import Dict, List, Optional, Any
from datetime import datetime
from asyncio import gather
from discord import Member, Guild
from core import Evict

log = getLogger("evict/processors")

async def process_audit_threshold(
    bot: Evict,
    key: str,
    config: Dict[str, Any],
    member: Member,
    module: str,
    guild: Guild
) -> bool:
    """Process audit threshold check immediately"""
    pipe = bot.redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, config.duration)
    value, _ = await pipe.execute()
    
    return value >= config.threshold

async def process_whitelist_check(bot: Evict, guild: Guild):
    """Process whitelist check immediately"""
    return await bot.db.fetchrow(
        """
        SELECT * FROM antinuke 
        WHERE guild_id = $1
        """,
        guild.id,
    )

async def process_punishment_data(
    bot: Evict,
    perpetrator: Member,
    module: str,
    action: str,
    reason: str,
    guild: Guild,
    current_time: datetime
) -> None:
    """Process punishment data immediately and concurrently"""
    
    await gather(
        bot.db.execute(
            """
            INSERT INTO antinuke_logs (
                guild_id, user_id, module, action, reason, timestamp
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            guild.id,
            perpetrator.id,
            module,
            action, 
            reason,
            current_time
        ),
        
        bot.redis.hincrby(
            f"metrics:antinuke:{guild.id}",
            module,
            1
        )
    )

def process_audit_threshold(
    action_history: List[Dict], 
    threshold: int,
    timeframe: int
) -> bool:
    """
    Process audit log threshold checks in a separate process.
    """
    recent_actions = len(action_history)
    return recent_actions >= threshold

def process_whitelist_check(
    user_id: int,
    whitelist_ids: List[int]
) -> bool:
    """
    Process whitelist checks in a separate process.
    """
    return user_id in whitelist_ids

def process_punishment_data(
    guild_data: Dict,
    module: str,
    action_type: str,
    details: Optional[str] = None
) -> Dict:
    """
    Process punishment data formatting in a separate process.
    """
    punishment_data = {
        'reason': f"{module.title()} {action_type} attempt detected",
        'audit_reason': f"[Antinuke] {module.title()} {action_type} attempt"
    }
    
    if details:
        punishment_data['reason'] += f" | {details}"

    return punishment_data