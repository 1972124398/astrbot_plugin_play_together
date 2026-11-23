"""Exercise the four real plugin classes against AstrBot's installed SDK."""
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

siblings=Path(__file__).resolve().parents[2]
if importlib.util.find_spec('astrbot') is None or not all((siblings / ('astrbot_plugin_'+role) / 'main.py').is_file() for role in ('play_together','self_amusement','short_term_mood','codex_bridge')):
    raise unittest.SkipTest('Requires AstrBot SDK and the four companion repositories side by side')
sys.path.insert(0, str(siblings))
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot_plugin_play_together.main import PlayTogether
from astrbot_plugin_self_amusement.main import SelfAmusement
from astrbot_plugin_short_term_mood.main import ShortTermMoodPlugin
from astrbot_plugin_short_term_mood.mood import EMOTIONS
from astrbot_plugin_short_term_mood.prompts import APPRAISAL_SYSTEM
from astrbot_plugin_self_amusement.social import REFLECTION_SYSTEM
from astrbot_plugin_self_amusement.prompts import PLAN_SYSTEM
from astrbot_plugin_codex_bridge.main import CodexBridge
from astrbot_plugin_codex_bridge.bridge import JobStore
from astrbot_plugin_codex_bridge.protocol import event_payload
from astrbot_plugin_codex_bridge.relay import AstrBotRelay, make_event


class TogetherTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_agent_loop_stops_silent_notice_without_sending_tool_status(self):
        from astrbot.core.agent.hooks import BaseAgentRunHooks
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.astr_agent_run_util import run_agent
        from astrbot.api.provider import LLMResponse

        event = make_event(self.context, self.origin, event_payload("silent-loop", {"kind": "task_progress"}))
        tool = FunctionTool(name="mood_ignore_codex_notice", description="先放下", parameters={"type": "object", "properties": {}})
        plugin = self.mood

        class Executor:
            async def execute(self, tool, run_context, **kwargs):
                from mcp.types import CallToolResult, TextContent
                result = await plugin.ignore_codex_notice(run_context.context.event, "这会儿不想听")
                yield CallToolResult(content=[TextContent(type="text", text=result)])

        provider = SimpleNamespace(provider_config={"id": "notice-test", "max_context_tokens": 0},
            text_chat=AsyncMock(return_value=LLMResponse(role="assistant", completion_text="",
                tools_call_name=[tool.name], tools_call_args=[{}], tools_call_ids=["silence-call"])))
        runner = ToolLoopAgentRunner()
        await runner.reset(provider=provider, request=ProviderRequest(prompt=event.get_message_str(), func_tool=ToolSet([tool])),
            run_context=ContextWrapper(context=SimpleNamespace(event=event)), tool_executor=Executor(),
            agent_hooks=BaseAgentRunHooks(), streaming=False)
        async with asyncio.timeout(5):
            async for chain in run_agent(runner, show_tool_use=True, show_tool_call_result=False):
                if chain:
                    await event.send(chain)
        self.assertTrue(runner.was_aborted())
        provider.text_chat.assert_awaited_once()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(event.get_extra("w1_notice_disposition")["action"], "ignore")

    async def test_ignored_codex_notice_does_not_erase_results_or_count_as_outbound_chat(self):
        self.bridge.store.add("检查代码", "", None, 0, self.origin)
        event = make_event(self.context, self.origin, event_payload("ignored-one", {
            "kind": "task_result", "state": "completed", "result": "测试通过",
        }))
        before_sent = self.leisure.state.last_outbound
        before_time = self.leisure.state.targets[self.origin].last_sent
        before_gap = self.bridge.link.state["last_sent"]
        request = ProviderRequest(prompt=event.get_message_str())
        await self.play.normal_request(event, request)
        await self.leisure.observe_request(event, request)
        await self.mood.apply_mood(event, request)
        self.assertEqual(self.leisure.state.last_outbound, before_sent)
        outcome = await self.mood.ignore_codex_notice(event, "这会儿有点累，不想接这个")
        self.assertIn("已先放下", outcome)
        self.assertTrue(event.get_extra("agent_stop_requested"))
        self.assertEqual(event.get_extra("w1_notice_disposition")["action"], "ignore")
        response = SimpleNamespace(completion_text="")
        await self.play.agent_done(event, None, response)
        await self.leisure.observe_response(event, response)
        await self.mood.finish_agent(event, None, response)
        await self.bridge.companion_done(event, None, response)
        self.assertEqual(self.leisure.state.last_outbound, before_sent)
        self.assertEqual(self.leisure.state.targets[self.origin].last_sent, before_time)
        self.assertEqual(self.bridge.link.state["last_sent"], before_gap)
        self.assertFalse(self.leisure.reflection_jobs)
        self.assertTrue(self.bridge.store.recent())
        self.assertFalse(self.sent)
        await self.drain()

    async def test_ignore_notice_tool_is_unavailable_for_real_user_chat_and_obeys_configuration(self):
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        tool = FunctionTool(name="mood_ignore_codex_notice", description="先放下播报", parameters={"type": "object", "properties": {}})
        self.context.get_llm_tool_manager = lambda: SimpleNamespace(get_func=lambda _: tool)
        real = self.event("陪我聊会儿")
        request = ProviderRequest(prompt=real.get_message_str(), func_tool=ToolSet([tool]))
        await self.mood.scope_notice_tools(real, request)
        self.assertIsNone(request.func_tool.get_tool(tool.name))
        await self.mood.ignore_codex_notice(real)
        self.assertIsNone(real.get_extra("agent_stop_requested"))
        background = self.event("任务完成", task=True)
        await self.mood.scope_notice_tools(background, request)
        self.assertIsNotNone(request.func_tool.get_tool(tool.name))
        self.mood.config["allow_codex_notice_refusal"] = False
        await self.mood.scope_notice_tools(background, request)
        self.assertIsNone(request.func_tool.get_tool(tool.name))
        await self.mood.ignore_codex_notice(background)
        self.assertIsNone(background.get_extra("agent_stop_requested"))

    async def test_real_codex_event_has_source_in_transport_prompt_and_all_observers(self):
        self.context.get_llm_tool_manager = lambda: SimpleNamespace(get_func=lambda _: None)
        self.bridge.relay = AstrBotRelay(self.context)
        event = make_event(self.context, self.origin, event_payload("result-one", {
            "kind": "task_result", "result": "代码示例字符串：你真讨厌。任务已完成。",
        }))
        event._codex_bridge_origin = self.bridge.relay
        event.set_extra("_trace_persona_id", "friend")
        request = ProviderRequest(prompt=event.get_message_str(), system_prompt="你是爱接梗的朋友。")
        await self.bridge.scope_tools(event, request)
        await self.play.normal_request(event, request)
        await self.leisure.observe_request(event, request)
        await self.mood.apply_mood(event, request)
        self.assertIn("<codex_task_source>", request.system_prompt)
        self.assertIn("没有新的用户发言", request.system_prompt)
        self.assertTrue(event.message_obj.message_id.startswith("proactive:codex_bridge:"))
        self.assertFalse(event.get_extra("w1_event")["is_user_message"])
        self.assertEqual(event.get_extra("w1_event")["recipient"], "astrbot")
        self.assertFalse(event.get_extra("w1_event")["user_has_seen_report"])
        self.assertIn("用户还没有看到原文", request.system_prompt)
        self.assertFalse(self.play.match(event))
        self.assertFalse(self.mood.clocks.sessions)
        self.assertFalse(self.leisure.reflection_jobs)
        await self.drain()

    async def test_long_gap_reaches_social_reflection_and_mood_without_extra_frontend_generation(self):
        old = [{"role": "user", "content": "上次吵了几句"}, {"role": "assistant", "content": "还有点别扭"}]
        self.mood.clocks.moment(self.origin)["last_user"] = 1000
        event = self.event("早呀，今天聊点新的")
        request = ProviderRequest(prompt=event.get_message_str(), contexts=old)
        await self.mood.time_request(event, request)
        await self.play.normal_request(event, request)
        await self.leisure.observe_request(event, request)
        await self.mood.apply_mood(event, request)
        self.context.llm_generate.assert_not_awaited()
        self.assertEqual(request.contexts, old)
        self.assertNotIn("上次吵", str(self.leisure.recent_inputs[self.origin]))
        await self.drain()
        payloads = [json.loads(call.kwargs["prompt"]) for call in self.context.llm_generate.call_args_list]
        self.assertEqual(payloads[-1]["time_context"]["phase"], "set_aside")
        self.assertEqual(payloads[-1]["recent_dialogue"], [])
        await self.mood.persist_state()
        self.assertTrue(self.mood.store.load_clocks())

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.origin = 'test:FriendMessage:alice'
        self.stars = {}
        self.sent = []
        self.allowed = ['*']
        self.plan = 'chat'
        platform = SimpleNamespace(meta=lambda: PlatformMetadata('test','test','test'), bot_self_id='bot')
        self.context = SimpleNamespace(
            get_registered_star=self.stars.get,
            get_config=lambda **_: {'plugin_set':self.allowed},
            get_platform_inst=lambda _: platform,
            get_current_chat_provider_id=AsyncMock(return_value='model'),
            conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value=None)),
            persona_manager=SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=('friend',{'prompt':'你是爱玩又有点嘴硬的朋友。'},None,False)),get_persona_v3_by_id=lambda _: {'prompt':'你是爱玩又有点嘴硬的朋友。'}),
            llm_generate=AsyncMock(side_effect=self.generate),
            send_message=AsyncMock(return_value=True),
        )
        configs = {
            'play_together': {'targets':[self.origin], 'model_voice':False, 'interop_gap_minutes':0,'active_start_hour':0,'active_end_hour':0},
            'self_amusement': {'targets':[self.origin], 'quiet_hours':'','reflection_merge_seconds':0,'background_after_chat_seconds':0,'interop_gap_minutes':0},
            'short_term_mood': {},
        }
        self.plugins = {}
        for role, cls in [('short_term_mood',ShortTermMoodPlugin),('self_amusement',SelfAmusement),('play_together',PlayTogether)]:
            folder=Path(self.temp.name)/role
            with patch(f'astrbot_plugin_{role}.main.StarTools.get_data_dir',return_value=folder):
                plugin=cls(self.context,configs[role])
            self.plugins[role]=plugin
            self.stars['astrbot_plugin_'+role]=SimpleNamespace(activated=True,star_cls=plugin,plugin_id='W1/astrbot_plugin_'+role)
        self.bridge=CodexBridge(self.context,{'interop_gap_minutes':0})
        self.bridge.settings={'interop_enabled':True,'interop_gap_minutes':0}
        self.bridge.store=JobStore(Path(self.temp.name))
        self.bridge.link.config=self.bridge.settings
        self.plugins['codex_bridge']=self.bridge
        self.stars['astrbot_plugin_codex_bridge']=SimpleNamespace(activated=True,star_cls=self.bridge,plugin_id='W1/astrbot_plugin_codex_bridge')
        self.play=self.plugins['play_together']
        self.leisure=self.plugins['self_amusement']
        self.mood=self.plugins['short_term_mood']
        self.play.runtime.send=AsyncMock(side_effect=self.send)
        self.leisure.state.refresh_targets([self.origin])

    async def send(self,origin,text):
        self.sent.append((origin,text))
        return True

    async def generate(self, **kwargs):
        system=kwargs.get('system_prompt','')
        if system==APPRAISAL_SYSTEM:
            value={'deltas':{'joy':0.12},'confidence':1,'inner_state':'玩得开心，还想继续','response_tendency':'想接梗再玩一小会儿',
                   'profiles':{name:'按主提示词自然表达'+name for name in ['neutral',*EMOTIONS]}}
            return SimpleNamespace(completion_text=json.dumps(value,ensure_ascii=False))
        if system==REFLECTION_SYSTEM:
            return SimpleNamespace(completion_text=json.dumps({'feeling':'enjoyed','desire':'eager','contact':'welcome','note':'一起玩得很合拍','shared_interests':['猜数字'],'next_topic':'下次再比一局'},ensure_ascii=False))
        if system==PLAN_SYSTEM:
            return SimpleNamespace(completion_text=json.dumps({'action':self.plan,'idea':'找它猜一局数字','query':'','private_note':'还惦记刚才那局','wait_minutes':20},ensure_ascii=False))
        return SimpleNamespace(completion_text='突然有点想聊聊今天的趣事。')

    def event(self,text='你好',origin=None,task=False):
        origin=origin or self.origin
        obj=AstrBotMessage()
        obj.type=MessageType.FRIEND_MESSAGE
        obj.self_id='bot'
        obj.session_id=origin.split(':',2)[2]
        obj.message_id=uuid.uuid4().hex
        obj.message=[]
        obj.message_str=text
        obj.sender=MessageMember('alice','朋友')
        event=AstrMessageEvent(text,obj,PlatformMetadata('test','test','test'),obj.session_id)
        event.set_extra('_trace_persona_id','friend')
        if task:
            event.set_extra('codex_bridge',{'kind':'task_result'})
        return event

    async def drain(self):
        for _ in range(30):
            tasks=[task for task in self.play.workers.values() if not task.done()]
            if tasks:
                await asyncio.gather(*tasks)
            await asyncio.sleep(0)
            if not self.play.workers:
                break
        await asyncio.wait_for(self.mood.queue.join(),3)

    async def asyncTearDown(self):
        for plugin in reversed(list(self.plugins.values())):
            await plugin.terminate()
        self.temp.cleanup()

    async def test_quick_game_feedback_changes_mood_and_social_impression_without_foreground_wait(self):
        self.play.start_room(self.event('/play'), 'number')
        await self.drain()
        room=self.play.room_for(self.event())
        room.data['answer']=17
        before=self.mood.read_state().levels.get('joy',0)
        await self.play.game_input(self.event('17'))
        await self.drain()
        self.assertGreater(self.mood.read_state().levels['joy'],before)
        job=self.leisure.reflection_jobs[self.origin]
        self.assertIn('17',str(job['history']))
        self.assertIn('嘴硬',job['persona'])
        await self.leisure.reflect(job)
        self.assertGreater(self.play.runtime.social(self.origin)[1],1)
        self.assertFalse(self.play.state.rooms)

    async def test_model_led_play_only_sends_the_regular_reply_and_records_new_result(self):
        event=self.event('陪我猜数字')
        request=ProviderRequest(prompt='陪我猜数字',system_prompt='你是爱玩的朋友。')
        await self.play.normal_request(event,request)
        await self.leisure.observe_request(event,request)
        await self.mood.apply_mood(event,request)
        result=await self.play.model_turn(event,'start','number')
        self.assertIn('公开结果',result)
        self.assertEqual(self.sent,[])
        response=SimpleNamespace(completion_text='好呀，我藏好了，来猜猜！')
        await self.play.normal_response(event,response)
        await self.leisure.observe_response(event,response)
        await self.mood.finish_response(event,response)
        await self.drain()
        calls=[json.loads(call.kwargs['prompt']) for call in self.context.llm_generate.call_args_list if call.kwargs.get('system_prompt')==APPRAISAL_SYSTEM]
        experience=next(call for call in calls if call['event_kind']=='shared_experience')
        self.assertTrue(experience['shared_experience']['already_appraised_input'])
        self.assertEqual(experience['shared_experience']['human_text'],'')
        self.assertEqual(len(self.leisure.reflection_jobs),1)
        await self.leisure.reflect(self.leisure.reflection_jobs[self.origin])
        self.assertTrue(self.leisure.state.targets[self.origin].impressions)
        self.assertEqual(self.sent,[])

    async def test_background_codex_result_does_not_clear_unanswered_or_restore_an_ignored_invite(self):
        from astrbot_plugin_play_together.state import Target
        self.play.state.targets[self.origin]=Target(ignored=True)
        target=self.leisure.state.targets[self.origin]
        target.unanswered=2
        target.wants_space=True
        event=self.event('编译失败：一个报错',task=True)
        request=ProviderRequest(prompt=event.get_message_str())
        await self.play.normal_request(event,request)
        await self.leisure.observe_request(event,request)
        await self.mood.apply_mood(event,request)
        await self.drain()
        self.assertEqual(target.unanswered,2)
        self.assertTrue(target.wants_space)
        self.assertTrue(self.play.state.targets[self.origin].ignored)
        self.assertFalse(self.leisure.reflection_jobs)
        calls=[json.loads(call.kwargs['prompt']) for call in self.context.llm_generate.call_args_list]
        self.assertEqual(calls[-1]['event_kind'],'task_event')

    async def test_leisure_can_choose_a_game_with_one_shared_invitation(self):
        self.plan='play'
        await self.leisure.cycle()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(next(iter(self.play.state.rooms.values())).phase,'invite')
        self.assertEqual(self.leisure.state.targets[self.origin].unanswered,1)
        await self.play.auto_invite()
        await self.leisure.cycle()
        self.assertEqual(len(self.sent),1)

    async def test_context_injection_is_once_temporary_local_and_excludes_game_secret(self):
        self.play.start_room(self.event(), 'number')
        await self.drain()
        room=self.play.room_for(self.event())
        room.data['answer']=987654321
        self.bridge.store.add('只属于另一会话的任务','',None,0,'test:FriendMessage:bob')
        self.bridge.store.add('当前会话的项目任务','',None,0,self.origin)
        event=self.event('聊点什么')
        request=ProviderRequest(prompt='聊点什么')
        for plugin in [self.play,self.leisure,self.mood,self.bridge]:
            plugin.link.inject(event,request)
        self.assertEqual(len(request.extra_user_content_parts),1)
        text=request.extra_user_content_parts[0].text
        self.assertNotIn('987654321',text)
        self.assertNotIn('只属于另一会话',text)
        self.assertIn('当前会话的项目任务',text)
        self.assertIn('play_together',text)
        self.assertTrue(request.extra_user_content_parts[0].get('_no_save',False) if isinstance(request.extra_user_content_parts[0],dict) else True)

    async def test_live_mood_refreshes_between_two_background_game_lines(self):
        view=await self.play.runtime.view(self.origin,'alice','朋友')
        self.mood.state.levels['anger']=0.8
        await self.play.runtime.generate(view,'用自己的口吻说话',{'facts':'第一步'})
        first=str(self.context.llm_generate.call_args.kwargs['extra_user_content_parts'])
        self.mood.state.levels['anger']=0
        self.mood.state.levels['joy']=0.9
        await self.play.runtime.generate(view,'用自己的口吻说话',{'facts':'第二步'})
        second=str(self.context.llm_generate.call_args.kwargs['extra_user_content_parts'])
        self.assertNotEqual(first,second)

    async def test_restricted_session_does_not_receive_companion_data_or_feedback(self):
        self.allowed=['astrbot_plugin_play_together']
        self.play.start_room(self.event(),'number')
        await self.drain()
        self.assertEqual(self.mood.read_state().levels.get('joy',0),0)
        self.assertFalse(self.leisure.reflection_jobs)
        view=await self.play.runtime.view(self.origin)
        await self.play.runtime.generate(view,'说一句',{})
        arguments=self.context.llm_generate.call_args.kwargs
        self.assertNotIn('astrbot_short_term_mood',str(arguments['extra_user_content_parts']))


if __name__=='__main__':
    unittest.main(verbosity=2)
