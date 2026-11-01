from dashboard_server import BotManager


def manager_without_io():
    return object.__new__(BotManager)


def test_dashboard_maps_place_and_chat_commands():
    manager = manager_without_io()
    assert manager._command_for("place", {"x": 3, "y": 4, "blockType": "STONE"}) == {
        "cmd": "place", "x": 3, "y": 4, "blockType": "STONE"
    }
    assert manager._command_for("msg", {"text": "halo"}) == {
        "cmd": "msg", "text": "halo"
    }
    assert manager._command_for("drop", {"itemType": "PHONE", "count": 1}) == {
        "cmd": "drop", "itemType": "PHONE", "count": 1
    }
    assert manager._command_for("collect", {}) == {"cmd": "collect"}
    assert manager._command_for("pickup", {"id": "drop-1"}) == {
        "cmd": "collect", "id": "drop-1"
    }


def test_dashboard_maps_break_and_navigation_commands():
    manager = manager_without_io()
    assert manager._command_for("break", {"x": 1, "y": 2}) == {
        "cmd": "mine", "x": 1, "y": 2, "delay": 0.16, "max": 30
    }
    assert manager._command_for("gotoplayer", {"name": "Serdo"}) == {
        "cmd": "goto_player", "name": "Serdo"
    }
    assert manager._command_for("findpath", {"x": 8, "y": 9}) == {
        "cmd": "goto_tile", "x": 8, "y": 9
    }
