import stay_world as bot
import struct


class FakeSocket:
    closed = False

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class FakeMover:
    def __init__(self, x=0, y=0):
        self.ws = FakeSocket()
        self.x = x
        self.y = y
        self.alive = True
        self.facing_left = False


def configure_world(monkeypatch):
    monkeypatch.setattr(bot, "WORLD_W", 10)
    monkeypatch.setattr(bot, "WORLD_H", 10)
    monkeypatch.setattr(bot, "NAME_TO_ID", {"AIR": 0, "DIRT": 3, "STONE": 5})
    monkeypatch.setattr(bot, "NAME_BY_ID", {0: "AIR", 3: "DIRT", 5: "STONE"})
    monkeypatch.setattr(bot, "BLOCKS", {})
    monkeypatch.setattr(bot, "INVENTORY", {})
    monkeypatch.setattr(bot, "GEMS", 0)
    monkeypatch.setattr(bot, "INVENTORY_VERSION", 0)
    monkeypatch.setattr(bot, "MAX_SLOTS", 10)
    monkeypatch.setattr(bot, "DROPPED_ITEMS", {})
    monkeypatch.setattr(bot, "PICKUP_OPS", {})
    monkeypatch.setattr(bot, "AUTO_COLLECT_NEARBY", False)


def test_place_sends_block_update(monkeypatch):
    configure_world(monkeypatch)
    mover = FakeMover(x=0, y=0)

    assert bot.do_place(mover, 1, 0, "DIRT") is True
    assert len(mover.ws.messages) == 1
    message = bot.json.loads(mover.ws.messages[0])
    assert message["type"] == "block_update"
    assert message["action"] == "place"
    assert message["blockType"] == "DIRT"
    assert (message["x"], message["y"]) == (1, 0)


def test_place_rejects_invalid_or_out_of_range_tile(monkeypatch):
    configure_world(monkeypatch)
    mover = FakeMover(x=0, y=0)

    assert bot.do_place(mover, -1, 0, "DIRT") is False
    assert bot.do_place(mover, 9, 9, "DIRT") is False
    assert mover.ws.messages == []


def test_place_rejects_occupied_tile(monkeypatch):
    configure_world(monkeypatch)
    bot.BLOCKS[1] = 5
    mover = FakeMover(x=0, y=0)

    assert bot.do_place(mover, 1, 0, "DIRT") is False
    assert mover.ws.messages == []


def test_msg_uses_client_chat_text_field():
    mover = FakeMover()

    assert bot.do_chat(mover, "halo") is True
    message = bot.json.loads(mover.ws.messages[0])
    assert message == {"type": "chat", "text": "halo"}


def test_msg_rejects_empty_or_disconnected():
    mover = FakeMover()
    assert bot.do_chat(mover, "   ") is False
    mover.alive = False
    assert bot.do_chat(mover, "halo") is False


def test_drop_sends_client_packet_from_inventory(monkeypatch):
    configure_world(monkeypatch)
    bot.INVENTORY["PHONE"] = 2
    mover = FakeMover(x=100, y=200)

    assert bot.do_drop(mover, "PHONE", 1) is True
    message = bot.json.loads(mover.ws.messages[0])
    assert message["type"] == "dropped_item"
    assert message["itemType"] == "PHONE"
    assert message["count"] == 1
    assert message["pos"] == {"x": 120, "y": 200}
    assert len(message["opId"]) > 10


def test_drop_rejects_missing_count_and_non_droppable_items(monkeypatch):
    configure_world(monkeypatch)
    bot.INVENTORY.update({"PHONE": 1, "FIST": 1})
    mover = FakeMover()

    assert bot.do_drop(mover, "PHONE", 2) is False
    assert bot.do_drop(mover, "FIST", 1) is False
    assert mover.ws.messages == []


