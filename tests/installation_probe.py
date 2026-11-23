import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

root = Path(sys.argv[1]).resolve()
source = Path(sys.argv[2]).resolve()
destination = root / "data/plugins/astrbot_plugin_play_together"
destination.parent.mkdir(parents=True)
shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__", "data", ".venv"))
os.environ["ASTRBOT_ROOT"] = str(root)
sys.path.insert(0, str(root))

from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.core import LogBroker, db_helper
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.star_handler import star_handlers_registry


def incoming(text, identifier):
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.session_id = "person"
    message.message_id = identifier
    message.self_id = "bot"
    message.sender = MessageMember("person", "朋友")
    message.message = []
    message.message_str = text
    return AstrMessageEvent(text, message, PlatformMetadata("probe", "probe", "probe"), "person")


async def drain(instance):
    for _ in range(10):
        tasks = list(instance.workers.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        if not instance.workers and not instance.queues:
            return
    raise AssertionError("Game workers did not settle")


async def main():
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    with patch("astrbot.core.core_lifecycle.update_llm_metadata", AsyncMock()):
        await core.initialize()
    metadata = next(star for star in core.star_context.get_all_stars() if star.name == "astrbot_plugin_play_together")
    instance = metadata.star_cls
    assert instance is not None and metadata.activated
    assert metadata.config["targets"] == [] and metadata.config.schema["targets"]["_special"] == "umo"
    handlers = star_handlers_registry.get_handlers_by_module_name(metadata.module_path)
    handler = next(item for item in handlers if item.handler_name == "game_input")
    assert handler.extras_configs["priority"] == 1000
    instance.config["model_voice"] = False
    instance.runtime.send = AsyncMock(return_value=True)
    event = incoming("陪我玩", "start")
    assert all(item.filter(event, core.astrbot_config) for item in handler.event_filters)
    await handler.handler(event)
    await drain(instance)
    room = next(iter(instance.state.rooms.values()))
    assert room.phase == "playing"
    assert event.is_stopped()
    assert not all(item.filter(incoming("正常聊天不抢走", "ordinary"), core.astrbot_config) for item in handler.event_filters)
    previous = instance
    success, error = await core.plugin_manager.reload(metadata.name)
    assert success, error
    assert previous.scheduler.done() and previous.saver.done()
    await core.stop()
    print(json.dumps({"loaded": True, "native_configuration": True, "selective_input_routing": True, "background_game_opening": True, "hot_reload": True}))


asyncio.run(main())
