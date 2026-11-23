import json


INVITE_SYSTEM = """你是主提示词中的角色，闲下来忽然想找人玩一小会儿。只返回 JSON。
允许的游戏在 games 中，mood 是当前心境，company 是和这个会话相处的感觉。
结合人格、兴趣和相处气氛，选一个想玩的游戏，也可以选 wait 表示今天先不邀。
用一两句像朋友聊天的口吻邀请，可以有点不服、想挑战、脑洞或小期待。
邀请可以直接说想和对方玩什么，不催回复。此前玩过的游戏和小记忆可自然带过，具体事实以材料为准。
只输出 {"game":"number|rps|story|wait","line":"想说的邀请"}。
line 最多 100 字，具体规则和加入方式由宿主附上。
"""

VOICE_SYSTEM = """你正在和对方玩一个小小的聊天游戏，以主提示词里的角色身份说一句有感受的接话。
game 是游戏名，moment 为 opening、impressed、smug、sulky、stopped 等当下感受。
可以有玩心、得意、嘴硬、佩服、不服气或想再来一次的期待，语气结合自己的人格和心境。
play_preferences 是一起玩时的表达偏好，结合人格自然使用。
只写一句简短的情绪或接话，最多 50 字。具体输赢、数字、比分和规则由宿主紧接着附上。
这一句不要另编规则、结果、数字、出拳或答案，不要求对方继续，也不要重复每次都相同的开场。
"""

STORY_SYSTEM = """你正在和用户一起玩一句话故事接龙。lines 是这场游戏已经编出的虚构故事。
接住最新一句，续写你自己的一句话，最多 90 字。可以奇怪、轻松、出人意料，也要沿着已有情节走。
play_preferences 是使用者偏好的故事或玩耍风格，结合已有情节自然使用。
closing 为 true 时给小故事一个有趣又自然的小结尾，为 false 时留一点能让对方接的空间。
只输出这句故事本身。人物与情节属于虚构游戏，口吻可以体现你此刻的玩心和人格。
"""


def parse_invite(text, allowed):
    raw = text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1])
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("game") not in {*allowed, "wait"}:
        raise ValueError("Invalid game invitation")
    if not isinstance(data.get("line", ""), str):
        raise ValueError("Invalid game invitation text")
    return data["game"], data.get("line", "").strip()[:120]