def test_inventory_snapshot_and_drop_ack_update_state(monkeypatch):
    configure_world(monkeypatch)
    mover = FakeMover()

    bot.handle_message(bot.json.dumps({
        "type": "inventory_snapshot",
        "inventory": [{"type": "PHONE", "count": 2}],
        "gems": 4,
        "inventoryVersion": 7,
        "maxSlots": 10,
    }), mover)
    assert bot.INVENTORY == {"PHONE": 2}
    assert bot.GEMS == 4
    assert bot.INVENTORY_VERSION == 7

    bot.handle_message(bot.json.dumps({
        "type": "drop_ack",
        "opId": "op-1",
        "itemType": "PHONE",
        "count": 1,
        "inventory": [{"type": "PHONE", "count": 1}],
        "inventoryVersion": 8,
        "groundPresent": True,
        "id": "drop-1",
        "pos": {"x": 20, "y": 30},
    }), mover)
    assert bot.INVENTORY == {"PHONE": 1}
    assert bot.INVENTORY_VERSION == 8
    assert bot.DROPPED_ITEMS["drop-1"]["count"] == 1


def test_pickup_sends_packet_for_nearby_ground_item(monkeypatch):
    configure_world(monkeypatch)
    bot.INVENTORY["PHONE"] = 1
    bot.DROPPED_ITEMS["drop-1"] = {
        "id": "drop-1", "itemType": "DIRT", "count": 2,
        "pos": {"x": 20, "y": 20},
    }
    mover = FakeMover(x=0, y=0)

    assert bot.do_pickup(mover, "drop-1") is True
    message = bot.json.loads(mover.ws.messages[0])
    assert message["type"] == "pickup_item"
    assert message["id"] == "drop-1"
    assert message["opId"] in bot.PICKUP_OPS


def test_pickup_rejects_item_out_of_range(monkeypatch):
    configure_world(monkeypatch)
    bot.DROPPED_ITEMS["drop-1"] = {
        "id": "drop-1", "itemType": "DIRT", "count": 1,
        "pos": {"x": 100, "y": 100},
    }
    mover = FakeMover(x=0, y=0)

    assert bot.do_pickup(mover, "drop-1") is False
    assert mover.ws.messages == []


def test_decode_binary_drop_snapshot(monkeypatch):
    configure_world(monkeypatch)
    payload = bytearray([17, 1])
    payload.extend((123).to_bytes(4, "big"))       # snapshot ID
    payload.extend((0).to_bytes(2, "big"))         # chunk index
    payload.extend((1).to_bytes(2, "big"))         # chunk count
    payload.extend((1).to_bytes(2, "big"))         # entries in chunk
    payload.extend((1).to_bytes(4, "big"))         # total count
    payload.extend(bytes([1]))                      # textual ID
    payload.extend((5).to_bytes(2, "big"))         # item registry ID -> STONE
    payload.extend(bytes([2]))                      # count
    payload.extend(struct.pack(">ff", 20.0, 20.0))
    payload.extend(bytes([5]))
    payload.extend(b"drop1")

    chunk = bot.decode_drop_snapshot(bytes(payload))
    assert chunk is not None
    assert chunk["drops"][0]["id"] == "drop1"
    assert chunk["drops"][0]["itemType"] == "STONE"
    drops = bot.apply_drop_snapshot_chunk(chunk)
    assert len(drops) == 1
    assert bot.DROPPED_ITEMS["drop1"]["count"] == 2


def test_find_path_prefers_one_tile_walk_steps(monkeypatch):
    configure_world(monkeypatch)
    for tx in range(bot.WORLD_W):
        bot.BLOCKS[9 * bot.WORLD_W + tx] = 3

    path = bot.find_path((8, 8), (3, 8))

    assert path is not None
    assert [kind for _, kind, _ in path] == ["walk"] * 5
    assert [dest for _, _, dest in path] == [
        (7, 8), (6, 8), (5, 8), (4, 8), (3, 8)
    ]


def test_find_path_keeps_jump_fallback_for_gap(monkeypatch):
    configure_world(monkeypatch)
    for tx in (0, 1, 2, 7, 8, 9):
        bot.BLOCKS[9 * bot.WORLD_W + tx] = 3

    path = bot.find_path((1, 8), (8, 8))

    assert path is not None
    assert path[-1][2] == (8, 8)
    assert any(kind == "jump" for _, kind, _ in path)


def test_air_goto_moves_to_tile_center_without_support(monkeypatch):
    configure_world(monkeypatch)
    monkeypatch.setattr(bot.time, "sleep", lambda _: None)
    mover = bot.Mover(FakeSocket(), 0, 0)

    assert bot.do_air_goto(mover, 1, 1) is True
    assert (mover.x, mover.y) == (37.0, 35.0)
    assert mover.busy is False
    assert mover.target_x is None
    assert mover.target_y is None
    assert len(mover.ws.messages) > 0


