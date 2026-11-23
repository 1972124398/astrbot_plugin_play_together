from __future__ import annotations

import asyncio
import copy
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

from .games import GAMES, Turn, choose_game, game_key
from .prompts import INVITE_SYSTEM, STORY_SYSTEM, VOICE_SYSTEM, parse_invite
from .routing import ACCEPT, AGAIN, HINT, PlayInputFilter, START, STOP, phrase
from .runtime import Runtime
from .state import PlayState, Room, Store, Target


class PlayTogether(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
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

    def touch(self, origin, human=True):
        now = time.time()
        self.state.touch(now, self.delay(), origin, human)
        hold = max((room.expires for room in self.state.rooms.values() if room.phase == "playing"), default=0)
        self.runtime.companion_touch(origin, now, hold)
        self.changed()
        self.wake.set()

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
        if self.closing or not self.config.get("enabled", True):
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
            return key in STOP | HINT or (room.phase == "playing" and GAMES[room.game].accepts(text))
        if key in START and self.games():
            return True
        _, target = self.target_for(event)
        return bool(key in AGAIN and target.last_player == event.get_sender_id() and self.games())

    def enqueue(self, scope, kind, text="", sender="", label="", message_id=""):
        self.queues.setdefault(scope, deque()).append({"kind": kind, "text": text, "sender": sender, "label": label, "id": message_id})
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

    def start_room(self, event, key):
        scope = self.runtime.event_scope(event)
        room = Room(scope, event.unified_msg_origin, key, "playing", time.time(),
                    time.time() + self.setting("game_idle_minutes", 15, 2, 120) * 60,
                    player_id=event.get_sender_id(), player_name=event.get_sender_name(), data=GAMES[key].start(self.options(), random))
        self.state.rooms[scope] = room
        self.touch(room.origin)
        self.enqueue(scope, "start")

    def stop_room(self, room):
        room.phase = "ending"
        room.last_public = "这局先收起来，想玩时再开一局。"
        room.revision += 1
        self.queues[room.scope] = deque()
        task = self.workers.get(room.scope)
        if task and not task.done() and room.scope not in self.sending:
            task.cancel()
        self.enqueue(room.scope, "stop")

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
            self.enqueue(room.scope, "hint" if key in HINT else "turn", text, event.get_sender_id(), event.get_sender_name(), event.message_obj.message_id)
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
                    return (await task).splitlines()[0][:160]
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
            turn = GAMES[room.game].opening(room.data)
        elif job["kind"] == "stop":
            turn = Turn("好，先收起来。之后想玩再叫我。", "stopped", True, "stopped")
        else:
            if room.phase != "playing" or job["sender"] != room.player_id:
                return
            turn = GAMES[room.game].hint(room.data) if job["kind"] == "hint" else GAMES[room.game].advance(room.data, job["text"], random)
        if turn.story:
            fallback = "于是，他们决定把这个秘密留到明天再慢慢研究。" if turn.finished else "话音刚落，门外又传来三声敲门声，还有一句小小的“我也想参加”。"
            line = await self.voice(room, STORY_SYSTEM, {"lines": room.data["lines"], "closing": turn.finished}, fallback)
            room.data["lines"].append(line)
            turn.facts = "我接：" + line + ("\n这段小故事先收在这里。想再编可说“再来一局”。" if turn.finished else "\n轮到你，用“接：”再续一句。")
        prefix = ""
        if job["kind"] == "start" or (turn.finished and turn.outcome != "stopped" and not turn.story):
            prefix = await self.voice(room, VOICE_SYSTEM, {"game": GAMES[room.game].name, "moment": "opening" if job["kind"] == "start" else turn.feeling})
        elif job["kind"] == "turn" and room.game == "rps":
            prefix = random.choice({"smug": ["嘿，这回让我得意一下。", "这轮接住了！"], "sulky": ["啧，被你拿下了。", "这轮算你厉害。"], "playful": ["还真想到一块去了。", "居然一样，再来。"]}[turn.feeling])
        elif turn.feeling == "nervous":
            prefix = random.choice(["被你逼近了。", "这下有点藏不住了。"])
        text = (prefix + "\n" if prefix else "") + turn.facts
        if self.state.rooms.get(scope) is not source or source.revision != room.revision:
            return
        room.revision += 1
        room.updated = time.time()
        room.expires = room.updated + self.setting("game_idle_minutes", 15, 2, 120) * 60
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
            async with asyncio.timeout(self.setting("send_timeout_seconds", 30, 3, 120)):
                await self.runtime.send(room.origin, text)
        finally:
            self.sending.discard(scope)
            if turn.finished and self.state.rooms.get(scope) is room:
                self.state.finish(room, turn.facts, time.time())
                self.state.rooms.pop(scope)
                self.views.pop(scope, None)
                self.touch(room.origin, human=bool(room.player_id))
                await self.persist()

    async def run_room(self, scope):
        while self.queues.get(scope):
            job = self.queues[scope].popleft()
            try:
                await self.process(scope, job)
            except asyncio.CancelledError:
                if self.closing:
                    raise
            except Exception as error:
                logger.warning("Play Together could not finish this game turn (%s)", type(error).__name__)
        self.queues.pop(scope, None)

    async def auto_invite(self):
        now = time.time()
        if (self.closing or not self.config.get("enabled", True) or not self.config.get("auto_invite", True)
                or now < self.state.next_invite or self.state.rooms or self.busy() or self.runtime.companion_busy()):
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
                        "games": {key: GAMES[key].name for key in self.games()}, "mood": self.runtime.mood(),
                        "company": self.runtime.social(origin)[0],
                        "recent_games": self.state.targets.get(origin, Target()).recent,
                        "preferences": str(self.config.get("play_preferences", "") or ""),
                    })
                key, line = parse_invite(text, self.games())
                if key == "wait":
                    self.activity = "这会儿先自己待着"
                    return
            except Exception as error:
                logger.info("Play Together used a local invitation (%s)", type(error).__name__)
        if self.closing or revision != self.state.revision or self.state.rooms or origin not in self.targets() or self.busy():
            return
        scope = self.runtime.scope(origin)
        room = Room(scope, origin, key, "invite", time.time(), time.time() + self.setting("invitation_minutes", 5, 1, 30) * 60)
        room.last_public = (line or f"想玩个{GAMES[key].name}，来一局？") + "\n想玩就回“来一局”，或者照常聊也行。"
        self.state.rooms[scope] = room
        self.state.reserve_invite(origin, time.time())
        await self.persist()
        if revision != self.state.revision:
            if self.state.rooms.get(scope) is room and room.phase == "invite":
                self.state.rooms.pop(scope)
            self.changed()
            return
        self.auto_sending = True
        try:
            async with asyncio.timeout(self.setting("send_timeout_seconds", 30, 3, 120)):
                await self.runtime.send(origin, room.last_public)
        finally:
            self.auto_sending = False
        self.activity = "递了个邀约，等对方想不想玩"

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

    @filter.on_llm_request(priority=-120)
    async def normal_request(self, event: AstrMessageEvent, request):
        if event.get_extra("play_together") or self.closing or not self.config.get("enabled", True):
            return
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
        room = self.room_for(event)
        if room and room.phase == "invite":
            if any(word in event.get_message_str() for word in ("怎么玩", "什么游戏", "啥游戏", "哪个游戏", "玩法", "规则", "猜数字", "猜拳", "故事", "接龙")):
                request.extra_user_content_parts.append(TextPart(text=f"你刚刚邀请当前会话玩{GAMES[room.game].name}，还在等对方接住。可以自然解释玩法，确认时让对方说“来一局”或用 /play 指定游戏，实际开局由陪它玩插件处理。").mark_as_temp())
            else:
                self.state.rooms.pop(room.scope, None)
        self.state.touch(time.time(), self.delay(), origin if origin in self.state.targets or origin in self.targets() else "")
        target = self.state.targets.get(origin)
        if target and target.last_result:
            request.extra_user_content_parts.append(TextPart(text="你和当前会话最近玩过的小游戏，供自然接话：\n" + target.last_result).mark_as_temp())
        self.changed()

    @filter.on_llm_response(priority=-120)
    async def normal_response(self, event: AstrMessageEvent, response):
        self.normal_turns.pop(event.get_extra("play_together_turn"), None)
        self.wake.set()

    @filter.on_agent_done(priority=-120)
    async def agent_done(self, event: AstrMessageEvent, run_context, response):
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
            yield event.plain_result("这边还有一局。用 /play_status 看看，或用 /play_stop 先收起来。")
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
        tasks = [task for task in [self.scheduler, self.saver, self.auto_task, *self.workers.values(), *self.voices.values()] if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.persist()
