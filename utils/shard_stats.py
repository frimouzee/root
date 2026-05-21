from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Dict, Optional
import psutil
import logging

log = logging.getLogger(__name__)

@dataclass
class ShardStats:
    shard_id: int
    latency: float
    guild_count: int
    member_count: int
    channel_count: int
    voice_connections: int
    started_at: datetime
    last_heartbeat: Optional[datetime] = None
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    commands_processed: int = 0
    events_processed: int = 0
    errors_occurred: int = 0

class ShardStatsManager:
    def __init__(self, bot):
        self.bot = bot
        self._stats: Dict[int, ShardStats] = {}
        self._process = psutil.Process()
        
    def initialize_shard(self, shard_id: int):
        """Initialize statistics for a shard."""
        with self.bot.monitoring.tracer.start_as_current_span("init_shard_stats") as span:
            span.set_attribute("shard_id", shard_id)
            self._stats[shard_id] = ShardStats(
                shard_id=shard_id,
                latency=0.0,
                guild_count=0,
                member_count=0,
                channel_count=0,
                voice_connections=0,
                started_at=datetime.now(UTC)
            )

    def update_shard_stats(self, shard_id: int):
        """Update statistics for a specific shard."""
        if shard_id not in self._stats:
            return

        with self.bot.monitoring.tracer.start_as_current_span("update_shard_stats") as span:
            span.set_attribute("shard_id", shard_id)
            
            stats = self._stats[shard_id]
            shard = self.bot.get_shard(shard_id)
            
            if not shard:
                return

            stats.latency = shard.latency
            stats.last_heartbeat = datetime.now(UTC)
            
            guilds = [g for g in self.bot.guilds if g.shard_id == shard_id]
            stats.guild_count = len(guilds)
            stats.member_count = sum(g.member_count for g in guilds)
            stats.channel_count = sum(len(g.channels) for g in guilds)
            stats.voice_connections = len([vc for vc in self.bot.voice_clients 
                                        if vc.guild.shard_id == shard_id])

            try:
                stats.cpu_usage = self._process.cpu_percent() / self.bot.shard_count
                stats.memory_usage = self._process.memory_percent() / self.bot.shard_count
            except Exception as e:
                log.error(f"Failed to update resource usage for shard {shard_id}: {e}")

    def increment_counter(self, shard_id: int, counter_type: str):
        """Increment a specific counter for a shard."""
        if shard_id not in self._stats:
            return

        if counter_type == "commands":
            self._stats[shard_id].commands_processed += 1
        elif counter_type == "events":
            self._stats[shard_id].events_processed += 1
        elif counter_type == "errors":
            self._stats[shard_id].errors_occurred += 1

    def get_shard_stats(self, shard_id: int) -> Optional[ShardStats]:
        """Get statistics for a specific shard."""
        return self._stats.get(shard_id)

    def get_all_stats(self) -> Dict[int, ShardStats]:
        """Get statistics for all shards."""
        return self._stats.copy()

    def get_average_latency(self) -> float:
        """Get average latency across all shards."""
        if not self._stats:
            return 0.0
        return sum(s.latency for s in self._stats.values()) / len(self._stats) 