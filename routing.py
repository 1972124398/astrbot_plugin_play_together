import re
import weakref

from astrbot.api.event import filter


ACCEPT = {"好", "好的", "好呀", "好啊", "好哇", "可以", "可以呀", "行", "行啊", "行呀", "来", "来吧", "来呀", "来一局", "开一局", "好玩", "玩", "我来", "一起玩", "开始", "开玩"}
STOP = {"不玩了", "先不玩", "今天不玩", "算了不玩了", "下次吧", "改天吧", "下次再玩", "先到这儿", "结束游戏", "退出游戏"}
START = {"陪我玩", "陪你玩", "一起玩游戏", "来玩个小游戏", "玩个小游戏"}
AGAIN = {"再来一局", "再玩一局", "再来一次"}
HINT = {"提示", "给个提示", "提示一下"}
PAUSE = {"先等一下", "等我一下", "暂停", "暂停一下", "先暂停", "先歇会儿"}
RESUME = {"继续", "继续玩", "接着玩", "接着来", "我回来了", "回来啦"}
SWITCH = {"换个游戏", "换一个游戏", "换个玩", "玩别的", "换一种"}
CONCEDE = {"认输", "我认输", "我认输啦", "我认输了", "投降", "我投降", "揭晓答案", "告诉我答案吧"}


def phrase(text):
    return re.sub(r"[\s，,。.!！?？~～]", "", text.strip())


class PlayInputFilter(filter.CustomFilter):
    @classmethod
    def bind(cls, plugin):
        cls.owner = weakref.ref(plugin)

    def filter(self, event, cfg):
        owner = getattr(self, "owner", lambda: None)()
        return bool(owner and not owner.closing and owner.match(event))
