from logging import getLogger
from typing import Dict, Optional, Union, TypedDict
from datetime import timedelta
from dataclasses import dataclass
from enum import Enum
import traceback
from opentelemetry import trace

log = getLogger("evict/processors")
tracer = trace.get_tracer(__name__)

class ActionType(Enum):
    HARDUNBAN = "hardunban"
    HARDBAN = "hardban"
    BAN = "ban"
    UNBAN = "unban"
    KICK = "kick"
    JAIL = "jail"
    UNJAIL = "unjail"
    MUTE = "mute"
    UNMUTE = "unmute"
    WARN = "warn"
    TIMEOUT = "timeout"
    UNTIMEOUT = "untimeout"
    ANTINUKE = "antinuke"
    ANTIRAID = "antiraid"

class ActionResult(TypedDict):
    title: str
    is_unaction: bool
    is_antinuke: bool
    is_antiraid: bool
    should_dm: bool
    duration: Optional[str]
    error: Optional[str]

@dataclass
class ModActionData:
    action: str
    duration: Optional[Union[timedelta, int]] = None
    guild_name: Optional[str] = None
    moderator: Optional[str] = None
    reason: Optional[str] = None

def process_mod_action(action_data: Dict[str, Union[str, int, timedelta, None]]) -> ActionResult:
    """
    Process moderation action data with improved error handling and performance.
    
    Args:
        action_data: Dictionary containing action details
        
    Returns:
        ActionResult containing processed action information
    """
    with tracer.start_span("process_mod_action") as span:
        try:
            action = ModActionData(**action_data)
            span.set_attribute("action", action.action)
            
            ACTION_TITLES = {
                ActionType.HARDUNBAN.value: "hardunbanned",
                ActionType.HARDBAN.value: "hardbanned",
                ActionType.BAN.value: "banned",
                ActionType.UNBAN.value: "unbanned",
                ActionType.KICK.value: "kicked",
                ActionType.JAIL.value: "jailed",
                ActionType.UNJAIL.value: "unjailed",
                ActionType.MUTE.value: "muted",
                ActionType.UNMUTE.value: "unmuted",
                ActionType.WARN.value: "warned",
            }
            
            action_title = ACTION_TITLES.get(action.action)
            if not action_title:
                if action.action.startswith(("antinuke", "antiraid")):
                    action_title = "punished"
                else:
                    action_title = f"{action.action}ed"
            
            is_unaction = action.action.startswith('un')
            is_antinuke = action.action.startswith('antinuke')
            is_antiraid = action.action.startswith('antiraid')
            should_dm = action.action not in (ActionType.TIMEOUT.value, ActionType.UNTIMEOUT.value)
            
            span.set_attributes({
                "action_title": action_title,
                "is_unaction": is_unaction,
                "is_antinuke": is_antinuke,
                "is_antiraid": is_antiraid,
                "should_dm": should_dm
            })
            
            log.debug(f"Processed moderation action: {action.action} -> {action_title}")
            
            return ActionResult(
                title=action_title,
                is_unaction=is_unaction,
                is_antinuke=is_antinuke,
                is_antiraid=is_antiraid,
                should_dm=should_dm,
                duration=str(action.duration) if action.duration else None,
                error=None
            )
            
        except Exception as e:
            log.error(f"Error processing mod action: {str(e)}\n{traceback.format_exc()}")
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            return ActionResult(
                title="error",
                is_unaction=False,
                is_antinuke=False,
                is_antiraid=False,
                should_dm=False,
                duration=None,
                error=str(e)
            )

def process_dm_script(script_data: Dict[str, str], default_data: Dict[str, str]) -> Dict[str, str]:
    """
    Process DM notification script with improved formatting and error handling.
    
    Args:
        script_data: Custom script data
        default_data: Default notification data
        
    Returns:
        Dictionary containing the processed script
    """
    with tracer.start_span("process_dm_script") as span:
        try:
            if script_data.get('custom_script'):
                span.set_attribute("script_type", "custom")
                return {'script': script_data['custom_script'], 'error': None}
            
            span.set_attribute("script_type", "default")
            
            action = default_data.get('action', 'actioned')
            guild_name = default_data.get('guild_name', 'the server')
            moderator = default_data.get('moderator', 'A moderator')
            reason = default_data.get('reason', 'No reason provided')
            duration = default_data.get('duration')
            
            script_parts = [
                f"You have been {action} in {guild_name}",
                f"Moderator: {moderator}",
                f"Reason: {reason}"
            ]
            
            if duration:
                script_parts.append(f"Duration: {duration}")
            
            script = "\n".join(script_parts)
            
            log.debug(f"Generated DM script for action: {action}")
            
            return {
                'script': script,
                'error': None
            }
            
        except Exception as e:
            log.error(f"Error processing DM script: {str(e)}\n{traceback.format_exc()}")
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            return {
                'script': "An error occurred while processing the notification.",
                'error': str(e)
            }