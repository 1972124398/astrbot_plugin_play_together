from __future__ import annotations

import asyncio
import copy
import json
import math
import random
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import ToolSet

from .games import GAMES, Turn, choose_game, game_key
from .interop import CompanionLink, conversation_key, event_kind
from .prompts import INVITE_SYSTEM, MODEL_PLAY_HINT, STORY_OPEN_SYSTEM, STORY_SYSTEM, VOICE_SYSTEM, parse_invite
from .routing import ACCEPT, AGAIN, CONCEDE, HINT, PAUSE, RESUME, SWITCH, PlayInputFilter, START, STOP, phrase
from .runtime import Runtime
from .state import PlayState, Room, Store, Target


class PlayTogether(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.link = CompanionLink(context, "play_together", config)
        self.runtime = Runtime(context, config)
        self.store = Store(StarTools.get_data_dir("astrbot_plugin_play_together"))
        try:
            self.state = self.store.load()
        except (ValueError, TypeError):
            self.state = PlayState()
            logger.warning("Play Together started with a fresh game state")
        self.queues = {}
        self.workers = {}
        self.voices = {}
        self.views = {}
        self.sending = set()
        self.normal_turns = {}
        self.serial = 0
        self.auto_task = None
        self.auto_sending = False
        self.scheduler = None
        self.saver = None
        self.save_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.save_wake = asyncio.Event()
        self.dirty = False
        self.closing = False
        self.activity = "等闲下来看看有没有人想玩"
        PlayInputFilter.bind(self)

    def setting(self, key, default, lower, upper):
        value = self.config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return default
        return min(upper, max(lower, value))

    def games(self):
        return [key for key in self.config.get("games", ["number", "rps", "story"]) if key in GAMES]

    def targets(self):
        return list(dict.fromkeys(origin.strip() for origin in self.config.get("targets", []) if isinstance(origin, str) and origin.strip()))

    def delay(self):
        first = self.setting("idle_min_minutes", 30, 1, 1440)
        last = max(first, self.setting("idle_max_minutes", 90, 1, 1440))
        return random.uniform(first, last) * 60

    async def initialize(self):
        now = time.time()
        self.state.expire(now)
        for scope, room in list(self.state.rooms.items()):
            if room.phase == "ending":
                self.state.finish(room, room.last_public or "上一局先收起来了，想玩可以再开。", now)
                self.state.rooms.pop(scope)
        self.state.last_active = self.state.last_active or now
        self.state.next_invite = max(self.state.next_invite, now + (60 if self.state.next_invite else self.delay()))
        await self.persist()
        self.scheduler = asyncio.create_task(self.run(), name="play-together-idle")
        self.saver = asyncio.create_task(self.run_saves(), name="play-together-save")

    def changed(self):
        self.dirty = True
        self.save_wake.set()

    async def persist(self):
        async with self.save_lock:
            data = asdict(self.state)
            self.dirty = False
            task = asyncio.create_task(asyncio.to_thread(self.store.save, data))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def run_saves(self):
        while True:
            await self.save_wake.wait()
            self.save_wake.clear()
            await asyncio.sleep(0.2)
            if self.dirty:
                try:
                    await self.persist()
                except Exception as error:
                    logger.warning("Play Together could not save its state (%s)", type(error).__name__)

    def touch(self, origin, human=True, broadcast=True):
        now = time.time()
        self.state.touch(now, self.delay(), origin, human)
        if human and broadcast:
            self.runtime.companion_touch(origin, now)
        self.changed()
        self.wake.set()

    def companion_snapshot(self, origin=""):
        rooms = [room for room in self.state.rooms.values() if room.expires > time.time()]
        if not origin:
            return {"holding_attention": any(room.phase in {"invite", "playing", "ending"} for room in rooms)}
        room = next((room for room in rooms if conversation_key(room.origin) == conversation_key(origin)), None)
        target = next((target for umo, target in self.state.targets.items() if conversation_key(umo) == conversation_key(origin)), None)
        return {"games": {key: GAMES[key].name for key in self.games()},
                "current_game": self.public_game(room) if room else None,
                "last_game": target.last_result if target else "",
                "can_invite": self.config.get("auto_invite", True) and origin in self.targets() and not rooms}

    def companion_observe(self, packet):
        if packet["kind"] == "human_activity":
            origin = packet["origin"]
            self.state.touch(time.time(), self.delay(), origin if origin in self.targets() else "")
            self.changed()
            self.wake.set()

    async def invite_from_companion(self, origin, idea, idle_token):
        if not self.link.enabled or not self.link.idle_current(idle_token):
            return False
        return await self._auto_invite(idle_token, origin, idea)

    def share_turn(self, room, job, text, outcome="", persona=""):
        self.link.publish("game_turn", room.origin, {
            "game": GAMES[room.game].name, "action": job["kind"], "outcome": outcome,
            "human_text": job.get("text", ""), "reply": text, "persona": persona,
            "handled_by_chat": job.get("result") is not None,
        }, event_id=f"play:{room.uid}:{room.revision}")

    def busy(self):
        now = time.monotonic()
        self.normal_turns = {token: started for token, started in self.normal_turns.items() if now - started < 600}
        return bool(self.normal_turns)

    def options(self):
        return {"number_upper": int(self.setting("number_upper", 30, 10, 100)),
                "number_attempts": int(self.setting("number_attempts", 6, 3, 12)),
                "rps_best_of": self.config.get("rps_best_of", 3) if self.config.get("rps_best_of", 3) in {1, 3, 5} else 3,
                "story_rounds": int(self.setting("story_rounds", 4, 2, 10))}

    def room_for(self, event):
        return self.state.rooms.get(self.runtime.event_scope(event))

    def target_for(self, event):
        origin = event.unified_msg_origin
        if origin in self.state.targets:
            return origin, self.state.targets[origin]
        if event.get_group_id():
            scope = self.runtime.event_scope(event)
            for other, target in self.state.targets.items():
                try:
                    if self.runtime.scope(other) == scope:
                        return other, target
                except ValueError:
                    pass
        return origin, Target()

    def match(self, event):
        if self.closing or not self.config.get("enabled", True) or event_kind(event) != "dialogue":
            return False
        text = event.get_message_str().strip()
        if not text or text.startswith(("/", "!")) or event.get_sender_id() == event.get_self_id():
            return False
        key = phrase(text)
        room = self.room_for(event)
        if room and room.expires > time.time():
            if room.phase == "invite":
                return key in ACCEPT | STOP | START or game_key(text) in self.games()
            if room.player_id != event.get_sender_id():
                return False
            return (key in STOP | HINT | PAUSE | RESUME | SWITCH | CONCEDE | AGAIN
                    or (room.phase in {"playing", "paused"} and GAMES[room.game].accepts(text)))
        if key in START and self.games():
            return True
        _, target = self.target_for(event)
        return bool(key in AGAIN and target.last_player == event.get_sender_id() and self.games())

    def enqueue(self, scope, kind, text="", sender="", label="", message_id="", result=None, story_line="", end_story=False):
        self.queues.setdefault(scope, deque()).append({"kind": kind, "text": text, "sender": sender, "label": label,
                                                     "id": message_id, "result": result, "story_line": story_line, "end_story": end_story})
        task = self.workers.get(scope)
        if task is None or task.done():
            self.workers[scope] = asyncio.create_task(self.run_room(scope), name="play-together-turn")
            self.workers[scope].add_done_callback(lambda done: self.worker_done(scope, done))

    def worker_done(self, scope, task):
        if not task.cancelled() and task.exception():
            logger.warning("Play Together turn worker stopped (%s)", type(task.exception()).__name__)
        if not self.closing and self.queues.get(scope) and self.workers.get(scope) is task:
            self.workers[scope] = asyncio.create_task(self.run_room(scope), name="play-together-turn")
            self.workers[scope].add_done_callback(lambda done: self.worker_done(scope, done))
        elif self.workers.get(scope) is task:
            self.workers.pop(scope, None)

    def clear_queue(self, scope):
        for job in self.queues.get(scope, []):
            result = job.get("result")
            if result is not None and not result.done():
                result.set_result("这一步已经先放下，按当前游戏状态继续聊就好。")
        self.queues[scope] = deque()

    def start_room(self, event, key, result=None, story_line=""):
        scope = self.runtime.event_scope(event)
        previous = self.state.rooms.get(scope)
        if previous:
            if previous.player_id:
                self.state.finish(previous, "这局先放下，换个心情再玩。", time.time())
            self.clear_queue(scope)
            self.views.pop(scope, None)
            task = self.workers.get(scope)
            if task and not task.done() and scope not in self.sending:
                task.cancel()
        room = Room(scope, event.unified_msg_origin, key, "playing", time.time(),
                    time.time() + self.setting("game_idle_minutes", 15, 2, 120) * 60,
                    player_id=event.get_sender_id(), player_name=event.get_sender_name(), data=GAMES[key].start(self.options(), random))
        self.state.rooms[scope] = room
        self.touch(room.origin, broadcast=result is None)
        self.enqueue(scope, "start", result=result, story_line=story_line)

    def stop_room(self, room):
        room.phase = "ending"
        room.last_public = "这局先收起来，想玩时再开一局。"
        room.revision += 1
        self.clear_queue(room.scope)
        task = self.workers.get(room.scope)
        if task and not task.done() and room.scope not in self.sending:
            task.cancel()
        self.enqueue(room.scope, "stop")

    def pause_room(self, room):
        if room.phase != "paused":
            room.paused_public = room.last_public
        room.phase = "paused"
        room.revision += 1
        room.expires = time.time() + self.setting("pause_minutes", 60, 5, 1440) * 60
        self.clear_queue(room.scope)
        task = self.workers.get(room.scope)
        if task and not task.done() and room.scope not in self.sending:
            task.cancel()
        self.enqueue(room.scope, "pause", sender=room.player_id)
        self.changed()

    @filter.custom_filter(PlayInputFilter, priority=1000)
    async def game_input(self, event: AstrMessageEvent):
        if not self.match(event):
            return
        text, key = event.get_message_str().strip(), phrase(event.get_message_str())
        room = self.room_for(event)
        if not room or room.expires <= time.time():
            previous = self.target_for(event)[1].last_game
            chosen = previous if key in AGAIN and previous in self.games() else choose_game(self.games(), previous)
            if chosen:
                self.start_room(event, chosen)
        elif key in STOP:
            if room.phase == "invite":
                self.state.targets.setdefault(room.origin, Target()).ignored = True
            self.stop_room(room)
        elif room.phase == "invite":
            room.game = game_key(text) or room.game
            room.player_id, room.player_name = event.get_sender_id(), event.get_sender_name()
            room.phase = "playing"
            room.data = GAMES[room.game].start(self.options(), random)
            room.expires = time.time() + self.setting("game_idle_minutes", 15, 2, 120) * 60
            self.touch(room.origin)
            self.enqueue(room.scope, "start")
        else:
            self.touch(room.origin)
            if key in PAUSE:
                self.pause_room(room)
            elif key in SWITCH | AGAIN:
                chosen = room.game if key in AGAIN else choose_game([game for game in self.games() if game != room.game])
                if chosen:
                    self.start_room(event, chosen)
                else:
                    self.enqueue(room.scope, "hint", sender=event.get_sender_id())
            else:
                kind = "hint" if key in HINT else "concede" if key in CONCEDE else "resume" if key in RESUME else "turn"
                self.enqueue(room.scope, kind, text, event.get_sender_id(), event.get_sender_name(), event.message_obj.message_id)
        event.stop_event()
        event.set_extra("play_together_handled", True)
        self.changed()

    async def voice(self, room, system, data, fallback=""):
        if self.busy() or (system == VOICE_SYSTEM and not self.config.get("model_voice", True)):
            return fallback
        try:
            async with asyncio.timeout(self.setting("voice_timeout_seconds", 8, 1, 60)):
                view = self.views.get(room.scope)
                if view is None:
                    view = await self.runtime.view(room.origin, room.player_id, room.player_name)
                    self.views[room.scope] = view
                task = asyncio.create_task(self.runtime.generate(view, system, data), name="play-together-voice")
                self.voices[room.scope] = task
                try:
                    return (await task).strip() or fallback
                finally:
                    self.voices.pop(room.scope, None)
        except asyncio.CancelledError:
            if self.closing or asyncio.current_task().cancelling():
                raise
            return fallback
        except Exception as error:
            logger.info("Play Together used its local game line (%s)", type(error).__name__)
            return fallback

    async def process(self, scope, job):
        source = self.state.rooms.get(scope)
        if source is None:
            return
        room = copy.deepcopy(source)
        if job["id"] and job["id"] in room.seen:
            return
        if job["kind"] == "start":
            if room.game == "story":
                opening = job.get("story_line") or await self.voice(room, STORY_OPEN_SYSTEM, {"previous_start": room.data["lines"][0]}, room.data["lines"][0])
                room.data["lines"][0] = opening
            turn = GAMES[room.game].opening(room.data)
        elif job["kind"] == "stop":
            turn = Turn("好，先收起来。之后想玩再叫我。", "stopped", True, "stopped")
        elif job["kind"] == "pause":
            room.phase = "paused"
            turn = Turn("先把这局放在这里，回来可以说“继续玩”，也可以直接接着出招。", "relaxed")
        else:
            if room.phase not in {"playing", "paused"} or job["sender"] != room.player_id:
                return
            if job["kind"] == "resume":
                room.phase = "playing"
                turn = Turn("回来啦，接着刚才这儿玩。\n" + (room.paused_public or GAMES[room.game].opening(room.data).facts))
            elif job["kind"] == "concede":
                if room.game == "number":
                    turn = Turn(f"好，那我揭晓：藏的是 {room.data['answer']}。这局就先让我得意一下。", "smug", True, "bot_won")
                elif room.game == "rps":
                    turn = Turn(f"这局你先认输啦，当前比分是你 {room.data['user_wins']} : 我 {room.data['bot_wins']}。", "smug", True, "bot_won")
                else:
                    turn = Turn("", "curious", True, "story_finished", True)
            elif job["kind"] == "hint":
                turn = GAMES[room.game].hint(room.data)
            else:
                room.phase = "playing"
                text = job["text"]
                if room.game == "story" and not GAMES["story"].accepts(text):
                    text = "接：" + text
                turn = GAMES[room.game].advance(room.data, text, random)
        if turn.story:
            if job.get("result") is not None and job["kind"] == "turn":
                turn.finished = bool(job.get("end_story"))
                turn.outcome = "story_finished" if turn.finished else ""
            fallback = "于是，他们决定把这个秘密留到明天再慢慢研究。" if turn.finished else "话音刚落，门外又传来三声敲门声，还有一句小小的“我也想参加”。"
            line = job.get("story_line") or await self.voice(room, STORY_SYSTEM, {"lines": room.data["lines"], "closing": turn.finished}, fallback)
            room.data["lines"].append(line)
            turn.facts = "我接：" + line + ("\n这段小故事先收在这里。想再编可说“再来一局”。" if turn.finished else "\n轮到你，用“接：”再续一句。")
        text = turn.facts
        if job.get("result") is None and not turn.story and job["kind"] != "stop":
            text = await self.voice(room, VOICE_SYSTEM, {
                "game": GAMES[room.game].name, "moment": "opening" if job["kind"] == "start" else turn.feeling,
                "facts": turn.facts, "player_message": job["text"], "previous_reply": source.last_public,
            }, turn.facts)
        if job["kind"] == "start" and room.game == "story":
            room.data["lines"][0] = text
        if self.state.rooms.get(scope) is not source or source.revision != room.revision:
            return
        room.revision += 1
        room.updated = time.time()
        minutes = self.setting("pause_minutes", 60, 5, 1440) if room.phase == "paused" else self.setting("game_idle_minutes", 15, 2, 120)
        room.expires = room.updated + minutes * 60
        room.last_public = text
        if job["id"]:
            room.seen = [*room.seen, job["id"]][-40:]
        if turn.finished:
            room.phase = "ending"
        self.state.rooms[scope] = room
        await self.persist()
        if self.state.rooms.get(scope) is not room:
            return
        self.sending.add(scope)
        try:
            if job.get("result") is None:
                async with asyncio.timeout(self.setting("send_timeout_seconds", 30, 3, 120)):
                    delivered = await self.runtime.send(room.origin, text)
                if delivered:
                    view = self.views.get(scope)
                    if view is None:
                        try:
                            view = await self.runtime.view(room.origin, room.player_id, room.player_name)
                            self.views[scope] = view
                        except Exception:
                            pass
                    self.share_turn(room, job, text, turn.outcome, getattr(view, "persona", ""))
        finally:
            self.sending.discard(scope)
            if turn.finished and self.state.rooms.get(scope) is room:
                self.state.finish(room, turn.facts, time.time())
                self.state.rooms.pop(scope)
                self.views.pop(scope, None)
                self.touch(room.origin, human=False)
                await self.persist()
        return text

    async def run_room(self, scope):
        while self.queues.get(scope):
            job = self.queues[scope].popleft()
            result = job.get("result")
            try:
                text = await self.process(scope, job)
                if result is not None and not result.done():
                    result.set_result(text or "这局状态已经有了变化，按当前状态继续接话。")
            except asyncio.CancelledError:
                if result is not None and not result.done():
                    result.set_result("这一步先放下了，可以按当前游戏状态继续聊。")
                if self.closing:
                    raise
            except Exception as error:
                logger.warning("Play Together could not finish this game turn (%s)", type(error).__name__)
                if result is not None and not result.done():
                    result.set_result("这一步没接好，可以看看 /play_status，再继续玩。")
        self.queues.pop(scope, None)

    async def auto_invite(self):
        token = self.link.claim_idle()
        if token is None:
            return
        try:
            return await self._auto_invite(token)
        finally:
            self.link.release_idle(token)

    async def _auto_invite(self, idle_token, preferred_origin="", idea=""):
        now = time.time()
        if (self.closing or not self.config.get("enabled", True) or not self.config.get("auto_invite", True)
                or (not preferred_origin and now < self.state.next_invite) or self.state.rooms or self.busy()):
            return
        current = datetime.fromtimestamp(now)
        first, last = int(self.setting("active_start_hour", 9, 0, 23)), int(self.setting("active_end_hour", 23, 0, 24))
        if first != last and not (first <= current.hour < last if first < last else current.hour >= first or current.hour < last):
            return
        limit = int(self.setting("daily_invites", 3, 0, 50))
        if self.state.invite_day == current.date().isoformat() and limit and self.state.invites_today >= limit:
            return
        candidates, weights = [], []
        for origin in self.targets():
            if preferred_origin and origin != preferred_origin:
                continue
            target = self.state.targets.get(origin, Target())
            if target.ignored or now - target.last_invited < self.setting("target_gap_minutes", 240, 10, 10080) * 60:
                continue
            try:
                _, platform = self.runtime.platform(origin)
                allowed = self.context.get_config(umo=origin).get("plugin_set", ["*"])
                if not ("*" in allowed or "astrbot_plugin_play_together" in allowed or getattr(self, "plugin_id", "") in allowed):
                    continue
                if not platform.meta().support_proactive_message or not await self.runtime.enabled(origin):
                    continue
                _, weight, leave = self.runtime.social(origin)
                if leave:
                    continue
                candidates.append(origin)
                weights.append(weight)
            except (ValueError, KeyError):
                continue
        if not candidates or not self.games():
            return
        origin = random.choices(candidates, weights=weights)[0]
        revision = self.state.revision
        self.state.next_invite = now + self.delay()
        self.changed()
        self.activity = "在想找谁玩一点什么"
        key = choose_game(self.games(), self.state.targets.get(origin, Target()).last_game)
        line = f"有点想找你玩个{GAMES[key].name}，来一小局？"
        if self.config.get("model_voice", True):
            try:
                async with asyncio.timeout(self.setting("voice_timeout_seconds", 8, 1, 60)):
                    view = await self.runtime.view(origin)
                    text = await self.runtime.generate(view, INVITE_SYSTEM, {
                        "games": {key: GAMES[key].name for key in self.games()}, "mood": self.link.snapshot("short_term_mood", origin),
                        "company": self.runtime.social(origin)[0],
                        "recent_games": self.state.targets.get(origin, Target()).recent,
                        "preferences": str(self.config.get("play_preferences", "") or ""),
                        "idea": idea,
                    })
                key, line = parse_invite(text, self.games())
                if key == "wait":
                    self.activity = "这会儿先自己待着"
                    return
            except Exception as error:
                logger.info("Play Together used a local invitation (%s)", type(error).__name__)
        if (self.closing or revision != self.state.revision or self.state.rooms or origin not in self.targets()
                or self.busy() or not self.link.idle_current(idle_token)):
            return
        scope = self.runtime.scope(origin)
        room = Room(scope, origin, key, "invite", time.time(), time.time() + self.setting("invitation_minutes", 5, 1, 30) * 60)
        room.last_public = (line or f"想玩个{GAMES[key].name}，来一局？") + "\n想玩就回“来一局”，或者照常聊也行。"
        self.state.rooms[scope] = room
        self.state.reserve_invite(origin, time.time())
        await self.persist()
        if revision != self.state.revision or not self.link.idle_current(idle_token):
            if self.state.rooms.get(scope) is room and room.phase == "invite":
                self.state.rooms.pop(scope)
            self.changed()
            return
        self.auto_sending = True
        try:
            async with asyncio.timeout(self.setting("send_timeout_seconds", 30, 3, 120)):
                delivered = await self.runtime.send(origin, room.last_public)
        finally:
            self.auto_sending = False
        self.activity = "递了个邀约，等对方想不想玩"
        if delivered:
            self.link.sent(origin, room.last_public)
        return bool(delivered)

    async def run(self):
        while True:
            self.wake.clear()
            expired = self.state.expire(time.time())
            for scope in expired:
                self.views.pop(scope, None)
                self.queues.pop(scope, None)
                task = self.workers.get(scope)
                if task and not task.done() and scope not in self.sending:
                    task.cancel()
            if expired:
                self.changed()
            if self.auto_task and self.auto_task.done() and not self.auto_task.cancelled():
                error = self.auto_task.exception()
                if error:
                    logger.info("Play Together will try inviting another time (%s)", type(error).__name__)
            if self.auto_task is None or self.auto_task.done():
                self.auto_task = asyncio.create_task(self.auto_invite(), name="play-together-invite")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=20)
            except TimeoutError:
                pass

    def public_game(self, room):
        data = {"game": GAMES[room.game].name, "phase": room.phase,
                "player": room.player_name, "last_reply": room.last_public}
        if room.phase == "invite":
            return data
        if room.game == "number":
            data.update(range=[room.data["low"], room.data["high"]], chances_left=room.data["left"])
        elif room.game == "rps":
            data.update(user_wins=room.data["user_wins"], your_wins=room.data["bot_wins"], best_of=room.data["best_of"])
        else:
            data.update(story=room.data["lines"][-10:], rounds=room.data["rounds"], total_rounds=room.data["total"])
        return data

    def model_tools(self, event, request):
        get_manager = getattr(self.context, "get_llm_tool_manager", None)
        if not get_manager:
            return
        if request.func_tool is not None:
            request.func_tool.remove_tool("play_together_turn")
        if not self.config.get("natural_play", True) or not self.config.get("enabled", True):
            return
        room = self.room_for(event)
        owns = room and (not room.player_id or room.player_id == event.get_sender_id())
        related = owns or (not room and any(word in event.get_message_str() for word in ("玩", "猜数字", "猜拳", "接龙")))
        if not related:
            return
        tool = get_manager().get_func("play_together_turn")
        if tool is None or not getattr(tool, "active", True):
            return
        if request.func_tool is None:
            request.func_tool = ToolSet()
        request.func_tool.add_tool(tool)
        state = self.public_game(room) if room else {"games": {key: GAMES[key].name for key in self.games()}, "phase": "not_started"}
        state["play_preferences"] = str(self.config.get("play_preferences", "") or "")
        request.extra_user_content_parts.append(TextPart(text=MODEL_PLAY_HINT + "\n" + json.dumps(state, ensure_ascii=False)).mark_as_temp())

    @filter.llm_tool(name="play_together_turn")
    async def model_turn(self, event: AstrMessageEvent, action: str, value: str = "", story_line: str = "", end_story: bool = False) -> str:
        """陪当前玩家自然玩游戏，更新实际进度并返回公开结果，供你自己组织回复。

        Args:
            action(string): start 开局，move 猜数字/出拳/接故事，hint 提示，switch 换游戏，concede 认输，pause 暂停，resume 继续，stop 收起。
            value(string): 玩家操作，猜数字用阿拉伯数字，出拳用石头/剪刀/布，故事用玩家这一句。开局或换游戏可填游戏名称。
            story_line(string): 玩故事时你接着编的那一小段，可以结合当前人格自由发挥，收尾时让情节自然落下。
            end_story(bool): 这次自然故事接龙是否收尾。轮数只是参考，双方兴致未尽可以继续，收尾时设为 true。
        """
        if self.closing or not self.config.get("enabled", True):
            return "陪它玩现在歇着，可以正常聊天。"
        room = self.room_for(event)
        if room and room.player_id and room.player_id != event.get_sender_id():
            return "当前这局由另一位玩家在玩，先让他们收尾。"
        future = asyncio.get_running_loop().create_future()
        if action in {"start", "switch"}:
            if room and room.phase in {"playing", "paused"} and action == "start":
                return "当前已经开局，可以 move 接着玩、resume 继续，或者 switch 换一种。"
            key = game_key(value) if value else (room.game if room and room.phase == "invite" else choose_game([key for key in self.games() if not room or key != room.game]))
            if key not in self.games():
                return "现在可以玩：" + "、".join(GAMES[key].name for key in self.games())
            self.start_room(event, key, result=future, story_line=story_line.strip())
            scope = self.runtime.event_scope(event)
            played_room = self.state.rooms[scope]
        else:
            if not room or (room.phase == "invite" and action != "stop"):
                return "还没有接住这一局，可以先用 start 开始。"
            kinds = {"move": "turn", "hint": "hint", "concede": "concede", "pause": "pause", "resume": "resume", "stop": "stop"}
            if action not in kinds:
                return "可以按当前游戏自然选择出招、提示、换游戏、暂停、继续或收起。"
            if action == "pause":
                room.paused_public = room.paused_public or room.last_public
            if action == "stop" and room.phase == "invite":
                self.state.targets.setdefault(room.origin, Target()).ignored = True
            self.touch(room.origin, broadcast=False)
            scope = room.scope
            played_room = room
            self.enqueue(scope, kinds[action], value or event.get_message_str(), event.get_sender_id(), event.get_sender_name(),
                         result=future, story_line=story_line.strip(), end_story=end_story)
        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=40)
            current = self.state.rooms.get(scope)
            event.set_extra("play_together_model_reply", {
                "scope": scope, "uid": played_room.uid, "origin": played_room.origin,
                "revision": current.revision if current and current.uid == played_room.uid else None,
                "story_line": played_room.game == "story" and action in {"move", "start", "switch"},
                "game": GAMES[played_room.game].name, "action": action, "facts": result,
            })
            return "游戏进度已更新，公开结果如下。请用自己的口吻直接接话，插件不会另发重复回复。\n" + result
        except TimeoutError:
            return "这一步还在后台接，先看看 /play_status，再决定怎么继续。"

    @filter.on_llm_request(priority=-120)
    async def normal_request(self, event: AstrMessageEvent, request):
        if event.get_extra("play_together") or self.closing or not self.config.get("enabled", True):
            return
        self.link.begin(event)
        if event.get_extra("play_together_persona") is None:
            event.set_extra("play_together_persona", request.system_prompt or "")
        self.link.inject(event, request)
        if event_kind(event) == "dialogue":
            self.model_tools(event, request)
        elif request.func_tool:
            request.func_tool.remove_tool("play_together_turn")
        token = event.get_extra("play_together_turn")
        if token is None:
            self.serial += 1
            token = self.serial
            event.set_extra("play_together_turn", token)
        self.normal_turns[token] = time.monotonic()
        if self.auto_task and not self.auto_task.done() and not self.auto_sending:
            self.auto_task.cancel()
        for task in self.voices.values():
            task.cancel()
        origin = self.target_for(event)[0]
        self.state.touch(time.time(), self.delay(), origin if origin in self.state.targets or origin in self.targets() else "",
                         human=event_kind(event) == "dialogue")
        target = self.state.targets.get(origin)
        if target and target.last_result:
            request.extra_user_content_parts.append(TextPart(text="你和当前会话最近玩过的小游戏，供自然接话：\n" + target.last_result).mark_as_temp())
        self.changed()

    @filter.on_llm_response(priority=-120)
    async def normal_response(self, event: AstrMessageEvent, response):
        self.link.end(event, response)
        if getattr(response, "tools_call_args", None):
            return
        reply = event.get_extra("play_together_model_reply")
        text = getattr(response, "completion_text", "") or ""
        if reply and text:
            self.link.publish("game_turn", reply["origin"], {
                "game": reply["game"], "action": reply["action"], "outcome": reply["facts"],
                "human_text": "", "reply": text, "handled_by_chat": True,
                "persona": event.get_extra("play_together_persona") or "", "already_appraised_input": True,
            }, event_id=f"play-model:{reply['uid']}:{reply['revision']}:{reply['action']}")
            room = self.state.rooms.get(reply["scope"])
            if room and room.uid == reply["uid"] and room.revision == reply["revision"]:
                room.last_public = text
                if reply["story_line"] and room.data.get("lines"):
                    room.data["lines"][-1] = text.removeprefix("接：").removeprefix("我接：").strip()
                self.changed()
            target = self.state.targets.get(reply["origin"])
            if target and target.last_room_id == reply["uid"]:
                target.last_result = text
                if target.recent:
                    target.recent[-1]["result"] = text[:500]
                self.changed()
        self.normal_turns.pop(event.get_extra("play_together_turn"), None)
        self.wake.set()

    @filter.on_agent_done(priority=-120)
    async def agent_done(self, event: AstrMessageEvent, run_context, response):
        self.link.end(event)
        self.normal_turns.pop(event.get_extra("play_together_turn"), None)
        self.wake.set()

    @filter.command("play", priority=1000)
    async def play(self, event: AstrMessageEvent, game: str = ""):
        if not self.config.get("enabled", True):
            yield event.plain_result("陪它玩现在歇着呢，启用后再来玩。")
            return
        room = self.room_for(event)
        invited = room.game if room and room.phase == "invite" else ""
        if invited:
            self.state.rooms.pop(room.scope, None)
            room = None
        if room and room.expires > time.time():
            selected = game_key(game) if game else None
            if selected in self.games() and (room.player_id == event.get_sender_id() or not room.player_id):
                self.start_room(event, selected)
                yield event.plain_result("好，换个心情开这局。")
                event.stop_event()
                return
            yield event.plain_result("我们这局还在。可以说“换个游戏”“先等一下”，或者接着出招。")
            return
        key = game_key(game) if game else (invited if invited in self.games() else choose_game(self.games(), self.target_for(event)[1].last_game))
        if key not in self.games():
            yield event.plain_result("可以玩：" + "、".join(GAMES[key].name for key in self.games()) + "。例如 /play 猜数字。" if self.games() else "先在配置页勾选想玩的小游戏，再来找我。")
            return
        self.start_room(event, key)
        yield event.plain_result("好呀，等我开个头。")
        event.stop_event()

    @filter.command("play_stop", priority=1000)
    async def stop(self, event: AstrMessageEvent):
        room = self.room_for(event)
        if not room:
            yield event.plain_result("这会儿没有正在玩的局，想玩就说“陪我玩”。")
        elif room.player_id and room.player_id != event.get_sender_id() and event.role != "admin":
            yield event.plain_result("这局先让正在玩的朋友决定，等收尾后再开新局。")
        else:
            self.stop_room(room)
            event.stop_event()

    @filter.command("play_hint", priority=1000)
    async def hint(self, event: AstrMessageEvent):
        room = self.room_for(event)
        if room and room.phase == "playing" and room.player_id == event.get_sender_id():
            self.touch(room.origin)
            self.enqueue(room.scope, "hint", sender=event.get_sender_id())
            event.stop_event()
        else:
            yield event.plain_result("等你进了一局，我再给你递点小线索。")

    @filter.command("play_status", priority=1000)
    async def status(self, event: AstrMessageEvent):
        room = self.room_for(event)
        if room and room.expires > time.time():
            yield event.plain_result(f"正在玩{GAMES[room.game].name}。\n{room.last_public or '开场正在后台想，稍等一下。'}")
        else:
            target = self.target_for(event)[1]
            yield event.plain_result(target.last_result or "还没开局呢。说“陪我玩”或 /play 就能来一小局。")

    @filter.command("play_pause", priority=1000)
    async def pause(self, event: AstrMessageEvent):
        room = self.room_for(event)
        if room and room.player_id == event.get_sender_id() and room.phase in {"playing", "paused"}:
            self.pause_room(room)
            event.stop_event()
        else:
            yield event.plain_result("这会儿没有你正在玩的局。想玩就叫我。")

    @filter.command("play_resume", priority=1000)
    async def resume(self, event: AstrMessageEvent):
        room = self.room_for(event)
        if room and room.player_id == event.get_sender_id() and room.phase == "paused":
            self.touch(room.origin)
            self.enqueue(room.scope, "resume", sender=event.get_sender_id())
            event.stop_event()
        else:
            yield event.plain_result("我们可以直接开一小局，或者接着正在玩的出招。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("play_here", priority=1000)
    async def add_here(self, event: AstrMessageEvent):
        self.config["targets"] = list(dict.fromkeys([*self.targets(), event.unified_msg_origin]))
        self.config.save_config()
        self.touch(event.unified_msg_origin)
        yield event.plain_result("已加入可以主动邀玩的会话，群聊和私聊共用一套设置。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("play_leave", priority=1000)
    async def leave_here(self, event: AstrMessageEvent):
        self.config["targets"] = [origin for origin in self.targets() if origin != event.unified_msg_origin]
        self.config.save_config()
        self.touch(event.unified_msg_origin)
        yield event.plain_result("已取消这里的主动邀玩，之后仍能自己开一局。")

    async def terminate(self):
        if self.closing:
            return
        self.closing = True
        self.link.close()
        tasks = [task for task in [self.scheduler, self.saver, self.auto_task, *self.workers.values(), *self.voices.values()] if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.persist()