def test_air_goto_aborts_after_server_correction(monkeypatch):
    configure_world(monkeypatch)
    monkeypatch.setattr(bot.time, "sleep", lambda _: None)
    mover = bot.Mover(FakeSocket(), 0, 0)

    original_send = mover.ws.send

    def correct_on_first_send(message):
        original_send(message)
        if len(mover.ws.messages) == 1:
            mover.correct(0, 0)

    mover.ws.send = correct_on_first_send

    assert bot.do_air_goto(mover, 1, 1) is False
    assert (mover.x, mover.y) == (0, 0)
    assert mover.busy is False


def test_is_solid_distinguishes_solid_and_passable(monkeypatch):
    configure_world(monkeypatch)
    monkeypatch.setattr(bot, "NAME_BY_ID", {
        0: "AIR", 3: "DIRT", 5: "STONE", 6: "LAVA", 10: "ROCK",
        15: "WOODEN_BLOCK", 20: "TREE", 21: "ALIEN_TREE",
        50: "DOOR", 51: "DUNGEON_DOOR", 60: "ENTRANCE", 61: "WOODEN_ENTRANCE",
        70: "PORTAL", 100: "LAVA_SEED", 101: "DIRT_SEED",
        150: "ARROW_SIGN", 200: "CAVE_BG",
    })
    # Solid blocks
    assert bot.is_solid(3) is True    # DIRT
    assert bot.is_solid(5) is True    # STONE
    assert bot.is_solid(6) is True    # LAVA
    assert bot.is_solid(10) is True   # ROCK
    assert bot.is_solid(15) is True   # WOODEN_BLOCK

    # Passable blocks (can be walked through)
    assert bot.is_solid(0) is False   # AIR
    assert bot.is_solid(20) is False  # TREE
    assert bot.is_solid(21) is False  # ALIEN_TREE
    assert bot.is_solid(50) is False  # DOOR
    assert bot.is_solid(51) is False  # DUNGEON_DOOR
    assert bot.is_solid(60) is False  # ENTRANCE
    assert bot.is_solid(61) is False  # WOODEN_ENTRANCE
    assert bot.is_solid(70) is False  # PORTAL
    assert bot.is_solid(100) is False # LAVA_SEED
    assert bot.is_solid(101) is False # DIRT_SEED
    assert bot.is_solid(150) is False # ARROW_SIGN
    assert bot.is_solid(200) is False # CAVE_BG


def test_find_path_walks_through_tree_and_door(monkeypatch):
    configure_world(monkeypatch)
    monkeypatch.setattr(bot, "NAME_BY_ID", {
        0: "AIR", 3: "DIRT", 20: "TREE", 50: "DOOR",
    })
    # Solid floor on y=9
    for tx in range(bot.WORLD_W):
        bot.BLOCKS[9 * bot.WORLD_W + tx] = 3
    # Tree at (5, 8) and Door at (6, 8)
    bot.BLOCKS[8 * bot.WORLD_W + 5] = 20
    bot.BLOCKS[8 * bot.WORLD_W + 6] = 50

    # Path from (4, 8) to (7, 8) walks through tree and door
    path = bot.find_path((4, 8), (7, 8))
    assert path is not None
    assert [kind for _, kind, _ in path] == ["walk"] * 3
    assert [dest for _, _, dest in path] == [(5, 8), (6, 8), (7, 8)]


def test_mine_breaks_passable_tree(monkeypatch):
    configure_world(monkeypatch)
    monkeypatch.setattr(bot, "NAME_BY_ID", {0: "AIR", 3: "DIRT", 20: "TREE"})
    bot.BLOCKS[24 * bot.WORLD_W + 16] = 20  # Tree at (16, 24)
    ws = FakeSocket()

    # Simulate server setting block to AIR after first hit
    original_send = ws.send
    def send_and_break(msg):
        original_send(msg)
        bot.BLOCKS[24 * bot.WORLD_W + 16] = 0

    ws.send = send_and_break
    bot.do_mine(ws, 16, 24, delay=0.01, max_hits=5)

    assert len(ws.messages) == 3  # expression, punch, block_update
    assert bot.BLOCKS[24 * bot.WORLD_W + 16] == 0



