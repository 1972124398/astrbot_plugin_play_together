import random
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_play_together.games import GAMES, MOVES, choose_game, game_key
from astrbot_plugin_play_together.state import PlayState, Room, Store


class GameTests(unittest.TestCase):
    def test_number_guesses_and_hint_are_consistent(self):
        game = GAMES["number"]
        data = game.start({}, random.Random(1))
        data["answer"] = 17
        first = game.advance(data, "我猜12", random)
        self.assertIn("比 12 大", first.facts)
        self.assertEqual(data["low"], 13)
        self.assertEqual(data["left"], 5)
        self.assertIn("奇数", game.hint(data).facts)
        self.assertEqual(data["left"], 5)
        game.advance(data, "9", random)
        self.assertEqual(data["left"], 5)
        end = game.advance(data, "17", random)
        self.assertTrue(end.finished)
        self.assertEqual(end.outcome, "user_won")

    def test_number_losing_reveals_the_actual_answer(self):
        data = GAMES["number"].start({"number_attempts": 3}, random.Random(1))
        data["answer"] = 30
        for guess in (1, 2, 3):
            end = GAMES["number"].advance(data, str(guess), random)
        self.assertTrue(end.finished)
        self.assertIn("30", end.facts)

    def test_rps_all_nine_combinations_and_picked_next_round(self):
        game = GAMES["rps"]
        beats = {"石头": "剪刀", "剪刀": "布", "布": "石头"}
        for user in MOVES:
            for bot in MOVES:
                data = {"bot_move": bot, "user_wins": 0, "bot_wins": 0, "best_of": 1}
                turn = game.advance(data, "我出" + user, random.Random(2))
                if user == bot:
                    self.assertFalse(turn.finished)
                else:
                    expected = "user_won" if beats[user] == bot else "bot_won"
                    self.assertEqual(turn.outcome, expected)
                self.assertIn(bot, turn.facts)
                self.assertIn(data["bot_move"], MOVES)

    def test_story_leaves_room_for_the_next_sentence_and_finishes(self):
        game = GAMES["story"]
        data = game.start({"story_rounds": 2}, random.Random(2))
        self.assertFalse(game.accepts("今天的天气怎么样"))
        self.assertTrue(game.accepts("接：猫把钥匙吞了"))
        first = game.advance(data, "接：猫把钥匙吞了", random)
        self.assertTrue(first.story)
        self.assertFalse(first.finished)
        last = game.advance(data, "续：钥匙忽然唱起歌", random)
        self.assertTrue(last.finished)
        self.assertEqual(data["lines"][-1], "钥匙忽然唱起歌")

    def test_registered_games_and_aliases_allow_rotation(self):
        self.assertEqual(game_key("猜拳"), "rps")
        self.assertEqual(game_key("故事接龙"), "story")
        self.assertNotEqual(choose_game(["number", "rps"], "number"), "number")

    def test_ignored_invitation_is_silently_retired_until_return(self):
        state = PlayState()
        room = Room("scope", "origin", "number", "invite", 100, 200)
        state.rooms["scope"] = room
        self.assertEqual(state.expire(201), ["scope"])
        self.assertTrue(state.targets["origin"].ignored)
        state.touch(300, 100, "origin")
        self.assertFalse(state.targets["origin"].ignored)

    def test_game_state_can_resume_without_exposing_secret_as_status(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = PlayState()
            room = Room("scope", "origin", "number", "playing", 100, 200, data={"answer": 17}, last_public="比12大")
            state.rooms["scope"] = room
            store.save(asdict(state))
            restored = store.load().rooms["scope"]
            self.assertEqual(restored.data["answer"], 17)
            self.assertNotIn("17", restored.last_public)
