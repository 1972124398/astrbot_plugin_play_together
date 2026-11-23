import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
SDK_AVAILABLE = importlib.util.find_spec("astrbot") is not None
if SDK_AVAILABLE:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.platform.astrbot_message import Group
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot_plugin_play_together.main import PlayTogether
    from astrbot_plugin_play_together.routing import PlayInputFilter
    from astrbot_plugin_play_together.state import Target


@unittest.skipUnless(SDK_AVAILABLE, "Requires AstrBot SDK")
class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.messages = []
        self.origin = "test:FriendMessage:person"
        self.context = SimpleNamespace(get_config=lambda **_: {"plugin_set": ["*"]})
        with patch("astrbot_plugin_play_together.main.StarTools.get_data_dir", return_value=Path(self.folder.name)):
            self.plugin = PlayTogether(self.context, {"model_voice": False, "targets": [self.origin], "active_start_hour": 0, "active_end_hour": 0})

        async def send(origin, text):
            self.messages.append((origin, text))
            return True

        self.plugin.runtime = SimpleNamespace(
            event_scope=lambda event: event.unified_msg_origin if not event.get_group_id() else "test:group:" + event.get_group_id(),
            scope=lambda origin: "test:group:" + origin.split(":", 2)[2] if "GroupMessage" in origin else origin,
            platform=lambda origin: (None, SimpleNamespace(meta=lambda: PlatformMetadata("test", "test", "test"))),
            view=AsyncMock(return_value=SimpleNamespace()), generate=AsyncMock(return_value="突然，门自己变成了一只小鸭子。"),
            send=AsyncMock(side_effect=send), enabled=AsyncMock(return_value=True),
            companion_touch=lambda *args: None, companion_busy=lambda: False,
            social=lambda origin: ({}, 1, False), mood=lambda: {},
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.folder.cleanup()

    def event(self, text="", user="person", group=""):
        obj = AstrBotMessage()
        obj.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        obj.self_id = "bot"
        obj.session_id = group or user
        obj.message_id = uuid.uuid4().hex
        obj.message = []
        obj.sender = MessageMember(user, user)
        obj.message_str = text
        obj.group = Group(group) if group else None
        event = AstrMessageEvent(text, obj, PlatformMetadata("test", "test", "test"), obj.session_id)
        return event

    async def drain(self):
        for _ in range(20):
            tasks = [task for task in self.plugin.workers.values() if not task.done()]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            await asyncio.sleep(0)
            if not self.plugin.queues and not self.plugin.workers:
                return
        self.fail("Game queue did not finish")

    async def start(self, game="number", group=""):
        event = self.event("/play", group=group)
        replies = [result async for result in self.plugin.play(event, game)]
        self.assertTrue(replies)
        await self.drain()
        return self.plugin.room_for(event)

    async def test_number_game_runs_through_commands_plain_guesses_and_replay(self):
        room = await self.start()
        room.data["answer"] = 17
        for text in ("12", "17"):
            event = self.event(text)
            self.assertTrue(self.plugin.match(event))
            await self.plugin.game_input(event)
            self.assertTrue(event.is_stopped())
            await self.drain()
        self.assertIn("比 12 大", self.messages[-2][1])
        self.assertIn("被你猜中了", self.messages[-1][1])
        self.assertFalse(self.plugin.state.rooms)
        again = self.event("再来一局")
        await self.plugin.game_input(again)
        await self.drain()
        self.assertEqual(self.plugin.room_for(again).game, "number")

    async def test_unrelated_text_and_other_plugin_commands_do_not_match(self):
        await self.start()
        for text in ("今天想吃什么", "/codex 帮我改代码", "我要买两个苹果", "12 行代码有问题"):
            self.assertFalse(PlayInputFilter().filter(self.event(text), {}))
        self.assertTrue(PlayInputFilter().filter(self.event("我猜15"), {}))

    async def test_group_game_only_accepts_current_player_and_keeps_group_scope(self):
        room = await self.start(group="group")
        self.assertTrue(self.plugin.match(self.event("12", group="group")))
        self.assertFalse(self.plugin.match(self.event("12", user="other", group="group")))
        self.assertEqual(room.scope, "test:group:group")

    async def test_story_only_claims_explicit_continuations(self):
        await self.start("story")
        self.assertFalse(self.plugin.match(self.event("你觉得这个故事怎么样")))
        event = self.event("接：然后猫把钥匙吞了")
        await self.plugin.game_input(event)
        await self.drain()
        self.assertIn("我接：", self.messages[-1][1])
        self.assertIn("猫把钥匙吞了", str(self.plugin.state.rooms[self.origin].data))

    async def test_duplicate_game_input_is_only_counted_once(self):
        room = await self.start()
        room.data["answer"] = 30
        event = self.event("12")
        await self.plugin.game_input(event)
        await self.drain()
        await self.plugin.game_input(event)
        await self.drain()
        self.assertEqual(self.plugin.state.rooms[self.origin].data["left"], 5)

    async def test_natural_start_is_selective_and_works_without_target_configuration(self):
        self.plugin.config["targets"] = []
        self.assertFalse(self.plugin.match(self.event("可以陪我玩一个小游戏吗")))
        event = self.event("陪我玩")
        self.assertTrue(self.plugin.match(event))
        await self.plugin.game_input(event)
        await self.drain()
        self.assertTrue(self.plugin.state.rooms)

    async def test_background_invitation_waits_for_acceptance_then_locks_the_player(self):
        await self.plugin.auto_invite()
        room = self.plugin.state.rooms[self.origin]
        self.assertEqual(room.phase, "invite")
        self.assertIn("来一局", self.messages[-1][1])
        event = self.event("好呀")
        await self.plugin.game_input(event)
        await self.drain()
        self.assertEqual(self.plugin.state.rooms[self.origin].player_id, "person")
        self.assertEqual(self.plugin.state.rooms[self.origin].phase, "playing")

    async def test_asking_how_to_play_keeps_the_invitation_and_can_pick_another_game(self):
        await self.plugin.auto_invite()
        event = self.event("怎么玩呀")
        await self.plugin.normal_request(event, ProviderRequest(prompt="怎么玩呀"))
        self.assertEqual(self.plugin.state.rooms[self.origin].phase, "invite")
        await self.plugin.normal_response(event, SimpleNamespace())
        choice = self.event("故事接龙")
        self.assertTrue(self.plugin.match(choice))
        await self.plugin.game_input(choice)
        await self.drain()
        self.assertEqual(self.plugin.state.rooms[self.origin].game, "story")

    async def test_unanswered_invitation_is_not_resent_after_expiry(self):
        await self.plugin.auto_invite()
        room = self.plugin.state.rooms[self.origin]
        self.plugin.state.expire(room.expires + 1)
        self.plugin.state.next_invite = 0
        self.plugin.config["target_gap_minutes"] = 10
        self.plugin.state.targets[self.origin].last_invited = 0
        await self.plugin.auto_invite()
        self.assertEqual(len(self.messages), 1)

    async def test_stop_can_interrupt_a_slow_opening_without_leaking_it_later(self):
        self.plugin.config["model_voice"] = True
        started = asyncio.Event()

        async def slow(*args):
            started.set()
            await asyncio.Event().wait()

        self.plugin.runtime.generate.side_effect = slow
        [result async for result in self.plugin.play(self.event("/play"), "number")]
        await asyncio.wait_for(started.wait(), 1)
        stop = self.event("不玩了")
        await self.plugin.game_input(stop)
        await self.drain()
        self.assertFalse(self.plugin.state.rooms)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("先收起来", self.messages[0][1])

    async def test_immediate_stop_before_worker_starts_does_not_stall(self):
        [result async for result in self.plugin.play(self.event("/play"), "number")]
        await self.plugin.game_input(self.event("不玩了"))
        await self.drain()
        self.assertFalse(self.plugin.state.rooms)
        self.assertIn("先收起来", self.messages[-1][1])

    async def test_ordinary_request_keeps_input_and_tools_and_adds_temporary_game_context(self):
        self.plugin.state.targets[self.origin] = Target(last_result="刚才猜中了17")
        request = ProviderRequest(prompt="刚才好好玩", system_prompt="原人设")
        tools = object()
        request.func_tool = tools
        event = self.event("刚才好好玩")
        await self.plugin.normal_request(event, request)
        self.assertEqual(request.prompt, "刚才好好玩")
        self.assertEqual(request.system_prompt, "原人设")
        self.assertIs(request.func_tool, tools)
        self.assertTrue(request.extra_user_content_parts[0].model_dump_for_context()["_no_save"])

    async def test_model_failure_falls_back_and_never_changes_the_number_rules(self):
        self.plugin.config["model_voice"] = True
        self.plugin.runtime.generate.side_effect = ValueError("model unavailable")
        room = await self.start()
        self.assertEqual(room.data["left"], 6)
        self.assertIn("1～30", self.messages[-1][1])

    async def test_configuration_uses_one_target_picker_and_three_game_choices(self):
        from astrbot.api import AstrBotConfig

        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
        config = AstrBotConfig(str(Path(self.folder.name) / "config.json"), schema=schema)
        self.assertEqual(schema["targets"]["_special"], "umo")
        self.assertEqual(config["targets"], [])
        self.assertEqual(config["games"], ["number", "rps", "story"])