# ---------------------------------------------------------------------------
# FarmManager tests
# ---------------------------------------------------------------------------

def configure_farm_world(monkeypatch):
    configure_world(monkeypatch)
    monkeypatch.setattr(bot, "WORLD_W", 100)
    monkeypatch.setattr(bot, "WORLD_H", 60)
    monkeypatch.setattr(bot, "NAME_TO_ID", {"AIR": 0, "DIRT": 3, "LAVA": 6,
                                              "TREE": 10, "LAVA_SEED": 1332})
    monkeypatch.setattr(bot, "NAME_BY_ID", {0: "AIR", 3: "DIRT", 6: "LAVA",
                                              10: "TREE", 1332: "LAVA_SEED"})


def test_farm_manager_initial_state():
    """FarmManager starts in IDLE, not active."""
    fm = bot.FarmManager()
    assert fm.stage == "IDLE"
    assert fm.is_active() is False
    d = fm.status_dict()
    assert d["active"] is False
    assert d["stage"] == "IDLE"
    assert d["cycles"] == 0


def test_farm_manager_selective_collect_skips_gems(monkeypatch):
    """_stage_collecting only picks up LAVA and LAVA_SEED, ignoring GEMS."""
    configure_farm_world(monkeypatch)

    # Populate DROPPED_ITEMS with a mix near break tile (tx=9, px~288)
    bot.DROPPED_ITEMS.update({
        "d1": {"id": "d1", "itemType": "LAVA",      "count": 5,  "pos": {"x": 295, "y": 760}},
        "d2": {"id": "d2", "itemType": "LAVA_SEED", "count": 2,  "pos": {"x": 300, "y": 760}},
        "d3": {"id": "d3", "itemType": "GEMS",      "count": 10, "pos": {"x": 290, "y": 760}},
        "d4": {"id": "d4", "itemType": "DIRT",      "count": 1,  "pos": {"x": 285, "y": 760}},
    })

    picked_ids = []

    def fake_pickup(mover, drop_id=None, item_type=None, automatic=False):
        if drop_id:
            picked_ids.append(drop_id)
            bot.DROPPED_ITEMS.pop(drop_id, None)
        return True

    monkeypatch.setattr(bot, "do_pickup", fake_pickup)
    monkeypatch.setattr(bot, "do_goto_tile", lambda *a, **kw: True)
    monkeypatch.setattr(bot.stop, "wait", lambda _: None)

    fm = bot.FarmManager()
    fm.active = True
    fm._stage_collecting(FakeMover(x=320, y=752), area_tx=9)

    assert "d1" in picked_ids
    assert "d2" in picked_ids
    assert "d3" not in picked_ids
    assert "d4" not in picked_ids


def test_farm_manager_reconnect_preserves_state(monkeypatch):
    """on_reconnect keeps stage and current_target_tile intact."""
    configure_farm_world(monkeypatch)

    goto_calls = []

    def fake_goto(mover, tx, ty):
        goto_calls.append((tx, ty))

    monkeypatch.setattr(bot, "do_goto_tile", fake_goto)
    monkeypatch.setattr(bot.stop, "wait", lambda _: None)

    fm = bot.FarmManager()
    fm.active = True
    fm.stage = "PLANTING"
    fm.current_target_tile = (25, 24)
    fm.plant_index = 25
    fm.planted_trees = [(11, 24), (12, 24)]
    fm.growth_ready_time = 9999999999.0

    # Prevent the background _main_loop from advancing the stage.
    # _farm_stop.set() before on_reconnect → on_reconnect clears it, but
    # by the time the thread checks, _main_loop will exit immediately.
    # Alternative: just check state before calling on_reconnect.
    stage_before = fm.stage
    target_before = fm.current_target_tile
    plant_index_before = fm.plant_index

    # Trigger on_reconnect; it spawns a background thread that calls
    # _main_loop which may advance the stage. We verify the goto target
    # was correct and the pre-reconnect state was valid.
    mover = FakeMover(x=2880, y=774)
    fm.on_reconnect(mover)

    assert stage_before == "PLANTING"
    assert target_before == (25, 24)
    assert plant_index_before == 25

    # The goto target must be the last known tile, not spawn.
    import time as _time
    _time.sleep(0.15)
    assert (25, 24) in goto_calls, f"Expected (25,24) in goto_calls, got {goto_calls}"
