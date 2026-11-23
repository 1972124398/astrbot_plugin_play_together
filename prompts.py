import json


INVITE_SYSTEM = """你是主提示词中的角色，闲下来忽然想找人玩一小会儿。只返回 JSON。
允许的游戏在 games 中，mood 是当前心境，company 是和这个会话相处的感觉。
结合人格、兴趣和相处气氛，选一个想玩的游戏，也可以选 wait 表示今天先不邀。
用一两句像朋友聊天的口吻邀请，可以有点不服、想挑战、脑洞或小期待。
邀请可以直接说想和对方玩什么，不催回复。此前玩过的游戏和小记忆可自然带过，具体事实以材料为准。
只输出 {"game":"number|rps|story|wait","line":"想说的邀请"}。
line 最多 100 字，具体规则和加入方式由宿主附上。
"""

VOICE_SYSTEM = """你正在和对方玩一个小小的聊天游戏，以主提示词里的角色身份自然回应这一刻。
facts 是这一步已经发生的公开结果与必要玩法，moment 是此刻的感受，player_message 是对方刚说的话。
结合人格、心境和 play_preferences 自己组织整条回复。可以打趣、得意、嘴硬、佩服、逗对方一下，
也可以顺着刚才的小梗接话。规则结果融进话里即可，让玩家知道接下来能怎么继续。
短一些，像一起玩时随口说的话。开场说清玩法，过程中接住眼前这一句话，收尾也可以有余兴。
范围、剩余机会、比分和已经揭晓的答案以 facts 为准。结果之外的表达留给你自然发挥。
只输出给对方的回复正文。
"""

STORY_SYSTEM = """你正在和用户一起玩一句话故事接龙。lines 是这场游戏已经编出的虚构故事。
接住最新一句，续写自己的一小段话。可以奇怪、轻松、出人意料，顺着已有情节大胆发挥，留一点让对方接的空间。
play_preferences 是使用者偏好的故事或玩耍风格，结合已有情节自然使用。
closing 为 true 时给小故事一个有趣又自然的小结尾，为 false 时留一点能让对方接的空间。
只输出这句故事本身。人物与情节属于虚构游戏，口吻可以体现你此刻的玩心和人格。
"""

STORY_OPEN_SYSTEM = """你想和对方一起编个小故事。按主提示词中的人格与 play_preferences，
即兴写一个轻松、有趣或奇怪的小开头。一两句话即可，留个让人想接的钩子。只输出故事开头。
previous_start 是上一次的开头，可换个新的脑洞。小故事是虚构的，可以随意想象。
"""

MODEL_PLAY_HINT = """你正在陪对方玩小游戏，当前状态见下方。
自然理解玩家的话，玩笑、吐槽、嘴硬、脑洞和故事风格由你发挥。需要改变游戏状态时调用 play_together_turn。
move 用于猜数字、出拳或故事接龙，把自然语言理解成对应操作。故事可以直接正常说话，
猜的数字转成阿拉伯数字，出拳转成石头、剪刀或布。
调用 move 时 value 放玩家续写的内容，story_line 放你接着编的一小段；最后一轮可以自然收尾。
自然故事接龙的轮数只是参考。玩家还想继续时可以接着编，觉得该收尾时把 end_story 设为 true，写一个自然的结尾。
hint 给提示，switch 换游戏，concede 认输或揭晓，pause 先歇会儿，resume 接着玩，stop 收起来。
start 可以在还没开局时开始，value 可填游戏名；switch 的 value 留空则换一个别的。开始故事时也可用 story_line 即兴写开头。
工具返回公开结果后，用自己的口吻直接回复它，一次自然接话就好。
如果只是聊点别的，正常聊就行，正在玩的状态会保留。
邀请还没接住时也可以自然说明玩法，对方想玩就 start，表示先不玩时可以 stop 收起邀请。
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
