"""Small chat games. Each game owns its rules and public turn facts."""

from dataclasses import dataclass
import random
import re


@dataclass
class Turn:
    facts: str
    feeling: str = "playful"
    finished: bool = False
    outcome: str = ""
    story: bool = False


class NumberGame:
    name = "猜数字"
    aliases = {"number", "数字", "猜数字"}

    def start(self, config, rng):
        upper = int(config.get("number_upper", 30))
        attempts = int(config.get("number_attempts", 6))
        return {"answer": rng.randint(1, upper), "upper": upper, "low": 1, "high": upper,
                "left": attempts, "attempts": attempts, "hinted": False}

    def opening(self, data):
        return Turn(f"我藏好了一个 1～{data['upper']} 的整数。你有 {data['attempts']} 次机会，直接发数字来猜。")

    def guess(self, text):
        match = re.fullmatch(r"(?:我猜|猜|是不是|是)?\s*(\d{1,3})\s*(?:吗|吧|呢)?[?？!！。]*", text.strip())
        return int(match[1]) if match else None

    def accepts(self, text):
        return self.guess(text) is not None

    def advance(self, data, text, rng):
        guess = self.guess(text)
        if guess is None:
            return Turn("直接猜一个整数就好，也可以说“不玩了”。")
        if not data["low"] <= guess <= data["high"]:
            return Turn(f"现在已经缩到 {data['low']}～{data['high']} 了，这次先不算，重新猜一个。")
        data["left"] -= 1
        if guess == data["answer"]:
            return Turn(f"被你猜中了，就是 {guess}！用了 {data['attempts'] - data['left']} 次。想再玩可说“再来一局”。", "impressed", True, "user_won")
        if data["left"] <= 0:
            return Turn(f"这次机会用完啦，我藏的是 {data['answer']}。想再玩可说“再来一局”。", "smug", True, "bot_won")
        if guess < data["answer"]:
            data["low"] = guess + 1
            direction = "大"
        else:
            data["high"] = guess - 1
            direction = "小"
        feeling = "nervous" if data["high"] - data["low"] <= 3 else "playful"
        return Turn(f"比 {guess} {direction}。还剩 {data['left']} 次，可以在 {data['low']}～{data['high']} 里猜。", feeling)

    def hint(self, data):
        if data["hinted"]:
            return Turn(f"再透露就快藏不住了。现在范围 {data['low']}～{data['high']}，试一个？")
        data["hinted"] = True
        parity = "偶数" if data["answer"] % 2 == 0 else "奇数"
        return Turn(f"给你一点小线索：它是{parity}。这条提示不占次数。")


MOVES = ("石头", "剪刀", "布")


class RpsGame:
    name = "石头剪刀布"
    aliases = {"rps", "猜拳", "石头剪刀布", "剪刀石头布"}

    def start(self, config, rng):
        best = int(config.get("rps_best_of", 3))
        return {"bot_move": rng.choice(MOVES), "user_wins": 0, "bot_wins": 0, "best_of": best}

    def opening(self, data):
        return Turn(f"{data['best_of']} 局先赢 {data['best_of'] // 2 + 1} 局。我已经出好藏着了，发“石头”“剪刀”或“布”吧，平局重来。")

    def move(self, text):
        text = re.sub(r"[\s!！。?？\ufe0f]", "", text).removeprefix("我出").removeprefix("出")
        return {"✊": "石头", "拳头": "石头", "✌": "剪刀", "✋": "布", "手掌": "布"}.get(text, text) if text else ""

    def accepts(self, text):
        return self.move(text) in MOVES

    def advance(self, data, text, rng):
        user, bot = self.move(text), data["bot_move"]
        if user not in MOVES:
            return Turn("发“石头”“剪刀”或“布”就能出拳。")
        if user == bot:
            result, feeling = "这轮平局，重来", "playful"
        elif (MOVES.index(user) - MOVES.index(bot)) % 3 == 2:
            data["user_wins"] += 1
            result, feeling = "这轮你赢", "sulky"
        else:
            data["bot_wins"] += 1
            result, feeling = "这轮我赢", "smug"
        facts = f"你出{user}，我出{bot}。{result}！你 {data['user_wins']} : 我 {data['bot_wins']}。"
        needed = data["best_of"] // 2 + 1
        if max(data["user_wins"], data["bot_wins"]) >= needed:
            outcome = "user_won" if data["user_wins"] >= needed else "bot_won"
            facts += "这局结束，想再玩可说“再来一局”。"
            return Turn(facts, feeling, True, outcome)
        data["bot_move"] = rng.choice(MOVES)
        return Turn(facts + "下一轮也藏好了，继续出拳？", feeling)

    def hint(self, data):
        return Turn("这回谁也不偷看。发“石头”“剪刀”或“布”吧。")


STARTERS = (
    "楼下那只猫今天带来一张纸条，上面写着：人类，你的快递在我手里。",
    "我买了一盆薄荷，第二天它开口问我：今天也要装得很精神吗？",
    "电梯里多了一个按钮，写着“去一个不用早起的星期一”。",
    "小镇上所有雨伞都失踪了，只有鸭子们看上去特别心虚。",
    "便利店老板递给我一颗糖，说吃完就能听见冰箱的心事。",
    "那天的月亮忘记下班，天亮后还挂着一块“再待五分钟”的牌子。",
)


class StoryGame:
    name = "一句话故事接龙"
    aliases = {"story", "故事", "接龙", "故事接龙", "一句话故事接龙"}

    def start(self, config, rng):
        return {"lines": [rng.choice(STARTERS)], "rounds": 0, "total": int(config.get("story_rounds", 4))}

    def opening(self, data):
        return Turn(f"""一起编个小故事，每人一句，接 {data['total']} 轮。
我先来：{data['lines'][0]}
你用“接：”续一句，我再接。随时可以说“不玩了”。""")

    def accepts(self, text):
        return bool(re.match(r"^(?:接|续)[：:]", text.strip()))

    def advance(self, data, text, rng):
        line = re.sub(r"^(?:接|续)[：:]\s*", "", text.strip())[:200]
        if not line:
            return Turn("用“接：”加上你想续的那句话就好。")
        data["lines"].append(line)
        data["rounds"] += 1
        return Turn("", "curious", data["rounds"] >= data["total"], "story_finished", True)

    def hint(self, data):
        return Turn("可以让刚才的人物遇到一件更奇怪的小事。用“接：”续一句，脑洞大一点也行。")


GAMES = {"number": NumberGame(), "rps": RpsGame(), "story": StoryGame()}


def game_key(value):
    for key, game in GAMES.items():
        if value.strip().lower() in game.aliases:
            return key
    return None


def choose_game(enabled, previous="", rng=random):
    choices = [key for key in enabled if key in GAMES]
    choices = [key for key in choices if key != previous] or choices
    return rng.choice(choices) if choices else None
