from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Room:
    scope: str
    origin: str
    game: str
    phase: str
    updated: float
    expires: float
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    player_id: str = ""
    player_name: str = ""
    data: dict = field(default_factory=dict)
    revision: int = 0
    last_public: str = ""
    seen: list[str] = field(default_factory=list)


@dataclass
class Target:
    last_invited: float = 0
    ignored: bool = False
    last_game: str = ""
    last_player: str = ""
    last_result: str = ""
    recent: list[dict] = field(default_factory=list)


@dataclass
class PlayState:
    rooms: dict[str, Room] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)
    last_active: float = 0
    next_invite: float = 0
    revision: int = 0
    invite_day: str = ""
    invites_today: int = 0

    @classmethod
    def from_dict(cls, data):
        state = cls()
        for key in ("last_active", "next_invite", "revision", "invite_day", "invites_today"):
            if key in data:
                setattr(state, key, data[key])
        for scope, room in data.get("rooms", {}).items():
            state.rooms[scope] = Room(**room)
        for origin, target in data.get("targets", {}).items():
            state.targets[origin] = Target(**target)
        return state

    def touch(self, now, delay, origin="", human=True):
        self.last_active = now
        self.next_invite = now + delay
        self.revision += 1
        if origin and human:
            self.targets.setdefault(origin, Target()).ignored = False

    def expire(self, now):
        expired = []
        for scope, room in list(self.rooms.items()):
            if room.expires <= now:
                self.rooms.pop(scope)
                expired.append(scope)
                if room.phase == "invite":
                    self.targets.setdefault(room.origin, Target()).ignored = True
                else:
                    self.finish(room, "先收起来，之后想玩再继续开一局。", now)
        return expired

    def finish(self, room, result, now):
        target = self.targets.setdefault(room.origin, Target())
        target.last_game = room.game
        target.last_player = room.player_id
        target.last_result = result
        target.recent.append({"game": room.game, "result": result[:500], "time": now})
        target.recent = target.recent[-4:]

    def reserve_invite(self, origin, now):
        day = datetime.fromtimestamp(now).date().isoformat()
        if self.invite_day != day:
            self.invite_day, self.invites_today = day, 0
        self.invites_today += 1
        self.targets.setdefault(origin, Target()).last_invited = now


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / "play.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self):
        if not self.path.exists():
            return PlayState()
        return PlayState.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, data):
        descriptor, temporary = tempfile.mkstemp(prefix="play-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
