from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional, List, Callable
from datetime import datetime, timedelta
from utils.logger import log
from opentelemetry import trace
from redis.asyncio import Redis
import config
import discord

class ClusterIPC:
    def __init__(self, bot, cluster_id: int):
        self.bot = bot
        self.cluster_id = cluster_id
        self.redis: Optional[Redis] = None
        self.pubsub = None
        self.handlers: Dict[str, Callable] = {}
        self.listener_task = None
        self.heartbeat_task = None
        self._stop_event = asyncio.Event()
        self.last_heartbeat = 0
        self.tracer = trace.get_tracer(__name__)
        self._message_lock = asyncio.Lock()
        log.info(f"IPC initialized for cluster {cluster_id}")

    async def start(self):
        """Start the IPC system."""
        try:
            self.redis = Redis.from_url(config.REDIS.DSN)
            
            self.pubsub = self.redis.pubsub()
            log.info("Created Redis pubsub")
            
            await self.pubsub.subscribe(f"cluster_{self.cluster_id}")
            log.info(f"Subscribed to cluster_{self.cluster_id}")
            
            await asyncio.sleep(0.1)
            
            self.add_handler("get_member_info", self.get_member_info)
            self.add_handler("get_guild_info", self.get_guild_info)
            self.add_handler("create_appeal_channels", self.create_appeal_channels)
            
            self.listener_task = asyncio.create_task(
                self._listen(), 
                name=f"ipc_listener_{self.cluster_id}"
            )
            log.info("Created listener task")
            
            await self.send_heartbeat()
            log.info("Sent initial heartbeat")
            
            self.heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"ipc_heartbeat_{self.cluster_id}"
            )
            log.info("Started heartbeat task")
            
            log.info("IPC fully initialized")
            
        except Exception as e:
            log.error(f"Failed to start IPC: {e}", exc_info=True)
            if self.pubsub:
                await self.pubsub.close()
            if self.redis:
                await self.redis.close()
            raise

    async def _listen(self):
        """Listen for messages."""
        try:
            while not self._stop_event.is_set():
                try:
                    message = await asyncio.wait_for(
                        self.pubsub.get_message(ignore_subscribe_messages=True),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    continue
                    
                if message is None:
                    continue
                    
                try:
                    async with self._message_lock:
                        data = json.loads(message['data'])
                        log.info(f"Cluster {self.cluster_id} received message: {data}")
                        
                        if data.get('source_cluster') == self.cluster_id:
                            log.debug(f"Skipping message from self")
                            continue
                            
                        if handler := self.handlers.get(data['command']):
                            log.info(f"Found handler for {data['command']}")
                            response = await handler(data.get('data', {}))
                            if response_channel := data.get('response_channel'):
                                response_message = json.dumps({
                                    'cluster_id': self.cluster_id,
                                    'data': response,
                                    'timestamp': datetime.utcnow().isoformat()
                                })
                                log.info(f"Sending response on channel {response_channel}: {response_message}")
                                await self.redis.publish(response_channel, response_message)
                        else:
                            log.warning(f"No handler found for command: {data['command']}")
                            log.debug(f"Available handlers: {list(self.handlers.keys())}")
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    log.error(f"Error handling IPC message: {e}", exc_info=True)
                    
        except asyncio.CancelledError:
            log.info("IPC listener cancelled")
        except Exception as e:
            log.error(f"IPC listener error: {e}", exc_info=True)
        finally:
            await self.cleanup()

    async def _heartbeat_loop(self):
        """Send periodic heartbeats."""
        try:
            while not self._stop_event.is_set():
                await self.send_heartbeat()
                await asyncio.sleep(30)  
        except asyncio.CancelledError:
            log.info("Heartbeat loop cancelled")
        except Exception as e:
            log.error(f"Heartbeat loop error: {e}", exc_info=True)

    async def send_heartbeat(self):
        """Send a heartbeat message."""
        try:
            await self.redis.publish(
                "cluster_heartbeat",
                json.dumps({
                    "cluster_id": self.cluster_id,
                    "shard_count": len(self.bot.shards),
                    "guilds": len(self.bot.guilds),
                    "users": len(self.bot.users),
                    "timestamp": asyncio.get_event_loop().time()
                })
            )
            self.last_heartbeat = asyncio.get_event_loop().time()
            log.debug(f"Sent heartbeat for cluster {self.cluster_id}")
        except Exception as e:
            log.error(f"Failed to send heartbeat: {e}", exc_info=True)

    def add_handler(self, action: str, handler: Callable):
        """Add a message handler."""
        self.handlers[action] = handler
        log.info(f"Added handler for {action}")

    async def cleanup(self):
        """Cleanup IPC resources."""
        self._stop_event.set()
        
        if self.listener_task:
            self.listener_task.cancel()
            
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            
        if self.pubsub:
            await self.pubsub.unsubscribe()
            await self.pubsub.close()
            
        if self.redis:
            await self.redis.close()
            
        log.info("IPC cleanup complete")

    async def broadcast(self, command: str, data: Optional[Dict[str, Any]] = None) -> Dict[int, Any]:
        """
        Broadcast message to all clusters and wait for responses.
        Returns a dictionary mapping cluster IDs to their responses.
        """
        with self.tracer.start_as_current_span("ipc_broadcast") as span:
            span.set_attribute("ipc.command", command)
            
            temp_redis = Redis.from_url(config.REDIS.DSN)
            temp_pubsub = temp_redis.pubsub()
            
            response_channel = f"response_{command}_{self.cluster_id}_{datetime.utcnow().timestamp()}"
            log.info(f"Broadcasting on response channel: {response_channel}")
            
            await temp_pubsub.subscribe(response_channel)
            log.info("Subscribed to response channel")
            
            message = json.dumps({
                "command": command,
                "data": data or {},
                "timestamp": datetime.utcnow().isoformat(),
                "source_cluster": self.cluster_id,
                "response_channel": response_channel
            })
            
            responses = {}
            try:
                log.info(f"Sending to {self.bot.cluster_count} clusters")
                for cluster_id in range(self.bot.cluster_count):
                    if cluster_id != self.cluster_id: 
                        await self.redis.publish(f"cluster_{cluster_id}", message)
                        log.info(f"Sent to cluster {cluster_id}")
                
                if handler := self.handlers.get(command):
                    own_response = await handler(data or {})
                    responses[self.cluster_id] = own_response
                    log.info(f"Added own response: {own_response}")
                
                start_time = asyncio.get_event_loop().time()
                
                while len(responses) < self.bot.cluster_count:
                    if asyncio.get_event_loop().time() - start_time > 5.0:
                        log.warning(f"Broadcast timeout. Got {len(responses)} responses")
                        break
                        
                    try:
                        response = await asyncio.wait_for(
                            temp_pubsub.get_message(ignore_subscribe_messages=True),
                            timeout=0.1
                        )
                    except asyncio.TimeoutError:
                        continue
                        
                    if response and response['type'] == 'message':
                        try:
                            response_data = json.loads(response['data'])
                            log.info(f"Parsed response from cluster {response_data.get('cluster_id')}: {response_data}")
                            if 'cluster_id' in response_data and 'data' in response_data:
                                responses[response_data['cluster_id']] = response_data['data']
                        except (json.JSONDecodeError, KeyError) as e:
                            log.error(f"Error parsing response: {e}")
                            continue
                    
            finally:
                await temp_pubsub.unsubscribe(response_channel)
                await temp_pubsub.close()
                await temp_redis.close()
                log.info(f"Broadcast complete. Got {len(responses)} responses")
                
            return responses

    async def send_to_cluster(self, cluster_id: int, command: str, data: Optional[Dict[str, Any]] = None):
        """Send message to specific cluster."""
        with self.tracer.start_as_current_span("ipc_send") as span:
            span.set_attribute("ipc.command", command)
            span.set_attribute("ipc.target_cluster", cluster_id)
            
            message = json.dumps({
                "command": command,
                "data": data or {},
                "timestamp": datetime.utcnow().isoformat(),
                "source_cluster": self.cluster_id
            })
            
            log.info(f"Sending message to cluster {cluster_id}: {message}")
            await self.redis.publish(f"cluster_{cluster_id}", message)
            log.info(f"Message sent to cluster {cluster_id}")

    async def get_cluster_status(self) -> List[Dict[str, Any]]:
        """Get status of all clusters."""
        with self.tracer.start_as_current_span("ipc_status"):
            status = []
            for cluster_id in range(self.bot.cluster_count):
                last_heartbeat = await self.redis.get(f"cluster_{cluster_id}_heartbeat")
                if last_heartbeat:
                    last_heartbeat = datetime.fromisoformat(last_heartbeat)
                    is_alive = datetime.utcnow() - last_heartbeat < timedelta(seconds=30)
                else:
                    is_alive = False
                    
                status.append({
                    "cluster_id": cluster_id,
                    "alive": is_alive,
                    "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None
                })
            return status 

    async def get_guild_info(self, ctx, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            await ctx.send("Guild not found.")
            return
        
        info = {
            'id': guild.id,
            'name': guild.name,
            'me': {
                'permissions': guild.me.guild_permissions.value
            }
        }
        return info

    async def get_member_info(self, data: dict) -> Optional[dict]:
        """Get member info for a guild"""
        guild = self.bot.get_guild(data['guild_id'])
        if not guild:
            return None
            
        member = guild.get_member(data['user_id'])
        if not member:
            return None
            
        return {
            'id': member.id,
            'administrator': member.guild_permissions.administrator
        }

    async def create_appeal_channels(self, ctx, appeal_server_id: int, guild_id: int):
        guild = self.bot.get_guild(appeal_server_id)
        if not guild:
            return None
        
        appeal_channel = await guild.create_text_channel("appeals")
        logs_channel = await guild.create_text_channel(
            "appeal-logs",
            overwrites={
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
        )
        
        embed = discord.Embed(
            title="Appeal System",
            description="Click the button below to submit an appeal.",
            color=discord.Color.blurple()
        )
        view = discord.ui.View(timeout=None)
        view.add_item(AppealButton(modal=True, action_type=None, guild_id=guild_id))
        await appeal_channel.send(embed=embed, view=view)
        
        return {
            'appeal_channel_id': appeal_channel.id,
            'logs_channel_id': logs_channel.id
        } 