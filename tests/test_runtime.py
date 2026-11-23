import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
SDK_AVAILABLE = importlib.util.find_spec("astrbot") is not None
if SDK_AVAILABLE:
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot_plugin_play_together.runtime import Runtime


@unittest.skipUnless(SDK_AVAILABLE, "Requires AstrBot SDK")
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.platform = SimpleNamespace(meta=lambda: PlatformMetadata("test", "test", "test"), bot_self_id="bot")
        self.context = SimpleNamespace(
            get_platform_inst=lambda _: self.platform, get_registered_star=lambda _: None,
            get_config=lambda **_: {},
            conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value=None)),
            persona_manager=SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=("friend", {"prompt": "一个爱接梗的朋友"}, None, False))),
            get_current_chat_provider_id=AsyncMock(return_value="model"),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="这回我要认真一点。")),
            send_message=AsyncMock(return_value=True),
        )
        self.runtime = Runtime(self.context, {"play_preferences": "故事喜欢奇怪的猫"})

    async def test_persona_and_preferences_reach_the_model_without_game_secrets(self):
        view = await self.runtime.view("test:FriendMessage:person", "person")
        text = await self.runtime.generate(view, "只说一句玩笑", {"game": "猜数字", "moment": "opening"})
        arguments = self.context.llm_generate.call_args.kwargs
        self.assertIn("爱接梗", arguments["system_prompt"])
        self.assertIn("奇怪的猫", arguments["prompt"])
        self.assertNotIn("answer", arguments["prompt"])
        self.assertTrue(view.event.message_obj.message_id.startswith("proactive:"))

    async def test_native_group_scope_accepts_group_member_session_ids(self):
        self.platform.meta = lambda: PlatformMetadata("wechat_native", "native", "test")
        scope = self.runtime.scope("test:GroupMessage:account|group|room%40chatroom|person")
        self.assertEqual(scope, "test:group:room@chatroom")

    async def test_generic_delivery_uses_the_existing_platform_router(self):
        self.assertTrue(await self.runtime.send("test:FriendMessage:person", "来玩一局？"))
        self.context.send_message.assert_awaited_once()

    async def test_companion_idle_time_is_held_until_the_active_game_can_rest(self):
        companion = SimpleNamespace(state=SimpleNamespace(engage=lambda *args, **kwargs: None, next_wake=50),
                                    idle_delay=lambda: 100, changed=lambda: None, wake=SimpleNamespace(set=lambda: None))
        self.context.get_registered_star = lambda _: SimpleNamespace(activated=True, star_cls=companion)
        self.runtime.companion_touch("test:FriendMessage:person", 1000, 2000)
        self.assertEqual(companion.state.next_wake, 2000)
