from logging import getLogger
from typing import Dict, Any
from opentelemetry import trace

log = getLogger(__name__)
tracer = trace.get_tracer(__name__)

def process_guild_data(guild_data: Dict[str, Any]) -> Dict[str, Any]:
    """Process guild statistics in a separate process."""
    with tracer.start_as_current_span("process_guild_data") as span:
        total_members = guild_data['member_count']
        span.set_attribute("total_members", total_members)
        
        bot_count = sum(1 for member in guild_data['members'] if member['bot'])
        user_count = total_members - bot_count
        
        stats = {
            'bot_count': bot_count,
            'user_count': user_count,
            'bot_percentage': round((bot_count / total_members) * 100, 2) if total_members > 0 else 0,
            'user_percentage': round((user_count / total_members) * 100, 2) if total_members > 0 else 0,
            'verification_level': guild_data.get('verification_level', 0),
            'channel_count': len(guild_data.get('channels', [])),
            'role_count': len(guild_data.get('roles', [])),
        }
        
        span.set_attributes({
            "bot_count": bot_count,
            "user_count": user_count
        })
        
        log.info(f"Processed guild stats: {stats}")
        return stats

def process_jail_permissions(channel_id: int, role_id: int) -> Dict[str, bool]:
    """Generate jail permissions for channel and role."""
    with tracer.start_as_current_span("process_jail_permissions") as span:
        span.set_attributes({
            "channel_id": str(channel_id),
            "role_id": str(role_id)
        })
        
        permissions = {
            'view_channel': False,
            'send_messages': False,
            'add_reactions': False,
            'use_external_emojis': False,
            'attach_files': False,
            'embed_links': False,
            'use_application_commands': False
        }
        
        log.info(f"Generated jail permissions for channel {channel_id}, role {role_id}")
        return permissions

def process_add_role(member_data: Dict[str, Any], role_data: Dict[str, Any], reason: str = None) -> bool:
    """Process role addition with error handling."""
    with tracer.start_as_current_span("process_add_role") as span:
        span.set_attributes({
            "member_id": str(member_data['id']),
            "role_id": str(role_data['id']),
            "reason": reason
        })
        
        try:
            log.info(f"Adding role {role_data['id']} to member {member_data['id']}")
            return True
            
        except Exception as e:
            log.error(f"Failed to add role: {e}")
            span.record_exception(e)
            return False 