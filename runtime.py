from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from urllib.parse import unquote

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.platform.astrbot_message import Group
from astrbot.core.platform.message_session import MessageSession

from .interop import CompanionLink


class GameEvent(AstrMessageEvent):
    def __init__(self, session, platform, player="", label=""):
        message = AstrBotMessage()
        message.type = session.message_type
        message.session_id = session.session_id
        message.message_id = "proactive:play_together:" + uuid.uuid4().hex
        message.message = []
        message.message_str = ""
        message.self_id = getattr(platform, "bot_self_id", "")
        message.sender = MessageMember(player, label or None)
        if session.message_type == MessageType.GROUP_MESSAGE:
            message.group = Group(session.session_id)
        super().__init__("", message, platform.meta(), session.session_id)
        self.plugins_name = None
        self.is_wake = self.is_at_or_wake_command = True
        self.set_extra("play_together", True)


class Runtime:
    def __init__(self, context, config):
        self.context, self.config = context, config
        self.link = CompanionLink(context, "play_together", config)

    def platform(self, origin):
        session = MessageSession.from_str(origin)
        platform = self.context.get_platform_inst(session.platform_id)
        if platform is None:
            raise ValueError("The message platform is unavailable")
        if session.message_type not in {MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE}:
            raise ValueError("Choose a private or group conversation")
        return session, platform

    def scope(self, origin):
        session, platform = self.platform(origin)
        if session.message_type == MessageType.GROUP_MESSAGE:
            group = session.session_id
            if platform.meta().name == "wechat_native":
                parts = session.session_id.split("|")
                if len(parts) in {3, 4} and parts[1] == "group":
                    group = unquote(parts[2])
            return session.platform_id + ":group:" + group
        return origin

    def event_scope(self, event):
        if event.get_group_id():
            return event.get_platform_id() + ":group:" + event.get_group_id()
        return event.unified_msg_origin

    def star(self, name):
        star = self.context.get_registered_star(name)
        if star and star.activated and star.star_cls:
            instance = star.star_cls
            if getattr(instance, "config", {}).get("enabled", True):
                return instance
        return None

    def social(self, origin):
        state = self.link.snapshot("self_amusement", origin)
        return state.get("impression", {}), state.get("attraction", 1.0), state.get("wants_space", False)

    def companion_busy(self):
        return not self.link.idle_ready()

    def companion_touch(self, origin, now, hold_until=0):
        self.link.human(origin)

    def mood(self):
        return self.link.snapshot("short_term_mood")

    async def view(self, origin, player="", label=""):
        session, platform = self.platform(origin)
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(origin)
        conversation = await manager.get_conversation(origin, cid) if cid else None
        config = self.context.get_config(umo=origin)
        persona_id, persona, _, special = await self.context.persona_manager.resolve_selected_persona(
            umo=origin, conversation_persona_id=getattr(conversation, "persona_id", None),
            platform_name=platform.meta().name, provider_settings=config.get("provider_settings", {}),
        )
        prompt = str(persona.get("prompt", "")) if persona else ""
        if special:
            from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT
            prompt = CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT
        event = GameEvent(session, platform, player, label)
        event.set_extra("_trace_persona_id", persona_id)
        provider = str(self.config.get("provider_id", "") or "").strip()
        provider = provider or await self.context.get_current_chat_provider_id(origin)
        return SimpleNamespace(origin=origin, event=event, persona=prompt, provider_id=provider)

    async def generate(self, view, system, data):
        data = {**data, "play_preferences": str(self.config.get("play_preferences", "") or "")[:2000]}
        data["companion"] = self.link.context_for(view.origin)
        request = ProviderRequest(system_prompt=view.persona + "\n\n" + system, prompt=json.dumps(data, ensure_ascii=False))
        mood = self.link.peer("short_term_mood", view.origin)
        if mood:
            view.event.set_extra("astrbot_short_term_mood.hint", None)
            await mood.apply_mood(view.event, request)
        response = await self.context.llm_generate(
            chat_provider_id=view.provider_id, prompt=request.prompt, system_prompt=request.system_prompt,
            extra_user_content_parts=request.extra_user_content_parts,
        )
        return response.completion_text.strip()

    async def enabled(self, origin):
        from astrbot.core.star.session_llm_manager import SessionServiceManager
        return (await SessionServiceManager.is_session_enabled(origin)
                and await SessionServiceManager.is_llm_enabled_for_session(origin))

    async def send(self, origin, text):
        session, platform = self.platform(origin)
        chain = MessageChain().message(text)
        if platform.meta().name == "wechat_native":
            from ..astrbot_plugin_wechat_native.event import session_target
            target = session_target(session.session_id, platform.native_config.account_id)
            return bool(await platform.send_chain(target, chain))
        return await self.context.send_message(session, chain)
