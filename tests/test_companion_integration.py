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


class TogetherTests(unittest.IsolatedAsyncioTestCase):
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
