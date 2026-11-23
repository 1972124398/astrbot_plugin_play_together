import asyncio
import importlib.util
import json
import sys
import tempfile
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

    async def test_pause_and_resume_keep_the_same_hidden_number_and_progress(self):
        room = await self.start()
        room.data["answer"] = 17
        await self.plugin.game_input(self.event("12"))
        await self.drain()
        await self.plugin.game_input(self.event("先等一下"))
        await self.drain()
        paused = self.plugin.state.rooms[self.origin]
        self.assertEqual(paused.phase, "paused")
        self.assertEqual(paused.data["left"], 5)
        await self.plugin.game_input(self.event("我回来了"))
        await self.drain()
        self.assertEqual(self.plugin.state.rooms[self.origin].phase, "playing")
        self.assertEqual(self.plugin.state.rooms[self.origin].data["answer"], 17)

    async def test_conceding_reveals_the_answer_and_switching_can_start_a_story(self):
        room = await self.start()
        room.data["answer"] = 17
        await self.plugin.game_input(self.event("我认输啦"))
        await self.drain()
        self.assertIn("17", self.messages[-1][1])
        self.assertFalse(self.plugin.state.rooms)
        await self.start()
        [result async for result in self.plugin.play(self.event("换故事"), "story")]
        await self.drain()
        self.assertEqual(self.plugin.state.rooms[self.origin].game, "story")

    async def test_model_action_returns_facts_without_sending_a_duplicate_message(self):
        room = await self.start()
        room.data["answer"] = 17
        before = len(self.messages)
        result = await self.plugin.model_turn(self.event("要不就猜十七"), "move", "17")
        self.assertIn("被你猜中了", result)
        self.assertEqual(len(self.messages), before)
        self.assertFalse(self.plugin.state.rooms)

    async def test_model_can_supply_both_sides_of_a_natural_story_turn(self):
        await self.start("story")
        before = len(self.messages)
        calls = self.plugin.runtime.generate.await_count
        result = await self.plugin.model_turn(self.event("猫决定报名当侦探"), "move", "猫决定报名当侦探", "鸭子们连夜把雨伞藏进了月亮里。")
        self.assertIn("月亮", result)
        self.assertIn("猫决定报名当侦探", self.plugin.state.rooms[self.origin].data["lines"])
        self.assertEqual(self.plugin.runtime.generate.await_count, calls)
        self.assertEqual(len(self.messages), before)

    async def test_model_tool_only_appears_for_the_current_group_player(self):
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        tool = FunctionTool(name="play_together_turn", description="陪玩", parameters={"type": "object", "properties": {}})
        self.context.get_llm_tool_manager = lambda: SimpleNamespace(get_func=lambda _: tool)
        await self.start(group="group")
        request = ProviderRequest(prompt="这把我想出剪刀")
        self.plugin.model_tools(self.event("这把我想出剪刀", group="group"), request)
        self.assertEqual(request.func_tool.tools[0].name, "play_together_turn")
        hint = request.extra_user_content_parts[0].text
        self.assertNotIn("bot_move", hint)
        other = ProviderRequest(prompt="普通群聊", func_tool=ToolSet([tool]))
        self.plugin.model_tools(self.event("普通群聊", user="other", group="group"), other)
        self.assertFalse(other.func_tool.tools)

    async def test_whole_reply_and_story_opening_are_generated_freely(self):
        self.plugin.config["model_voice"] = True
        self.plugin.runtime.generate.return_value = "这回我要藏得巧一点。你先猜一个吧！"
        await self.start()
        self.assertEqual(self.messages[-1][1], "这回我要藏得巧一点。你先猜一个吧！")
        self.plugin.runtime.generate.return_value = "一只猫忽然把月亮装进了自己的口袋。"
        [result async for result in self.plugin.play(self.event("换故事"), "story")]
        await self.drain()
        self.assertEqual(self.plugin.state.rooms[self.origin].data["lines"][0], "一只猫忽然把月亮装进了自己的口袋。")

    async def test_natural_story_can_continue_past_suggested_rounds_and_end_when_ready(self):
        room = await self.start("story")
        room.data["total"] = 1
        event = self.event("猫把月亮放进罐子")
        await self.plugin.model_turn(event, "move", "猫把月亮放进罐子", "鸭子好奇地敲了敲罐子。")
        self.assertIn(self.origin, self.plugin.state.rooms)
        await self.plugin.model_turn(self.event("最后罐子又打开了"), "move", "最后罐子又打开了", "月亮跳回天上，留了一罐星星给大家。", True)
        self.assertNotIn(self.origin, self.plugin.state.rooms)

    async def test_story_remembers_the_reply_the_model_actually_showed_to_the_player(self):
        await self.start("story")
        event = self.event("猫要去月亮上进货")
        await self.plugin.model_turn(event, "move", "猫要去月亮上进货", "先拿两罐星星试试。")
        actual = "接：我递给它一个背包，说月亮上打折时记得多带两罐星星。"
        await self.plugin.normal_response(event, SimpleNamespace(completion_text=actual))
        room = self.plugin.state.rooms[self.origin]
        self.assertIn("背包", room.data["lines"][-1])
        self.assertEqual(room.last_public, actual)
