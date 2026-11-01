#!/usr/bin/env python3
import json, logging, math, os, struct, threading, time, urllib.request, uuid
import websocket
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNT = os.environ.get("NEO_ACCOUNT", f"{BASE_DIR}/guest_account.json")
CMD_FILE = os.environ.get("NEO_CMD_FILE", f"{BASE_DIR}/stay_cmd.txt")
STATE_FILE = os.environ.get("NEO_STATE_FILE", f"{BASE_DIR}/bot_state.json")
LOG_FILE = os.environ.get("NEO_LOG_FILE", f"{BASE_DIR}/stay_world.log")
BOT_ID = os.environ.get("NEO_BOT_ID", "default")
WORLD = os.environ.get("NEO_WORLD", "778839")
BASE = "wss://game.playneotopia.com"
Q = "?clientPlatform=web&clientRevision=37"
UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
HEARTBEAT_SEC = 5
TICK_HZ = 41  # client sends at ~41Hz while moving (24ms throttle)
MAX_SPEED_TICK = 64.0  # 64px/pkt accepted by server; 128 trips anticheat
MAX_INTERACTION_TILES = 3
CHAT_MAX_LENGTH = 200
PICKUP_RANGE_PX = 32.0
AIR_GOTO_SPEED_PX = 4.0
AIR_GOTO_MAX_SPEED_PX = 8.0
AUTO_COLLECT_NEARBY = os.environ.get("NEO_AUTO_COLLECT", "1").lower() not in {
    "0", "false", "no", "off"
}

# captured from real client jump: (dy, wire vy) every ~50ms; net -108px then land
JUMP_ARC = [(-23, -7), (-20, -6), (-17, -5), (-15, -5), (-12, -4), (-9, -3),
            (-7, -2), (-4, -1), (-1, 0), (3, 1), (6, 2), (9, 4), (13, 5),
            (17, 6), (21, 7), (24, 8), (15, 0)]
JUMP_PACKET_SEC = 0.05

# --- world map / platformer physics (mirrors client constants) ---
TILE = 32
PLAYER_W, PLAYER_H = 22, 26
GRAVITY, JUMP_V, MAX_VY, MAX_VX = 0.32, -8.2, 13.0, 5.0
SIM_HZ = 60            # client physics ticks at 60Hz
SIM_SEND_EVERY = 3     # client emits a position packet every ~50ms (3 ticks)
SOLID_NAMES = {"DIRT", "GRASS", "STONE", "ROCK", "BEDROCK", "WORLD_LOCK",
               "SHOOTER_BLOCK", "SUGAR_CANE", "LAVA", "DUNGEON_LAVA"}
PASSABLE_NAMES = {"AIR", "TREE", "DOOR", "SIGN", "ENTRANCE", "WOODEN_ENTRANCE",
                  "SEED", "BUSH", "DAISY", "RED_ROSE", "POPPY", "SEAWEED",
                  "PORTAL", "GATE", "GATE1", "DUNGEON_GATE", "DUNGEON_EXIT", "DUNGEON_PORTAL"}
WORLD_W, WORLD_H = 100, 60
NAME_BY_ID = {}
NAME_TO_ID = {}
BLOCKS = {}
PLAYERS = {}   # uid -> {name, session, x, y}
SESSIONS = {}  # sessionId -> uid (batch_positions uidHash)
INVENTORY = {}  # item type -> count
GEMS = 0
INVENTORY_VERSION = 0
MAX_SLOTS = 10
JOIN_NONCE = None
DROPPED_ITEMS = {}  # server drop id -> latest ground item event
PICKUP_OPS = {}  # pickup opId -> ground drop id
CAPTCHA = {}  # challengeId -> bot_captcha event (latest)
CAPTCHA_ACTIVE = False  # True when server is blocking gameplay pending captcha
DROP_SNAPSHOT = None
NON_DROPPABLE_ITEMS = {"FIST", "WRENCH"}

os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("stay")
stop = threading.Event()
lock = threading.Lock()
state_lock = threading.Lock()
world_lock = threading.RLock()
BOT_NAME = os.environ.get("NEO_BOT_NAME", "")
CURRENT_UID = ""


def refresh_token():
    global CURRENT_UID
    acc = json.load(open(ACCOUNT))
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": acc["refresh_token"]}).encode()
    req = urllib.request.Request(
        f"https://securetoken.googleapis.com/v1/token?key={acc['api_key']}",
        data=body, headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    acc["id_token"] = r["id_token"]
    acc["refresh_token"] = r["refresh_token"]
    json.dump(acc, open(ACCOUNT, "w"), indent=2)
    CURRENT_UID = r["user_id"]
    log.info("token refreshed, uid=%s", r["user_id"])
    return r["id_token"]


def send(ws, obj):
    with lock:
        ws.send(json.dumps(obj))


def connection_ready(mover):
    """Return whether command handlers may send data to the worker."""
    if mover is None or not mover.alive or mover.ws is None:
        return False
    closed = getattr(mover.ws, "closed", False)
    return not closed


def valid_tile(x, y):
    try:
        tx, ty = int(x), int(y)
    except (TypeError, ValueError):
        return False
    return 0 <= tx < WORLD_W and 0 <= ty < WORLD_H


def tile_center(x, y):
    return (int(x) * TILE + TILE / 2, int(y) * TILE + TILE / 2)


def within_interaction_range(mover, x, y):
    if not connection_ready(mover):
        return False
    cx, cy = tile_center(x, y)
    px, py = mover.x + PLAYER_W / 2, mover.y + PLAYER_H / 2
    return math.hypot(cx - px, cy - py) < MAX_INTERACTION_TILES * TILE


def state_snapshot(mover, status=None):
    if status is None:
        status = "online" if connection_ready(mover) else "offline"
    position = None
    if mover is not None:
        position = {"x": round(mover.x, 2), "y": round(mover.y, 2),
                    "tile": list(tile_at(mover.x, mover.y))}
    with world_lock:
        blocks = dict(BLOCKS)
        names = dict(NAME_BY_ID)
        inventory = [{"type": item_type, "count": count}
                     for item_type, count in sorted(INVENTORY.items())]
        dropped_items = [dict(item) for item in DROPPED_ITEMS.values()]
        gems = GEMS
        inventory_version = INVENTORY_VERSION
        max_slots = MAX_SLOTS
        players = [
            {
                "uid": uid,
                "name": player.get("name", uid),
                "session": player.get("session"),
                "x": round(float(player.get("x", 0)), 2),
                "y": round(float(player.get("y", 0)), 2),
                "tile": list(tile_at(player.get("x", 0), player.get("y", 0))),
            }
            for uid, player in PLAYERS.items()
        ]
    return {
        "botId": BOT_ID,
        "name": BOT_NAME,
        "status": status,
        "world": WORLD,
        "updatedAt": time.time(),
        "position": position,
        "worldSize": {"width": WORLD_W, "height": WORLD_H},
        "blocks": {str(idx): int(tid) for idx, tid in blocks.items()},
        "blockNames": {str(tid): name for tid, name in names.items()},
        "inventory": inventory,
        "gems": gems,
        "inventoryVersion": inventory_version,
        "maxSlots": max_slots,
        "droppedItems": dropped_items,
        "players": players,
        "farm": farm_manager.status_dict(),
        "captcha": {
            "active": CAPTCHA_ACTIVE,
            "lastChallenge": next(iter(CAPTCHA.values()), None),
        },
    }


def write_state(mover, status=None):
    """Publish a dashboard-safe snapshot without credentials or socket data."""
    payload = state_snapshot(mover, status)
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    try:
        with state_lock:
            with open(tmp, "w") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, STATE_FILE)
    except Exception as exc:
        log.warning("state write failed: %s", exc)


class Mover:
    def __init__(self, ws, x, y):
        self.ws = ws
        self.x = float(x)
        self.y = float(y)
        self.target_x = None
        self.target_y = None
        self.facing_left = False
        self.seq = 0
        self.t0 = time.monotonic()
        self.alive = True
        self.need_send = True
        self.jump_until = 0.0
        self.max_speed = MAX_SPEED_TICK
        self.busy = False
        self.corrections = 0

    def packet(self, velx, vely, flags):
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        ts = int((time.monotonic() - self.t0) * 1000) & 0xFFFFFFFF
        return struct.pack(">BHIffhhBI", 1, 21, self.seq,
                           self.x, self.y, int(velx), int(vely), flags, ts)

    def tick(self):
        if self.busy:
            return
        velx = 0.0
        vely = 0.0
        moved = False
        if self.target_x is not None:
            rem = self.target_x - self.x
            if abs(rem) <= self.max_speed:
                self.x = self.target_x
                self.target_x = None
                moved = True
            else:
                step = self.max_speed if rem > 0 else -self.max_speed
                self.x += step
                velx = step
                self.facing_left = step < 0
                moved = True
        if self.target_y is not None:
            rem = self.target_y - self.y
            if abs(rem) <= self.max_speed:
                self.y = self.target_y
                self.target_y = None
                moved = True
            else:
                step = self.max_speed if rem > 0 else -self.max_speed
                self.y += step
                vely = step
                moved = True
        if not moved and not self.need_send and time.monotonic() >= self.jump_until:
            return
        self.need_send = False
        flags = (1 if self.facing_left else 0)
        if velx != 0:
            flags |= 2 if velx < 0 else 4
        if vely < 0:
            flags |= 8
        elif vely > 0:
            flags |= 16
        if time.monotonic() < self.jump_until:
            flags |= 8
        pkt = self.packet(velx, vely, flags)
        with lock:
            self.ws.send(pkt)

    def set_target(self, dx):
        if self.target_x is None:
            base = self.x
        else:
            base = self.target_x
        self.target_x = base + dx
        log.info("target %.1f (dx=%.0f) from %.1f", self.target_x, dx, self.x)

    def set_target_y(self, dy):
        if self.target_y is None:
            base = self.y
        else:
            base = self.target_y
        self.target_y = base + dy
        log.info("target_y %.1f (dy=%.0f) from %.1f", self.target_y, dy, self.y)

    def correct(self, x, y):
        log.warning("correction -> (%.1f,%.1f) was (%.1f,%.1f)", x, y, self.x, self.y)
        self.x, self.y = x, y
        self.corrections += 1

    def tp(self, x, y):
        log.info("teleport -> (%.1f,%.1f) from (%.1f,%.1f)", x, y, self.x, self.y)
        self.x, self.y = x, y
        self.target_x = None
        self.target_y = None
        self.need_send = True

    def jump(self, hold=0.2):
        self.jump_until = time.monotonic() + hold
        log.info("jump (hold %.2fs)", hold)

    def loop(self):
        interval = 1.0 / TICK_HZ
        next_t = time.monotonic()
        while self.alive and not stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log.warning("tick failed: %s", e)
                return
            next_t += interval
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()


def is_solid(tid):
    name = NAME_BY_ID.get(tid)
    if name is not None:
        if not name or name == "AIR":
            return False
        if name in PASSABLE_NAMES:
            return False
        if (name.endswith("_SEED") or name.endswith("_TREE") or
                name.endswith("_DOOR") or name.endswith("_SIGN") or
                name.endswith("_ENTRANCE") or "ENTRANCE" in name or
                "PORTAL" in name or "GATE" in name):
            return False
        if name.endswith("_BG") or name.endswith("_WALLPAPER") or name.endswith("_BACKGROUND") or name.endswith("_WALL"):
            return False
        if name in SOLID_NAMES:
            return True
        if name.endswith("_BLOCK") or name.endswith("_BRICKS") or name.endswith("_BRICK") or name.endswith("_ROCK"):
            return True
        if any(k in name for k in ["STONE", "DIRT", "GRASS", "LAVA", "BEDROCK", "ROCK"]):
            return True
        return False
    return tid in (3, 4, 5, 8, 10, 16, 25)


def solid_tile(tx, ty):
    if tx < 0 or ty < 0 or tx >= WORLD_W or ty >= WORLD_H:
        return True
    return is_solid(BLOCKS.get(ty * WORLD_W + tx, 0))


def aabb_hits(x, y):
    x0 = int(math.floor(x))
    y0 = int(math.floor(y))
    x1 = int(math.floor(x + PLAYER_W - 0.01))
    y1 = int(math.floor(y + PLAYER_H - 0.01))
    for ty in range(y0 // TILE, y1 // TILE + 1):
        for tx in range(x0 // TILE, x1 // TILE + 1):
            if solid_tile(tx, ty):
                return True
    return False


def stand_pos(tx, ty):
    return tx * TILE, (ty + 1) * TILE - PLAYER_H


def tile_at(x, y):
    """Support tile under the player's center (allows standing on a ledge edge)."""
    return (int((x + PLAYER_W / 2) // TILE),
            int((y + PLAYER_H) // TILE) - 1)


def can_stand(tx, ty):
    x, y = stand_pos(tx, ty)
    if x < 0 or y < 0 or x + PLAYER_W > WORLD_W * TILE or y + PLAYER_H > WORLD_H * TILE:
        return False
    if not solid_tile(tx, ty + 1):
        return False
    return not aabb_hits(x, y)


def _js_round(v):
    return math.floor(v + 0.5)


class Sim:
    """Client-accurate movement sim: subpixel accumulator, gravity after move."""

    def __init__(self, x, y):
        self.x = float(x)
        self.y = float(y)
        self.vx = self.vy = 0.0
        self.ax = self.ay = 0.0
        self.grounded = aabb_hits(self.x, self.y + 1)

    def _move(self, axis, vel):
        pos = self.x if axis == 0 else self.y
        acc = self.ax if axis == 0 else self.ay
        base = math.floor(pos)
        i = vel + acc
        c = _js_round(i)
        if c == 0:
            if vel == 0:
                return False
            c = 1 if vel > 0 else -1
        if axis == 0:
            blocked = aabb_hits(base + c, self.y)
        else:
            blocked = aabb_hits(self.x, base + c)
        if not blocked:
            new_pos, new_acc, collided = base + c, i - c, False
        else:
            step = 1 if c > 0 else -1
            p = base
            while abs(p - base) < abs(c):
                nxt = p + step
                if (aabb_hits(nxt, self.y) if axis == 0 else aabb_hits(self.x, nxt)):
                    break
                p = nxt
            new_pos, new_acc, collided = p, 0.0, True
        if axis == 0:
            self.x, self.ax = new_pos, new_acc
        else:
            self.y, self.ay = new_pos, new_acc
        return collided

    def step(self, direction, jump=False):
        if direction:
            self.vx += direction * 0.9
            self.vx = max(-MAX_VX, min(MAX_VX, self.vx))
        else:
            if self.vx > 0:
                self.vx = max(0.0, self.vx - 0.9)
            elif self.vx < 0:
                self.vx = min(0.0, self.vx + 0.9)
            if abs(self.vx) < 0.1:
                self.vx = 0.0
        if jump and self.grounded:
            self.vy = JUMP_V
            self.grounded = False
        self._move(0, self.vx)
        landed = self._move(1, self.vy)
        if landed:
            if self.vy > 0:
                self.grounded = True
            self.vy = 0.0
        else:
            self.grounded = False
        self.vy += GRAVITY
        if self.vy > MAX_VY:
            self.vy = MAX_VY


def edge_steps(x, y, direction, kind):
    """Simulate one path edge from a position; returns (dest_tile, states).

    state = (x, y, vx, vy, airborne, ctrl) at SIM_HZ.
    """
    sim = Sim(x, y)
    sim.grounded = True
    start_tx = int((x + PLAYER_W / 2) // TILE)
    states = []
    fell = False
    max_t = 100 if kind == "hop" else 180 if kind in ("drop", "fall") else 40
    for t in range(max_t):
        sim.step(direction, jump=(kind == "hop" and t == 0))
        states.append((sim.x, sim.y, sim.vx, sim.vy,
                       t == 0 or not sim.grounded, direction))
        if kind == "hop":
            if t > 3 and sim.grounded:
                break
        elif kind == "walk":
            if sim.grounded and int((sim.x + PLAYER_W / 2) // TILE) != start_tx:
                break
        else:
            if not sim.grounded and t > 1:
                fell = True
            if fell and sim.grounded:
                break
    dest = tile_at(sim.x, sim.y)
    return dest, states


def jump_aim_steps(x, y, target_cx):
    """Jump with air control: accelerate toward target_cx, brake above it."""
    direction = 1 if target_cx >= x else -1
    sim = Sim(x, y)
    sim.grounded = True
    states = []
    for t in range(100):
        rem = target_cx - sim.x
        if direction > 0:
            ctrl = 1 if rem > 18 else -1 if rem < -4 else 0
        else:
            ctrl = -1 if rem < -18 else 1 if rem > 4 else 0
        sim.step(ctrl, jump=(t == 0))
        states.append((sim.x, sim.y, sim.vx, sim.vy,
                       t == 0 or not sim.grounded, ctrl))
        if t > 3 and sim.grounded:
            break
    dest = tile_at(sim.x, sim.y)
    return dest, states


def edge_dest(src, direction, kind):
    x, y = stand_pos(*src)
    return edge_steps(x, y, direction, kind)[0]


def sim_jump(src, direction, jump=True, ticks=100):
    return edge_dest(src, direction, "hop" if jump else "drop")


def sim_drop(src, direction, ticks=140):
    return edge_dest(src, direction, "drop")


def _nearest_standable(tx, ty, radius=6):
    best, best_d = None, None
    for r in range(radius + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                nx, ny = tx + dx, ty + dy
                if can_stand(nx, ny):
                    d = abs(dx) + abs(dy)
                    if best_d is None or d < best_d:
                        best, best_d = (nx, ny), d
        if best is not None:
            return best
    return None


def find_path(start, goal, max_nodes=5000):
    import heapq
    if not can_stand(*start):
        start = _nearest_standable(*start, radius=2)
        if start is None:
            return None
    if not can_stand(*goal):
        goal = _nearest_standable(*goal)
        if goal is None:
            return None
    if start == goal:
        return []
    cache = {}

    def neighbors(node):
        if node in cache:
            return cache[node]
        tx, ty = node
        out = []
        seen = set()
        for direction in (-1, 1):
            for kind in ("walk", "drop"):
                dest = edge_dest(node, direction, kind)
                if dest == node or not can_stand(*dest):
                    continue
                if (dest, kind) not in seen:
                    seen.add((dest, kind))
                    out.append((dest, direction, kind))
            sx, sy = stand_pos(tx, ty)
            for dx in range(1, 10):
                for dy in range(-3, 4):
                    cand = (tx + direction * dx, ty + dy)
                    if not can_stand(*cand):
                        continue
                    if any(d == cand for d, _, _ in out):
                        continue
                    cx = stand_pos(*cand)[0] + 5
                    dest, _ = jump_aim_steps(sx, sy, cx)
                    if dest == cand:
                        out.append((cand, direction, "jump"))
            dest = edge_dest(node, direction, "hop")
            if dest != node and can_stand(*dest) and (dest, "hop") not in seen:
                seen.add((dest, "hop"))
                out.append((dest, direction, "hop"))
        dest = edge_dest(node, 0, "fall")
        if dest != node and can_stand(*dest):
            out.append((dest, 0, "fall"))
        cache[node] = out
        return out

    def heur(n):
        return abs(n[0] - goal[0]) + abs(n[1] - goal[1])

    def edge_cost(src, dest, kind):
        """Prefer short, grounded steps while keeping long jumps as fallback."""
        dx = abs(dest[0] - src[0])
        dy = abs(dest[1] - src[1])
        if kind == "walk":
            return 1
        if kind == "hop":
            return 2 + dx + dy
        if kind == "jump":
            return 2 + dx + dy
        if kind in ("drop", "fall"):
            return max(1, dx + dy)
        return 1 + dx + dy

    heap = [(heur(start), 0, start)]
    came = {}
    cost = {start: 0}
    seen = set()
    while heap and len(seen) < max_nodes:
        _, c, node = heapq.heappop(heap)
        if node in seen:
            continue
        seen.add(node)
        if node == goal:
            actions = []
            cur = node
            while cur in came:
                prev, direction, kind = came[cur]
                actions.append((direction, kind, cur))
                cur = prev
            actions.reverse()
            return actions
        for nxt, direction, kind in neighbors(node):
            nc = c + edge_cost(node, nxt, kind)
            if nc < cost.get(nxt, 1 << 30):
                cost[nxt] = nc
                came[nxt] = (node, direction, kind)
                heapq.heappush(heap, (nc + heur(nxt), nc, nxt))
    return None


def send_sim(mover, sim, direction, airborne):
    flags = 0
    if direction < 0 or sim.vx < -0.5:
        flags |= 2
    elif direction > 0 or sim.vx > 0.5:
        flags |= 4
    if sim.vx < -0.5:
        flags |= 1
    if airborne:
        flags |= 8
    pkt = mover.packet(int(_js_round(sim.vx)), int(_js_round(sim.vy)), flags)
    mover.x, mover.y = sim.x, sim.y
    with lock:
        mover.ws.send(pkt)


def do_goto_tile(mover, tx, ty):
    if mover.busy:
        return
    if not BLOCKS:
        log.warning("goto_tile: block map not loaded")
        return
    goal = (tx, ty)
    mover.busy = True
    try:
        corrections0 = mover.corrections
        tick = 1.0 / SIM_HZ
        next_t = time.monotonic()

        def pace():
            nonlocal next_t
            next_t += tick
            d = next_t - time.monotonic()
            if d > 0:
                time.sleep(d)
            else:
                next_t = time.monotonic()

        def snap_x(sim):
            """Shift onto the canonical tile-left x so planned jumps match physics."""
            tx, ty = tile_at(sim.x, sim.y)
            if not can_stand(tx, ty):
                return
            target = float(stand_pos(tx, ty)[0])
            if abs(sim.x - target) <= 0.5:
                return
            log.info("align %s x %.1f -> %.1f", (tx, ty), sim.x, target)
            for _ in range(20):
                if abs(sim.x - target) <= 0.5:
                    break
                step = max(-5.0, min(5.0, target - sim.x))
                sim.x += step
                sim.vx = step
                send_sim(mover, sim, 1 if step > 0 else -1, False)
                pace()
            sim.vx = 0.0

        def landing_is_valid(sim, dest):
            """Validate that the simulated landing matches the planned destination and is physically standable."""
            actual = tile_at(sim.x, sim.y)
            if actual != dest:
                log.info("landing mismatch: actual %s != planned %s", actual, dest)
                return False
            if not can_stand(*actual):
                log.warning("landing tile %s is not standable", actual)
                return False
            x, y = stand_pos(*actual)
            if x < 0 or y < 0 or x + PLAYER_W > WORLD_W * TILE or y + PLAYER_H > WORLD_H * TILE:
                log.warning("landing tile %s out of bounds", actual)
                return False
            if aabb_hits(x, y):
                log.warning("landing tile %s has collision", actual)
                return False
            return True

        max_steps = max(64, abs(goal[0] - tile_at(mover.x, mover.y)[0]) * 8,
                        abs(goal[1] - tile_at(mover.x, mover.y)[1]) * 8)
        consecutive_mismatches = 0
        for step_no in range(max_steps):
            snap_x(Sim(mover.x, mover.y))
            start = tile_at(mover.x, mover.y)
            path = find_path(start, goal)
            if path is None:
                log.warning("goto_tile: no path from %s to %s", start, goal)
                return
            if not path:
                log.info("goto_tile done at (%.0f,%.0f) tile %s",
                         mover.x, mover.y, start)
                return
            log.info("goto_tile step %d %s -> %s: next=%s remaining=%d edges",
                     step_no + 1, start, goal, path[0][2], len(path))
            sim = Sim(mover.x, mover.y)
            direction, kind, dest = path[0]
            if mover.corrections != corrections0:
                log.warning("goto_tile aborted: server corrected position")
                return
            if kind == "jump":
                _, states = jump_aim_steps(sim.x, sim.y,
                                           stand_pos(*dest)[0] + 5)
            else:
                _, states = edge_steps(sim.x, sim.y, direction, kind)
            log.info("goto edge %s dir=%+d from (%.0f,%.0f) -> %s (%d ticks)",
                     kind, direction, sim.x, sim.y, dest, len(states))
            last_ctrl = direction
            for i, (sx, sy, vx, vy, air, ctrl) in enumerate(states):
                sim.x, sim.y, sim.vx, sim.vy = sx, sy, vx, vy
                last_ctrl = ctrl
                if i % SIM_SEND_EVERY == 0:
                    send_sim(mover, sim, ctrl, air)
                pace()
                if mover.corrections != corrections0:
                    log.warning("goto_tile aborted mid-edge: server corrected position")
                    return
            send_sim(mover, sim, last_ctrl, False)
            if not landing_is_valid(sim, dest):
                consecutive_mismatches += 1
                log.warning("goto edge ended invalid (streak %d); replanning from actual position",
                            consecutive_mismatches)
                if consecutive_mismatches >= 3:
                    log.warning("goto_tile gave up after %d consecutive invalid landings at (%.0f,%.0f)",
                                consecutive_mismatches, mover.x, mover.y)
                    return
                continue
            consecutive_mismatches = 0
            snap_x(sim)
        log.warning("goto_tile gave up after %d steps at (%.0f,%.0f)",
                    max_steps,
                    mover.x, mover.y)
    except Exception as e:
        log.warning("goto_tile failed: %s", e)
    finally:
        mover.need_send = True
        mover.busy = False


def connect_master(token):
    m = websocket.create_connection(BASE + "/ws/master" + Q, timeout=25,
                                    origin="https://playneotopia.com")
    m.settimeout(15)
    send(m, {"type": "auth", "token": token})
    ev = json.loads(m.recv())
    if ev.get("type") != "auth_success":
        raise RuntimeError(f"master auth failed: {ev}")
    log.info("master auth_success name=%s tutorial=%s", ev.get("canonicalName"),
             ev.get("tutorialState"))
    display_name = ev.get("canonicalName") or (BOT_NAME if BOT_NAME else None)
    if not display_name:
        try:
            acc_file = json.load(open(ACCOUNT))
            display_name = acc_file.get("canonical_name") or None
        except Exception:
            display_name = None
    if not display_name:
        raise RuntimeError("master returned no canonicalName and NEO_BOT_NAME is empty")
    log.info("join_world with name=%s", display_name)
    send(m, {"type": "join_world", "worldName": WORLD, "protocolVersion": 6,
             "movementProfile": "tuned-timed-v1", "pos": {"x": 0, "y": 0},
             "name": display_name, "skin": {}, "country": None,
             "platform": "desktop"})
    route = json.loads(m.recv())
    if route.get("type") != "route":
        raise RuntimeError(f"no route: {route}")
    log.info("route worker=%s path=%s", route.get("workerId"), route.get("wsPath"))
    return m, route, ev.get("canonicalName")


def connect_worker(route):
    w = ws_connect(BASE + route["wsPath"] + Q, origin="https://playneotopia.com",
                   compression="deflate", open_timeout=25, max_size=None,
                   additional_headers={"User-Agent": UA})
    send(w, {"type": "auth_ticket", "ticket": route["ticket"]})
    while not stop.is_set():
        try:
            raw = w.recv(timeout=15)
        except TimeoutError:
            continue
        except ConnectionClosed as e:
            raise RuntimeError(f"worker closed before join: {e}")
        ev = json.loads(raw)
        if ev.get("type") == "world_joined":
            for p in ev.get("players") or []:
                track_player(p)
            log.info("JOINED world=%s name=%s pos=(%s,%s) players=%d",
                     ev.get("worldName"), ev.get("name"), ev.get("spawnX"),
                     ev.get("spawnY"), len(ev.get("players") or []))
            for p in PLAYERS.values():
                log.info("  player %s session=%s pos=(%.0f,%.0f)", p["name"],
                         p["session"], p["x"], p["y"])
            return w, ev
        if ev.get("type") in ("world_join_reject", "auth_failed"):
            raise RuntimeError(f"worker rejected: {ev}")
    raise RuntimeError("stopped before join")


def apply_meta(ev):
    global WORLD_W, WORLD_H, NAME_BY_ID, NAME_TO_ID
    with world_lock:
        WORLD_W = int(ev.get("width", WORLD_W))
        WORLD_H = int(ev.get("height", WORLD_H))
        reg = ev.get("registry") or {}
        NAME_BY_ID = {int(v): k for k, v in reg.items()}
        NAME_TO_ID = {k: int(v) for k, v in reg.items()}
    log.info("world meta %dx%d registry=%d", WORLD_W, WORLD_H, len(NAME_BY_ID))


def decode_blocks(data):
    t = data[1:]
    if len(t) < 8 or t[:4] != b"NEOW":
        return
    ver = t[4]
    cnt = int.from_bytes(t[6:8], "big")
    size = 9 if ver >= 3 else 7 if ver >= 2 else 5
    blocks = {}
    for i in range(cnt):
        o = 8 + i * size
        if o + size > len(t):
            break
        idx = int.from_bytes(t[o:o + 2], "big")
        typ = int.from_bytes(t[o + 2:o + 4], "big") if ver >= 2 else t[o + 2]
        blocks[idx] = typ
    with world_lock:
        BLOCKS.clear()
        BLOCKS.update(blocks)
    log.info("block map: %d entries (%dx%d v%d)", len(BLOCKS), WORLD_W, WORLD_H, ver)


def apply_block_update(ev):
    x, y = ev.get("x"), ev.get("y")
    if x is None or y is None:
        return
    name = (ev.get("block") or {}).get("type") or ev.get("blockType")
    tid = NAME_TO_ID.get(name)
    if tid is None:
        return
    with world_lock:
        BLOCKS[int(y) * WORLD_W + int(x)] = tid


def track_player(ev):
    uid = ev.get("uid")
    if not uid:
        return
    with world_lock:
        p = PLAYERS.setdefault(uid, {"name": uid, "session": None, "x": 0.0, "y": 0.0})
        if ev.get("name"):
            p["name"] = ev["name"]
        pos = ev.get("pos") or {}
        if "x" in pos:
            p["x"] = float(pos["x"])
        if "y" in pos:
            p["y"] = float(pos["y"])
        if "x" in ev and "y" in ev:
            p["x"], p["y"] = float(ev["x"]), float(ev["y"])
        if ev.get("sessionId") is not None:
            p["session"] = int(ev["sessionId"])
            SESSIONS[p["session"]] = uid


def track_leave(ev):
    uid = ev.get("uid")
    with world_lock:
        p = PLAYERS.pop(uid, None)
        if p and p.get("session") is not None:
            SESSIONS.pop(p["session"], None)
        if ev.get("sessionId") is not None:
            SESSIONS.pop(int(ev["sessionId"]), None)


def update_batch(data):
    cnt = int.from_bytes(data[1:3], "big")
    with world_lock:
        for i in range(cnt):
            o = 3 + i * 21
            if o + 21 > len(data):
                break
            h, x, y = struct.unpack(">Iff", data[o:o + 12])
            uid = SESSIONS.get(h)
            if uid in PLAYERS:
                PLAYERS[uid]["x"], PLAYERS[uid]["y"] = x, y


def apply_inventory(ev, direction=0):
    """Apply an authoritative inventory update or a small ack delta."""
    global GEMS, INVENTORY_VERSION, MAX_SLOTS, JOIN_NONCE
    with world_lock:
        items = ev.get("inventory")
        if isinstance(items, list):
            updated = {}
            for item in items:
                if not isinstance(item, dict) or not item.get("type"):
                    continue
                item_type = str(item["type"]).upper()
                try:
                    count = max(0, int(item.get("count", 0)))
                except (TypeError, ValueError):
                    continue
                updated[item_type] = updated.get(item_type, 0) + count
            INVENTORY.clear()
            INVENTORY.update(updated)
        elif direction and ev.get("itemType"):
            item_type = str(ev["itemType"]).upper()
            try:
                count = int(ev.get("count", 0)) * direction
            except (TypeError, ValueError):
                count = 0
            INVENTORY[item_type] = max(0, INVENTORY.get(item_type, 0) + count)
            if INVENTORY[item_type] == 0:
                INVENTORY.pop(item_type, None)

        if "gems" in ev:
            try:
                GEMS = max(0, int(ev["gems"]))
            except (TypeError, ValueError):
                pass
        if "inventoryVersion" in ev:
            try:
                INVENTORY_VERSION = max(INVENTORY_VERSION, int(ev["inventoryVersion"]))
            except (TypeError, ValueError):
                pass
        if "maxSlots" in ev:
            try:
                MAX_SLOTS = max(1, int(ev["maxSlots"]))
            except (TypeError, ValueError):
                pass
        if ev.get("joinNonce"):
            JOIN_NONCE = ev["joinNonce"]


def apply_ground_drop(ev):
    item_id = ev.get("id")
    if not item_id:
        return
    item_key = str(item_id)
    try:
        count = int(ev.get("count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    with world_lock:
        item = dict(DROPPED_ITEMS.get(item_key, {}))
    item.update({"id": item_id, "count": count})
    if ev.get("itemType") is not None:
        item["itemType"] = ev["itemType"]
    if ev.get("pos"):
        item["pos"] = ev["pos"]
    for key in ("actorUid", "uid", "timestamp"):
        if key in ev:
            item[key] = ev[key]
    with world_lock:
        if item["count"] > 0:
            DROPPED_ITEMS[item_key] = item
        else:
            DROPPED_ITEMS.pop(item_key, None)


def remove_ground_drop(ev):
    item_id = ev.get("id")
    op_id = ev.get("opId")
    with world_lock:
        if item_id:
            DROPPED_ITEMS.pop(str(item_id), None)
        if op_id:
            PICKUP_OPS.pop(str(op_id), None)


def decode_drop_snapshot(data):
    """Decode worker opcode 17 (version-1 ground-drop snapshot chunk)."""
    if len(data) < 16 or data[0] != 17 or data[1] != 1:
        return None
    snapshot_id = int.from_bytes(data[2:6], "big")
    chunk_index = int.from_bytes(data[6:8], "big")
    chunk_count = int.from_bytes(data[8:10], "big")
    entry_count = int.from_bytes(data[10:12], "big")
    total_count = int.from_bytes(data[12:16], "big")
    if not 1 <= chunk_count <= 2048 or chunk_index >= chunk_count:
        return None
    if entry_count > 2048 or total_count > 120000:
        return None

    drops = []
    offset = 16
    for _ in range(entry_count):
        if offset + 12 > len(data):
            return None
        id_kind = data[offset]
        offset += 1
        if id_kind not in (0, 1):
            return None
        item_id = int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
        count = data[offset]
        offset += 1
        x, y = struct.unpack(">ff", data[offset:offset + 8])
        offset += 8
        if count < 1 or not math.isfinite(x) or not math.isfinite(y):
            return None
        if id_kind == 0:
            if offset + 10 > len(data):
                return None
            high = int.from_bytes(data[offset:offset + 6], "big")
            offset += 6
            low = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            drop_id = f"drop_{high}_{low}"
        else:
            name_len = data[offset]
            offset += 1
            if name_len < 1 or offset + name_len > len(data):
                return None
            drop_id = data[offset:offset + name_len].decode("utf-8", errors="replace")
            offset += name_len
        drops.append({
            "id": drop_id,
            "itemType": NAME_BY_ID.get(item_id, f"ID_{item_id}"),
            "count": count,
            "pos": {"x": x, "y": y},
        })
    if offset != len(data):
        return None
    return {
        "snapshotId": snapshot_id,
        "chunkIndex": chunk_index,
        "chunkCount": chunk_count,
        "totalCount": total_count,
        "drops": drops,
    }


def apply_drop_snapshot_chunk(chunk):
    global DROP_SNAPSHOT
    if not chunk:
        return []
    with world_lock:
        if (DROP_SNAPSHOT is None
                or DROP_SNAPSHOT["snapshotId"] != chunk["snapshotId"]
                or DROP_SNAPSHOT["chunkCount"] != chunk["chunkCount"]
                or DROP_SNAPSHOT["totalCount"] != chunk["totalCount"]):
            DROP_SNAPSHOT = {
                "snapshotId": chunk["snapshotId"],
                "chunkCount": chunk["chunkCount"],
                "totalCount": chunk["totalCount"],
                "chunks": {},
            }
        snapshot = DROP_SNAPSHOT
        # Always replace chunk (handles periodic resend with same id but new content)
        snapshot["chunks"][chunk["chunkIndex"]] = chunk["drops"]
        if len(snapshot["chunks"]) != snapshot["chunkCount"]:
            return []
        drops = []
        for index in range(snapshot["chunkCount"]):
            drops.extend(snapshot["chunks"].get(index, []))
        if len(drops) != snapshot["totalCount"]:
            DROP_SNAPSHOT = None
            return []
        DROPPED_ITEMS.clear()
        DROPPED_ITEMS.update({str(item["id"]): item for item in drops})
        DROP_SNAPSHOT = None
        return drops


def decode_drop_event(data):
    """Worker opcode 8: new ground drop.

    Layout: opcode | 13-byte header | u8 id_len | drop_id | u8 src_len | source_uid
    Header fields: u16 a, u8 item_id, u16 count, u16 x_grid, u16 y_grid, u16 ?
    """
    if len(data) < 14 or data[0] != 8:
        return None
    hdr = data[1:14]
    item_id = hdr[2]
    count = int.from_bytes(hdr[3:5], "big")
    x_grid = int.from_bytes(hdr[5:7], "big")
    y_grid = int.from_bytes(hdr[7:9], "big")
    i = 14
    id_len = data[i]
    i += 1
    if i + id_len > len(data):
        return None
    drop_id = data[i:i + id_len].decode("utf-8", errors="replace")
    i += id_len
    if i + 1 > len(data):
        return None
    src_len = data[i]
    i += 1
    source_uid = data[i:i + src_len].decode("utf-8", errors="replace")
    i += src_len
    if i != len(data) or not drop_id:
        return None
    # x mapping verified: xg=17716 -> px=2880, xg=17708 -> px=2752 (resid=0).
    # y mapping not yet derivable; use mover position estimate in apply_drop_event.
    px = (x_grid - 17536) * 16
    pos = None
    if 0 <= px < WORLD_W * 32:
        pos = {"x": px}
    return {
        "id": drop_id,
        "itemType": NAME_BY_ID.get(item_id, f"ID_{item_id}"),
        "count": count,
        "sourceUid": source_uid,
        "pos": pos,
    }


def decode_drop_removal(data):
    """Worker opcode 9: ground drop removed: id string + source uid string."""
    if len(data) < 3 or data[0] != 9:
        return None
    i = 1
    id_len = data[i]
    i += 1
    if i + id_len > len(data):
        return None
    drop_id = data[i:i + id_len].decode("utf-8", errors="replace")
    i += id_len
    if i + 1 > len(data):
        return None
    src_len = data[i]
    i += 1
    source_uid = data[i:i + src_len].decode("utf-8", errors="replace")
    i += src_len
    if i != len(data) or not drop_id:
        return None
    return {"id": drop_id, "sourceUid": source_uid}


def apply_drop_event(ev, mover):
    """Ground drop from another player: add to DROPPED_ITEMS. The event carries
    an x pixel position (from x_grid) but no reliable y, so fall back to the
    mover's or tracked player's y when it is missing."""
    drop_id = ev["id"]
    source = ev.get("sourceUid") or ""
    with world_lock:
        item = dict(DROPPED_ITEMS.get(drop_id, {}))
    item.update({
        "id": drop_id,
        "itemType": ev.get("itemType"),
        "count": int(ev.get("count") or 0),
        "sourceUid": source,
    })
    ev_pos = ev.get("pos") or {}
    with world_lock:
        pos = dict(ev_pos)
        if "x" not in pos or "y" not in pos:
            if source in PLAYERS:
                tracked = PLAYERS[source]
                pos.setdefault("x", round(tracked.get("x", 0), 2))
                pos.setdefault("y", round(tracked.get("y", 0), 2))
            elif mover is not None:
                pos.setdefault("x", round(mover.x, 2))
                pos.setdefault("y", round(mover.y, 2))
        if pos:
            item["pos"] = pos
        DROPPED_ITEMS[drop_id] = item
    log.info("drop event +%s x%s by %s", ev.get("itemType"),
             ev.get("count"), source or "?")
    write_state(mover, "online")


def apply_drop_removal(ev):
    drop_id = ev["id"]
    with world_lock:
        removed = DROPPED_ITEMS.pop(drop_id, None)
    log.info("drop removed %s (had %s)", drop_id,
             f"{removed.get('itemType')} x{removed.get('count')}"
             if removed else "unknown")
    if removed:
        write_state(None, "online")


def reader(w, mover):
    while not stop.is_set():
        try:
            data = w.recv(timeout=30)
        except TimeoutError:
            continue
        except ConnectionClosed as e:
            rcvd = e.rcvd
            log.warning("server CLOSE code=%s reason=%r",
                        rcvd.code if rcvd else None, rcvd.reason if rcvd else "")
            return
        except Exception as e:
            log.warning("worker recv closed: %s", e)
            return
        try:
            handle_message(data, mover)
        except Exception as e:
            log.warning("message handling failed: %r", e)


def handle_message(data, mover):
    if isinstance(data, bytes):
        if len(data) >= 9 and data[0] == 6:
            x, y = struct.unpack(">ff", data[1:9])
            mover.correct(x, y)
        elif data and data[0] == 16:
            decode_blocks(data)
        elif data and data[0] == 17:
            chunk = decode_drop_snapshot(data)
            drops = apply_drop_snapshot_chunk(chunk)
            if chunk:
                log.info("drop snapshot chunk %d/%d entries=%d total=%d",
                         chunk["chunkIndex"] + 1, chunk["chunkCount"],
                         len(chunk["drops"]), chunk["totalCount"])
            if drops:
                log.info("drop snapshot ready: %d ground items", len(drops))
                write_state(mover, "online")
        elif len(data) >= 3 and data[0] == 4:
            update_batch(data)
        elif data and data[0] == 8:
            ev = decode_drop_event(data)
            if ev:
                apply_drop_event(ev, mover)
            else:
                log.info("bin opcode=8 undecoded len=%d hex=%s",
                         len(data), data[:80].hex())
        elif data and data[0] == 9:
            ev = decode_drop_removal(data)
            if ev:
                apply_drop_removal(ev)
            else:
                log.info("bin opcode=9 undecoded len=%d hex=%s",
                         len(data), data[:80].hex())
        elif data and data[0] == 7:
            log.info("drop counter opcode=7 len=%d hex=%s",
                     len(data), data[:80].hex())
        elif data:
            log.info("bin opcode=%d len=%d hex=%s", data[0], len(data), data[:80].hex())
        return
    try:
        ev = json.loads(data)
    except Exception:
        return
    t = ev.get("type")
    if t in ("bot_captcha", "verification_required"):
        global CAPTCHA_ACTIVE
        CAPTCHA_ACTIVE = True
    if t == "world_snapshot_meta":
        apply_meta(ev)
        return
    if t == "inventory_snapshot":
        apply_inventory(ev)
        log.info("inventory snapshot items=%d gems=%d version=%d",
                 len(INVENTORY), GEMS, INVENTORY_VERSION)
        return
    if t in ("drop_ack", "pickup_ack"):
        apply_inventory(ev, -1 if t == "drop_ack" else 1)
    elif t == "drop_reject":
        log.warning("drop rejected opId=%s item=%s reason=%s",
                    ev.get("opId"), ev.get("itemType"), ev.get("reason"))
    elif t == "pickup_reject":
        log.warning("pickup rejected opId=%s id=%s reason=%s",
                    ev.get("opId"), ev.get("id"), ev.get("reason"))
        with world_lock:
            PICKUP_OPS.pop(str(ev.get("opId")), None)
    if t in ("dropped_item", "drop_ack", "drop_merged", "drop_count_updated"):
        if t != "drop_ack" or ev.get("groundPresent", True):
            apply_ground_drop(ev)
            if t == "dropped_item" and ev.get("itemType"):
                p = ev.get("pos") or {}
                log.info("dropped_item %s x%s at (%s,%s) by %s",
                         ev.get("itemType"), ev.get("count", 0),
                         p.get("x"), p.get("y"), ev.get("actorUid") or ev.get("uid"))
            if t == "dropped_item":
                write_state(mover, "online")
        if t == "dropped_item" and ev.get("id"):
            actor_uid = ev.get("actorUid") or ev.get("uid")
            if actor_uid != CURRENT_UID:
                maybe_auto_pickup(mover, str(ev["id"]))
    elif t == "pickup_ack":
        remove_ground_drop(ev)
    elif t == "item_picked_up":
        remove_ground_drop(ev)
    if t in ("block_update", "block_ack"):
        apply_block_update(ev)
    if t == "player_joined":
        track_player(ev)
        log.info("player joined %s session=%s pos=(%s,%s)", ev.get("name"),
                 ev.get("sessionId"), (ev.get("pos") or {}).get("x"),
                 (ev.get("pos") or {}).get("y"))
        return
    if t == "player_position":
        track_player(ev)
        return
    if t == "player_left":
        track_leave(ev)
        log.info("player left %s", ev.get("uid"))
        return
    if t in ("weather_snapshot", "butterfly_snapshot", "butterfly_spawn",
             "npc_snapshot", "npc_spawn", "ambient_ships_config",
             "batch_positions"):
        return
    if t == "bot_captcha":
        challenge_id = str(ev.get("challengeId", ""))
        CAPTCHA.clear()
        CAPTCHA[challenge_id] = ev
        log.warning("bot_captcha action=%s provider=%s siteKey=%s challengeId=%s "
                    "cData=%s token=%s — gameplay locked until resolved",
                    ev.get("action"), ev.get("provider"), ev.get("siteKey"),
                    challenge_id, ev.get("cData"), ev.get("token"))
        return
    if t == "verification_required":
        log.warning("verification_required reason=%s msg=%s — gameplay locked",
                    ev.get("reasonCode"), ev.get("message"))
        return
    log.info("msg %s %s", t, json.dumps(ev)[:300])


def do_punch(ws, x, y, count, delay, old_type="DIRT"):
    for i in range(count):
        if stop.is_set():
            return
        send(ws, {"type": "expression", "expressionId": "punch"})
        send(ws, {"type": "punch", "x": x, "y": y})
        # real client also sends an optimistic break claim; server validates it
        send(ws, {"type": "block_update", "opId": str(uuid.uuid4()), "x": x, "y": y,
                  "action": "break", "blockType": "AIR",
                  "block": {"type": "AIR", "health": 0, "maxHealth": 0,
                            "backgroundType": "CAVE_BG"},
                  "oldBlockType": old_type})
        log.info("punch %d/%d at tile (%d,%d)", i + 1, count, x, y)
        time.sleep(delay)


def block_name_at(x, y):
    return NAME_BY_ID.get(BLOCKS.get(y * WORLD_W + x, 0), "DIRT")


def do_combo(mover, x, y, block_type, cycles, hit_delay):
    """Place + break a block `cycles` times in a single thread.

    Strategy: send place and immediately start rapid punch hits in the same
    cycle without blocking on block_ack. This overlaps place-ack latency with
    the punch loop so each cycle is bounded by hit_delay * health only.
    """
    log.info("combo start tile=(%d,%d) block=%s cycles=%d hit_delay=%.0fms",
             x, y, block_type, cycles, hit_delay * 1000)
    # Number of hits needed = max health of the block type (LAVA=3, DIRT=3).
    hit_count = 3
    block_id = NAME_TO_ID.get(block_type, 0)
    t0 = time.monotonic()
    for c in range(1, cycles + 1):
        if stop.is_set():
            break
        tc = time.monotonic()
        # Ensure the tile is locally AIR so do_place does not reject.
        with world_lock:
            BLOCKS.pop(y * WORLD_W + x, None)
        do_place(mover, x, y, block_type)
        # Mark solid so the punch loop knows the block is present.
        if block_id:
            with world_lock:
                BLOCKS[y * WORLD_W + x] = block_id
        # Fire a fixed number of hits (covers the block's full health).
        for _ in range(hit_count):
            send(mover.ws, {"type": "expression", "expressionId": "punch"})
            send(mover.ws, {"type": "punch", "x": x, "y": y})
            send(mover.ws, {"type": "block_update", "opId": str(uuid.uuid4()),
                             "x": x, "y": y, "action": "break",
                             "blockType": "AIR",
                             "block": {"type": "AIR", "health": 0, "maxHealth": 0,
                                       "backgroundType": "CAVE_BG"},
                             "oldBlockType": block_type})
            time.sleep(hit_delay)
        # Clear locally so the next place is not rejected.
        with world_lock:
            BLOCKS.pop(y * WORLD_W + x, None)
        cycle_ms = (time.monotonic() - tc) * 1000
        log.info("combo cycle %d/%d: %d hits %.0f ms", c, cycles,
                 hit_count, cycle_ms)
    dt = time.monotonic() - t0
    log.info("combo done: %d cycles in %.2fs (%.0f ms/cycle)",
             cycles, dt, dt / max(cycles, 1) * 1000)


def do_mine(ws, x, y, delay, max_hits):
    log.info("mine start (%d,%d) type=%s delay=%.0fms", x, y,
             block_name_at(x, y), delay * 1000)
    t0 = time.monotonic()
    hits = 0
    for _ in range(max_hits):
        if stop.is_set():
            break
        if hits > 0 and BLOCKS.get(y * WORLD_W + x, 0) == 0:
            break
        old_type = block_name_at(x, y)
        send(ws, {"type": "expression", "expressionId": "punch"})
        send(ws, {"type": "punch", "x": x, "y": y})
        send(ws, {"type": "block_update", "opId": str(uuid.uuid4()), "x": x, "y": y,
                  "action": "break", "blockType": "AIR",
                  "block": {"type": "AIR", "health": 0, "maxHealth": 0,
                            "backgroundType": "CAVE_BG"},
                  "oldBlockType": old_type})
        hits += 1
        # Poll BLOCKS every 50 ms until the server's block_ack updates it;
        # exit fast without a fixed tail sleep once the block is gone.
        for _ in range(int(delay / 0.05) if delay > 0.05 else 1):
            if BLOCKS.get(y * WORLD_W + x, 0) == 0:
                break
            time.sleep(0.05)
    dt = time.monotonic() - t0
    broke = BLOCKS.get(y * WORLD_W + x, 0) == 0
    log.info("mine done (%d,%d): %s after %d hits in %.2fs (%.0fms/hit)",
             x, y, "BROKEN" if broke else "not broken", hits, dt,
             (dt / hits * 1000) if hits else 0)


def do_place(mover, x, y, block_type="DIRT", block=None):
    """Place one block after local safety checks; the server remains authoritative."""
    try:
        tx, ty = int(x), int(y)
    except (TypeError, ValueError):
        log.warning("place rejected: invalid tile (%r,%r)", x, y)
        return False
    if not connection_ready(mover):
        log.warning("place rejected: bot is not connected")
        return False
    if not valid_tile(tx, ty):
        log.warning("place rejected: tile out of bounds (%d,%d)", tx, ty)
        return False
    if not within_interaction_range(mover, tx, ty):
        log.warning("place rejected: tile (%d,%d) is out of range", tx, ty)
        return False

    idx = ty * WORLD_W + tx
    existing = BLOCKS.get(idx, 0)
    air_id = NAME_TO_ID.get("AIR", 0)
    if existing not in (0, air_id):
        log.warning("place rejected: tile (%d,%d) is occupied by %s",
                    tx, ty, NAME_BY_ID.get(existing, existing))
        return False

    block_type = str(block_type or "").strip().upper()
    if not block_type:
        log.warning("place rejected: empty block type")
        return False
    if NAME_TO_ID and block_type not in NAME_TO_ID:
        log.warning("place rejected: unknown block type %s", block_type)
        return False

    payload = dict(block or {})
    payload.setdefault("type", block_type)
    payload.setdefault("health", 3)
    payload.setdefault("maxHealth", payload["health"])
    payload.setdefault("backgroundType", "AIR")
    old_type = NAME_BY_ID.get(existing, "AIR")
    send(mover.ws, {
        "type": "block_update",
        "opId": str(uuid.uuid4()),
        "x": tx,
        "y": ty,
        "action": "place",
        "blockType": block_type,
        "block": payload,
        "oldBlockType": old_type,
    })
    log.info("place sent at tile (%d,%d) type=%s", tx, ty, block_type)
    return True


def do_chat(mover, text):
    if not connection_ready(mover):
        log.warning("msg rejected: bot is not connected")
        return False
    text = str(text or "")
    if not text.strip():
        log.warning("msg rejected: empty text")
        return False
    if len(text) > CHAT_MAX_LENGTH:
        log.warning("msg rejected: text exceeds %d characters", CHAT_MAX_LENGTH)
        return False
    send(mover.ws, {"type": "chat", "text": text})
    log.info("msg sent: %s", text[:80])
    return True


def do_drop(mover, item_type, count=1):
    """Drop an item from the authoritative inventory at the bot's feet."""
    if not connection_ready(mover):
        log.warning("drop rejected: bot is not connected")
        return False
    item_type = str(item_type or "").strip().upper()
    if not item_type:
        log.warning("drop rejected: empty item type")
        return False
    try:
        count = int(count)
    except (TypeError, ValueError):
        log.warning("drop rejected: invalid count=%r", count)
        return False
    if count < 1:
        log.warning("drop rejected: count must be >= 1")
        return False
    with world_lock:
        available = int(INVENTORY.get(item_type, 0))
    if available < count:
        log.warning("drop rejected: %s count=%d available=%d",
                    item_type, count, available)
        return False
    if item_type in NON_DROPPABLE_ITEMS:
        log.warning("drop rejected: %s is not droppable", item_type)
        return False

    offset = -20 if mover.facing_left else 20
    op_id = str(uuid.uuid4())
    send(mover.ws, {
        "type": "dropped_item",
        "opId": op_id,
        "itemType": item_type,
        "pos": {"x": round(mover.x + offset, 2), "y": round(mover.y, 2)},
        "count": count,
    })
    log.info("drop sent item=%s count=%d pos=(%.0f,%.0f) opId=%s",
             item_type, count, mover.x + offset, mover.y, op_id[:8])
    return True


def inventory_can_receive(item_type, count):
    item_type = str(item_type or "").upper()
    try:
        count = int(count)
    except (TypeError, ValueError):
        return False
    with world_lock:
        current = int(INVENTORY.get(item_type, 0))
        if current:
            return current + count <= 200
        used_slots = sum(
            1 for name, value in INVENTORY.items()
            if value > 0 and name not in NON_DROPPABLE_ITEMS
        )
        return used_slots < MAX_SLOTS


def _drop_distance(mover, item):
    pos = item.get("pos") or {}
    try:
        x, y = float(pos["x"]), float(pos["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return math.hypot(mover.x - x, mover.y - y)


def do_pickup(mover, drop_id=None, item_type=None, automatic=False):
    """Pick up a nearby ground item using the client pickup packet."""
    if not connection_ready(mover):
        log.warning("pickup rejected: bot is not connected")
        return False
    wanted_id = str(drop_id) if drop_id else None
    wanted_type = str(item_type or "").strip().upper() or None
    with world_lock:
        if wanted_id:
            candidates = [DROPPED_ITEMS.get(wanted_id)]
        else:
            candidates = list(DROPPED_ITEMS.values())
    candidates = [item for item in candidates if item]
    if wanted_type:
        candidates = [item for item in candidates
                      if str(item.get("itemType", "")).upper() == wanted_type]
    if not candidates:
        log.warning("pickup rejected: no matching ground item%s",
                    f" id={wanted_id}" if wanted_id else "")
        return False

    def distance_key(item):
        if wanted_id and str(item.get("id")) == wanted_id and \
                str(item.get("sourceUid") or "") != CURRENT_UID:
            return 0.0
        distance = _drop_distance(mover, item)
        return distance if distance is not None else float("inf")

    nearest = min(candidates, key=distance_key)
    distance = _drop_distance(mover, nearest)
    if distance is None:
        if not (wanted_id and str(nearest.get("id")) == wanted_id
                and str(nearest.get("sourceUid") or "") != CURRENT_UID):
            log.warning("pickup rejected: ground item has no position")
            return False
        distance = 0.0
    if distance > PICKUP_RANGE_PX and not (
            wanted_id and str(nearest.get("id")) == wanted_id
            and str(nearest.get("sourceUid") or "") != CURRENT_UID):
        log.warning("pickup rejected: item %s is %.1fpx away (max %.0f)",
                    nearest.get("id"), distance, PICKUP_RANGE_PX)
        return False
    item_name = str(nearest.get("itemType") or "").upper()
    count = int(nearest.get("count", 0) or 0)
    if not item_name or count < 1:
        log.warning("pickup rejected: invalid ground item %s", nearest.get("id"))
        return False
    if not inventory_can_receive(item_name, count):
        log.warning("pickup rejected: inventory full for %s x%d", item_name, count)
        return False

    op_id = str(uuid.uuid4())
    with world_lock:
        PICKUP_OPS[op_id] = str(nearest["id"])
    send(mover.ws, {"type": "pickup_item", "opId": op_id, "id": nearest["id"]})
    log.info("%spickup sent id=%s item=%s count=%d distance=%.1fpx sourceUid=%s CURRENT_UID=%s opId=%s",
             "auto " if automatic else "", nearest["id"], item_name, count,
             distance, nearest.get("sourceUid"), CURRENT_UID, op_id[:8])
    return True


def maybe_auto_pickup(mover, drop_id):
    if not AUTO_COLLECT_NEARBY:
        return
    threading.Thread(target=do_pickup, args=(mover, drop_id),
                     kwargs={"automatic": True}, daemon=True).start()


def do_jump(mover, dx=0.0, stop_dy=None):
    if mover.busy:
        return
    mover.busy = True
    try:
        vx = int(round(dx / 3.0)) if dx else 0
        flags_dir = (2 if dx < 0 else 4) if dx else 0
        start_y = mover.y
        target_y = None if stop_dy is None else start_y + stop_dy
        log.info("jump start (%.1f,%.1f) dx=%.1f stop=%s", mover.x, mover.y,
                 dx, target_y)
        for i, (dy, vy) in enumerate(JUMP_ARC):
            mover.x += dx
            mover.y += dy
            if target_y is not None and dy > 0 and mover.y >= target_y:
                mover.y = target_y
                with lock:
                    mover.ws.send(mover.packet(vx, 0, 0x08 | flags_dir))
                break
            with lock:
                mover.ws.send(mover.packet(vx, vy, 0x08 | flags_dir))
            time.sleep(JUMP_PACKET_SEC)
        mover.need_send = True
        log.info("jump done (%.1f,%.1f)", mover.x, mover.y)
    except Exception as e:
        log.warning("jump failed: %s", e)
    finally:
        mover.busy = False


def do_air_goto(mover, x, y, units="tile", speed=AIR_GOTO_SPEED_PX):
    """Experimental free-air movement; abort immediately on server correction."""
    if mover.busy:
        log.warning("air_goto rejected: mover is busy")
        return False
    if not connection_ready(mover):
        log.warning("air_goto rejected: bot is not connected")
        return False
    try:
        units = str(units or "tile").strip().lower()
        if units in ("tile", "tiles"):
            tx, ty = int(x), int(y)
            if not valid_tile(tx, ty):
                log.warning("air_goto rejected: invalid tile (%r,%r)", x, y)
                return False
            target_x = tx * TILE + TILE / 2 - PLAYER_W / 2
            target_y = ty * TILE + TILE / 2 - PLAYER_H / 2
        elif units in ("px", "pixel", "pixels"):
            target_x, target_y = float(x), float(y)
        else:
            log.warning("air_goto rejected: unknown units %s", units)
            return False
        target_x, target_y = float(target_x), float(target_y)
        if not (0 <= target_x <= WORLD_W * TILE - PLAYER_W and
                0 <= target_y <= WORLD_H * TILE - PLAYER_H):
            log.warning("air_goto rejected: target out of bounds (%.1f,%.1f)",
                        target_x, target_y)
            return False
        speed = min(max(float(speed), 0.5), AIR_GOTO_MAX_SPEED_PX)
    except (TypeError, ValueError):
        log.warning("air_goto rejected: invalid target (%r,%r)", x, y)
        return False

    mover.busy = True
    mover.target_x = None
    mover.target_y = None
    corrections0 = mover.corrections
    tick = 1.0 / TICK_HZ
    next_t = time.monotonic()
    steps = 0
    log.info("air_goto start (%.1f,%.1f) -> (%.1f,%.1f) units=%s speed=%.1f",
             mover.x, mover.y, target_x, target_y, units, speed)
    try:
        while mover.alive and not stop.is_set():
            if mover.corrections != corrections0:
                log.warning("air_goto aborted: server corrected position")
                return False
            if not connection_ready(mover):
                log.warning("air_goto aborted: bot is not connected")
                return False
            dx, dy = target_x - mover.x, target_y - mover.y
            distance = math.hypot(dx, dy)
            if distance <= 0.5:
                log.info("air_goto done at (%.1f,%.1f) in %d steps",
                         mover.x, mover.y, steps)
                return True

            scale = min(1.0, speed / distance)
            nx, ny = mover.x + dx * scale, mover.y + dy * scale
            vx, vy = nx - mover.x, ny - mover.y
            flags = 0
            if vx < -0.5:
                flags |= 1 | 2
            elif vx > 0.5:
                flags |= 4
            if vy < -0.5:
                flags |= 8
            elif vy > 0.5:
                flags |= 16
            with lock:
                mover.x, mover.y = nx, ny
                mover.ws.send(mover.packet(vx, vy, flags))
            steps += 1
            if mover.corrections != corrections0:
                log.warning("air_goto aborted after send: server corrected position")
                return False
            next_t += tick
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()
        return False
    except Exception as e:
        log.warning("air_goto failed: %s", e)
        return False
    finally:
        mover.target_x = None
        mover.target_y = None
        mover.need_send = True
        mover.busy = False


class FarmManager:
    """Automated LAVA farming loop with reconnect-resilient state machine.

    Stages (in order):
        IDLE → DROPPING_SEEDS → BREAKING → COLLECTING_BREAK →
        PLANTING → WAITING_GROWTH → HARVESTING → RETURN_HOME → (loop)

    State survives reconnect because farm_manager is a module-level global.
    On re-join, run_session() calls farm_manager.on_reconnect(mover) which
    navigates the bot back to current_target_tile then resumes the loop.
    """

    FARM_ITEMS = frozenset({"LAVA", "LAVA_SEED"})
    HOME_TILE  = (10, 24)
    BREAK_TILE = (9, 24)
    PLANT_START = 11
    PLANT_END   = 50
    DROP_ZONE_START = 1
    DROP_ZONE_END   = 40
    DROP_MAX_PER_SLOT = 1500
    SEED_OVERFLOW_THRESHOLD = 200
    BREAK_HIT_DELAY = 0.13
    BREAK_HITS = 3
    GROW_TIME = 33.0  # 31s grow + 2s buffer

    def __init__(self):
        self._farm_stop = threading.Event()
        self.active = False
        self.stage = "IDLE"
        self.current_target_tile = self.HOME_TILE
        self.plant_index = self.PLANT_START
        self.harvest_index = self.PLANT_START
        self.planted_trees = []
        self.growth_ready_time = 0.0
        self.seed_overflow_index = self.DROP_ZONE_START
        self.stats = {"cycles": 0, "lava_broken": 0, "trees_harvested": 0}

    def is_active(self):
        return self.active

    def request_stop(self):
        self._farm_stop.set()
        log.info("farm: stop requested")

    def status_dict(self):
        return {
            "active": self.active,
            "stage": self.stage,
            "target": list(self.current_target_tile),
            "plantIndex": self.plant_index,
            "harvestIndex": self.harvest_index,
            "seedOverflowIndex": self.seed_overflow_index,
            "cycles": self.stats["cycles"],
            "lavaInventory": INVENTORY.get("LAVA", 0),
            "seedInventory": INVENTORY.get("LAVA_SEED", 0),
            "treesHarvested": self.stats["trees_harvested"],
        }

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def start(self, mover):
        """Start farming from scratch (called from poll_commands thread)."""
        if self.active:
            log.warning("farm: already active, ignoring start")
            return
        self._farm_stop.clear()
        self.active = True
        self.stage = "DROPPING_SEEDS"
        self.plant_index = self.PLANT_START
        self.harvest_index = self.PLANT_START
        self.planted_trees = []
        self.growth_ready_time = 0.0
        log.info("farm: starting loop")
        threading.Thread(target=self._main_loop, args=(mover,), daemon=True).start()

    def on_reconnect(self, mover):
        """Called after world_joined when farm was active. Resumes from last tile."""
        if not self.active:
            return
        self._farm_stop.clear()
        log.info("farm: reconnected — resuming stage=%s target=%s",
                 self.stage, self.current_target_tile)
        def _resume():
            do_goto_tile(mover, *self.current_target_tile)
            if not self._farm_stop.is_set() and not stop.is_set():
                self._main_loop(mover)
        threading.Thread(target=_resume, daemon=True).start()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _main_loop(self, mover):
        try:
            while not self._farm_stop.is_set() and not stop.is_set():
                s = self.stage
                if s == "DROPPING_SEEDS":
                    self._stage_dropping_seeds(mover)
                elif s == "BREAKING":
                    self._stage_breaking(mover)
                elif s == "COLLECTING_BREAK":
                    self._stage_collecting(mover, area_tx=self.BREAK_TILE[0])
                elif s == "PLANTING":
                    self._stage_planting(mover)
                elif s == "WAITING_GROWTH":
                    self._stage_waiting(mover)
                elif s == "HARVESTING":
                    self._stage_harvesting(mover)
                elif s == "RETURN_HOME":
                    self._stage_return_home(mover)
                else:
                    log.warning("farm: unknown stage %r, stopping", s)
                    break
        except Exception as e:
            log.warning("farm: loop error: %s", e)
        finally:
            self.active = False
            self.stage = "IDLE"
            log.info("farm: loop ended; stats=%s", self.stats)

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage_dropping_seeds(self, mover):
        """Dump excess LAVA_SEED (≥200) to the drop zone (1..40, 26)."""
        with world_lock:
            seed_count = int(INVENTORY.get("LAVA_SEED", 0))
        if seed_count < self.SEED_OVERFLOW_THRESHOLD:
            log.info("farm: DROPPING_SEEDS — seeds=%d < threshold, skip", seed_count)
            self.stage = "BREAKING"
            return

        log.info("farm: DROPPING_SEEDS — seeds=%d, overflow zone x=%d",
                 seed_count, self.seed_overflow_index)

        while not self._farm_stop.is_set() and not stop.is_set():
            with world_lock:
                seed_count = int(INVENTORY.get("LAVA_SEED", 0))
            if seed_count < self.SEED_OVERFLOW_THRESHOLD:
                break
            if self.seed_overflow_index > self.DROP_ZONE_END:
                log.warning("farm: drop zone full (x>40), seed overflow not resolved")
                break

            tx = self.seed_overflow_index
            # Bot stands one tile to the right of drop target, on y=24 platform.
            stand_tx = tx + 1
            self.current_target_tile = (stand_tx, 24)
            do_goto_tile(mover, stand_tx, 24)
            if self._farm_stop.is_set() or stop.is_set():
                return

            # Estimate how many seeds already exist at this x coordinate.
            with world_lock:
                existing = sum(
                    int(item.get("count", 0))
                    for item in DROPPED_ITEMS.values()
                    if item.get("itemType") == "LAVA_SEED"
                    and abs(float((item.get("pos") or {}).get("x", -9999)) - tx * TILE) < 25
                )
            slot_remaining = self.DROP_MAX_PER_SLOT - existing
            if slot_remaining <= 0:
                log.info("farm: drop slot x=%d full (%d), advance", tx, existing)
                self.seed_overflow_index += 1
                continue

            # Keep at least 199 seeds in inventory for planting.
            to_drop = min(seed_count - (self.SEED_OVERFLOW_THRESHOLD - 1), slot_remaining)
            if to_drop <= 0:
                break

            # Face left so do_drop places items at (mover.x - 20), closer to tx.
            mover.facing_left = True
            ok = do_drop(mover, "LAVA_SEED", to_drop)
            if ok:
                log.info("farm: dropped LAVA_SEED x%d at x=%d zone", to_drop, tx)
            stop.wait(0.5)  # allow drop_ack to update inventory

            # If still overflowing, try advancing slot.
            with world_lock:
                seed_count = int(INVENTORY.get("LAVA_SEED", 0))
            if seed_count >= self.SEED_OVERFLOW_THRESHOLD:
                self.seed_overflow_index += 1

        mover.facing_left = False
        self.stage = "BREAKING"

    def _stage_breaking(self, mover):
        """Place LAVA at (9,24) and punch it until inventory LAVA == 0.

        Uses do_combo (proven to work) for each chunk of breaking.
        do_combo handles place+punch interleaving correctly; it does not
        block on block_ack, so rapid successive places are not rejected.
        """
        self.current_target_tile = self.HOME_TILE
        do_goto_tile(mover, *self.HOME_TILE)
        if self._farm_stop.is_set() or stop.is_set():
            return

        log.info("farm: BREAKING — lava in inventory=%d", INVENTORY.get("LAVA", 0))

        # If no LAVA in inventory, wait (idle) until some appears (e.g. from harvest).
        # Poll every 2 s; the outer loop in _main_loop will re-enter this stage.
        while not self._farm_stop.is_set() and not stop.is_set():
            with world_lock:
                lava_count = int(INVENTORY.get("LAVA", 0))
            if lava_count <= 0:
                log.info("farm: BREAKING — no lava in inventory, waiting...")
                stop.wait(2.0)
                continue
            chunks = min(lava_count, 20)
            do_combo(mover, *self.BREAK_TILE, "LAVA", chunks, self.BREAK_HIT_DELAY)
            self.stats["lava_broken"] += chunks
            if self._farm_stop.is_set() or stop.is_set():
                break

        log.info("farm: BREAKING done, lava remaining=%d", INVENTORY.get("LAVA", 0))
        self.stage = "COLLECTING_BREAK"

    def _stage_collecting(self, mover, area_tx):
        """Pick up LAVA and LAVA_SEED near area_tx, skip GEMS and other items."""
        log.info("farm: COLLECTING near tx=%d", area_tx)
        area_px_lo = area_tx * TILE - 48
        area_px_hi = area_tx * TILE + 80

        # Gather candidate drop IDs (snapshot to avoid dict-changes during iteration).
        collected = 0
        for attempt in range(30):  # max 30 rounds
            if self._farm_stop.is_set() or stop.is_set():
                break
            with world_lock:
                candidates = [
                    item for item in DROPPED_ITEMS.values()
                    if item.get("itemType") in self.FARM_ITEMS
                    and area_px_lo <= float((item.get("pos") or {}).get("x", -9999)) <= area_px_hi
                ]
            if not candidates:
                break
            for item in candidates:
                if self._farm_stop.is_set() or stop.is_set():
                    break
                ok = do_pickup(mover, drop_id=item["id"])
                if ok:
                    collected += 1
            stop.wait(0.3)

        log.info("farm: COLLECTING done — picked %d drops", collected)
        # Determine next stage.
        with world_lock:
            seed_count = int(INVENTORY.get("LAVA_SEED", 0))
        if area_tx == self.BREAK_TILE[0]:
            # After break-area collection: check overflow then plant or wait.
            if seed_count >= self.SEED_OVERFLOW_THRESHOLD:
                self.stage = "DROPPING_SEEDS"
            elif seed_count > 0 or self.planted_trees:
                self.stage = "PLANTING"
            else:
                self.stage = "BREAKING"
        else:
            # After harvest-area collection: return home.
            self.stage = "RETURN_HOME"

    def _stage_planting(self, mover):
        """Plant LAVA_SEED from plant_index..PLANT_END in empty tiles."""
        log.info("farm: PLANTING from tx=%d, seeds=%d",
                 self.plant_index, INVENTORY.get("LAVA_SEED", 0))

        for tx in range(self.plant_index, self.PLANT_END + 1):
            if self._farm_stop.is_set() or stop.is_set():
                return
            with world_lock:
                seed_count = int(INVENTORY.get("LAVA_SEED", 0))
                tile_occupied = BLOCKS.get(24 * WORLD_W + tx, 0) != 0
            if seed_count <= 0:
                break
            if tile_occupied:
                self.plant_index = tx + 1
                continue

            self.current_target_tile = (tx, 24)
            do_goto_tile(mover, tx, 24)
            if self._farm_stop.is_set() or stop.is_set():
                return

            ok = do_place(mover, tx, 24, "LAVA_SEED")
            if ok:
                if (tx, 24) not in self.planted_trees:
                    self.planted_trees.append((tx, 24))
                self.growth_ready_time = time.time() + self.GROW_TIME
                log.info("farm: planted LAVA_SEED at (%d,24), grow_ready in %.0fs",
                         tx, self.GROW_TIME)
            self.plant_index = tx + 1

            with world_lock:
                seed_count = int(INVENTORY.get("LAVA_SEED", 0))
            if seed_count <= 0:
                break

        log.info("farm: PLANTING done — planted_trees=%d, plant_index=%d",
                 len(self.planted_trees), self.plant_index)
        if self.planted_trees:
            self.stage = "WAITING_GROWTH"
        else:
            self.stage = "BREAKING"

    def _stage_waiting(self, mover):
        """Wait at HOME_TILE until growth_ready_time."""
        self.current_target_tile = self.HOME_TILE
        do_goto_tile(mover, *self.HOME_TILE)

        remaining = self.growth_ready_time - time.time()
        log.info("farm: WAITING_GROWTH — %.0fs remaining", max(remaining, 0))

        while not self._farm_stop.is_set() and not stop.is_set():
            remaining = self.growth_ready_time - time.time()
            if remaining <= 0:
                break
            stop.wait(min(remaining, 1.0))

        log.info("farm: WAITING_GROWTH done")
        self.harvest_index = self.PLANT_START
        self.stage = "HARVESTING"

    def _stage_harvesting(self, mover):
        """Punch each planted tree and collect drops."""
        log.info("farm: HARVESTING %d planted trees from tx=%d",
                 len(self.planted_trees), self.harvest_index)

        # Build list of trees to harvest (planted_trees or scan BLOCKS for TREE).
        tree_id = NAME_TO_ID.get("TREE", 0)
        trees_to_harvest = []
        for tx in range(self.PLANT_START, self.PLANT_END + 1):
            with world_lock:
                bid = BLOCKS.get(24 * WORLD_W + tx, 0)
            name = NAME_BY_ID.get(bid, "")
            if "TREE" in name or bid == tree_id:
                trees_to_harvest.append(tx)
        if not trees_to_harvest and self.planted_trees:
            trees_to_harvest = [t[0] for t in self.planted_trees]
        trees_to_harvest = [tx for tx in trees_to_harvest if tx >= self.harvest_index]
        trees_to_harvest.sort()

        for tx in trees_to_harvest:
            if self._farm_stop.is_set() or stop.is_set():
                return
            self.harvest_index = tx
            self.current_target_tile = (tx, 24)
            do_goto_tile(mover, tx, 24)
            if self._farm_stop.is_set() or stop.is_set():
                return

            log.info("farm: harvesting tree at (%d,24)", tx)
            do_mine(mover.ws, tx, 24, delay=0.15, max_hits=3)
            stop.wait(0.4)  # Wait for drop packets.

            # Collect drops near this tile (LAVA and LAVA_SEED only).
            area_px_lo = tx * TILE - 48
            area_px_hi = tx * TILE + 80
            for _ in range(10):
                if self._farm_stop.is_set() or stop.is_set():
                    break
                with world_lock:
                    nearby = [
                        item for item in DROPPED_ITEMS.values()
                        if item.get("itemType") in self.FARM_ITEMS
                        and area_px_lo <= float((item.get("pos") or {}).get("x", -9999)) <= area_px_hi
                    ]
                if not nearby:
                    break
                for item in nearby:
                    do_pickup(mover, drop_id=item["id"])
                stop.wait(0.3)

            self.stats["trees_harvested"] += 1
            self.harvest_index = tx + 1

        # Reset for next cycle.
        self.planted_trees = []
        self.plant_index = self.PLANT_START
        self.harvest_index = self.PLANT_START
        log.info("farm: HARVESTING done — trees_harvested total=%d",
                 self.stats["trees_harvested"])
        self.stage = "RETURN_HOME"

    def _stage_return_home(self, mover):
        """Return to HOME_TILE, increment cycle counter, decide next stage."""
        self.current_target_tile = self.HOME_TILE
        do_goto_tile(mover, *self.HOME_TILE)
        self.stats["cycles"] += 1
        log.info("farm: RETURN_HOME — cycle %d complete", self.stats["cycles"])
        # Decide next stage based on inventory state.
        with world_lock:
            seed_count = int(INVENTORY.get("LAVA_SEED", 0))
        if seed_count >= self.SEED_OVERFLOW_THRESHOLD:
            self.stage = "DROPPING_SEEDS"
        else:
            self.stage = "BREAKING"


farm_manager = FarmManager()


def poll_commands(mover):
    if not os.path.exists(CMD_FILE):
        return
    try:
        lines = [l.strip() for l in open(CMD_FILE) if l.strip()]
        os.remove(CMD_FILE)
    except Exception as e:
        log.warning("cmd read failed: %s", e)
        return
    for line in lines:
        try:
            cmd = json.loads(line)
        except Exception as e:
            log.warning("bad cmd %s: %s", line, e)
            continue
        kind = cmd.get("cmd")
        if kind == "move":
            mover.set_target(float(cmd.get("dx", 0)))
        elif kind in ("goto", "walkto"):
            mover.set_target(float(cmd["x"]) - mover.x)
        elif kind == "walkto_tile":
            target = float(cmd["x"]) * 32 + 16 - 11
            mover.set_target(target - mover.x)
        elif kind == "goto_tile":
            threading.Thread(target=do_goto_tile,
                             args=(mover, int(cmd["x"]), int(cmd["y"])),
                             daemon=True).start()
        elif kind == "air_goto":
            threading.Thread(
                target=do_air_goto,
                args=(mover, cmd.get("x"), cmd.get("y"),
                      cmd.get("units", "tile"),
                      cmd.get("speed", AIR_GOTO_SPEED_PX)),
                daemon=True,
            ).start()
        elif kind == "player":
            if not PLAYERS:
                log.warning("player: no tracked players")
            for p in PLAYERS.values():
                log.info("player %s session=%s pos=(%.0f,%.0f) tile %s",
                         p["name"], p["session"], p["x"], p["y"],
                         tile_at(p["x"], p["y"]))
        elif kind in ("goto_player", "follow"):
            name = str(cmd.get("name", "")).strip().lower()
            match = None
            for p in PLAYERS.values():
                if not name or p["name"].lower() == name:
                    match = p
                    break
            if match is None:
                log.warning("goto_player: %r not tracked", name)
                continue
            target = tile_at(match["x"], match["y"])
            log.info("goto_player %s pos=(%.0f,%.0f) tile %s", match["name"],
                     match["x"], match["y"], target)
            threading.Thread(target=do_goto_tile, args=(mover, *target),
                             daemon=True).start()
        elif kind == "map":
            x0 = int(cmd.get("x", 0))
            y0 = int(cmd.get("y", 0))
            w = int(cmd.get("w", 20))
            h = int(cmd.get("h", 10))
            log.info("map x=%d..%d y=%d..%d", x0, x0 + w - 1, y0, y0 + h - 1)
            for ty in range(y0, y0 + h):
                log.info("map y=%d %s", ty, " ".join(
                    str(BLOCKS.get(ty * WORLD_W + tx, 0))
                    for tx in range(x0, x0 + w)))
        elif kind == "path":
            start = tile_at(mover.x, mover.y)
            goal = (int(cmd["x"]), int(cmd["y"]))
            t0 = time.monotonic()
            p = find_path(start, goal)
            log.info("path %s -> %s: %s in %.2fs", start, goal,
                     "None" if p is None else "%d edges" % len(p),
                     time.monotonic() - t0)
            for direction, k, dest in (p or []):
                log.info("  %s dir=%+d -> %s", k, direction, dest)
        elif kind == "simjump":
            src = (int(cmd["x"]), int(cmd["y"]))
            dst = (int(cmd["tx"]), int(cmd["ty"]))
            sx, sy = stand_pos(*src)
            dest, _ = jump_aim_steps(sx, sy, stand_pos(*dst)[0] + 5)
            log.info("simjump %s -> %s: landed %s", src, dst, dest)
        elif kind == "speed":
            mover.max_speed = float(cmd.get("value", MAX_SPEED_TICK))
            log.info("max speed set to %.1f px/tick", mover.max_speed)
        elif kind == "stop":
            mover.target_x = None
            mover.target_y = None
            log.info("stopped at (%.1f,%.1f)", mover.x, mover.y)
        elif kind == "fly":
            mover.set_target_y(float(cmd.get("dy", 0)))
        elif kind == "jump":
            threading.Thread(target=do_jump,
                             args=(mover, float(cmd.get("dx", 0)),
                                   None if "stop_dy" not in cmd else float(cmd["stop_dy"])),
                             daemon=True).start()
        elif kind == "tp":
            tx = float(cmd["x"]) if "x" in cmd else mover.x + float(cmd.get("dx", 0))
            ty = float(cmd["y"]) if "y" in cmd else mover.y + float(cmd.get("dy", 0))
            mover.tp(tx, ty)
        elif kind == "pos":
            log.info("bot pos (%.1f,%.1f) target=(%s,%s)", mover.x, mover.y,
                     mover.target_x, mover.target_y)
        elif kind in ("say", "msg"):
            do_chat(mover, cmd.get("text", cmd.get("message", "")))
        elif kind in ("drop", "drop_item"):
            do_drop(mover, cmd.get("itemType", cmd.get("type", "")),
                    cmd.get("count", 1))
        elif kind in ("pickup", "collect"):
            do_pickup(mover, cmd.get("id"), cmd.get("itemType"))
        elif kind == "place":
            do_place(mover, cmd.get("x"), cmd.get("y"),
                     cmd.get("blockType", cmd.get("type", "DIRT")),
                     cmd.get("block"))
        elif kind == "punch":
            threading.Thread(target=do_punch,
                             args=(mover.ws, int(cmd["x"]), int(cmd["y"]),
                                   int(cmd.get("count", 1)),
                                   float(cmd.get("delay", 0.16)),
                                   str(cmd.get("oldType", "DIRT"))),
                             daemon=True).start()
        elif kind == "mine":
            threading.Thread(target=do_mine,
                             args=(mover.ws, int(cmd["x"]), int(cmd["y"]),
                                   float(cmd.get("delay", 0.16)),
                                   int(cmd.get("max", 30))),
                             daemon=True).start()
        elif kind in ("combo", "loop"):
            threading.Thread(target=do_combo,
                             args=(mover,
                                   int(cmd["x"]), int(cmd["y"]),
                                   str(cmd.get("blockType",
                                               cmd.get("type", "LAVA"))),
                                   int(cmd.get("cycles", 5)),
                                   float(cmd.get("hit_delay", 0.13))),
                             daemon=True).start()
        elif kind in ("farm", "farm_start", "farm_stop", "farm_status"):
            action = cmd.get("action", "status")
            if kind == "farm_start":
                action = "start"
            elif kind == "farm_stop":
                action = "stop"
            elif kind == "farm_status":
                action = "status"
            if action == "start":
                farm_manager.start(mover)
            elif action == "stop":
                farm_manager.request_stop()
            else:
                log.info("farm status: %s", farm_manager.status_dict())
        elif kind in ("captcha", "captcha_info"):
            if CAPTCHA:
                last = next(iter(CAPTCHA.values()), {})
                log.info("captcha info: action=%s provider=%s siteKey=%s challengeId=%s cData=%s",
                         last.get("action"), last.get("provider"),
                         last.get("siteKey"), last.get("challengeId"),
                         last.get("cData"))
            else:
                log.info("captcha: no active challenge")
        elif kind == "captcha_resolve":
            htoken = str(cmd.get("token", ""))
            if not htoken:
                log.warning("captcha_resolve: no token provided")
            # CAPTCHA now holds only the latest challenge (single entry).
            # The server rotates challengeId every ~30s; the dashboard's
            # challengeId may be stale. Always use the live one so the
            # session token and challenge match the server's expectation.
            live = next(iter(CAPTCHA.values()), {}) if CAPTCHA else {}
            live_challenge_id = live.get("challengeId", "") or str(cmd.get("challengeId", ""))
            live_session_token = str(live.get("token", ""))
            live_cdata = str(live.get("cData", ""))
            if live_challenge_id and str(cmd.get("challengeId", "")) and \
                    live_challenge_id != str(cmd.get("challengeId", "")):
                log.info("captcha_resolve: cmd challengeId=%s stale, using live=%s",
                         str(cmd.get("challengeId", ""))[:16], live_challenge_id[:16])
            packet = {
                "type": "captcha_resolved",
                "provider": "hcaptcha",
                "challengeId": live_challenge_id,
                "cData": live_cdata,
                "response": htoken,
            }
            if live_session_token:
                packet["token"] = live_session_token
            log.info("captcha_resolve: sending packet=%s", json.dumps(packet, default=str)[:400])
            send(mover.ws, packet)
            log.info("captcha_resolve sent challengeId=%s sessionToken=%s response=%s",
                     live_challenge_id[:16], live_session_token[:16] if live_session_token else "(none)",
                     htoken[:20])
        else:
            log.warning("unknown cmd: %s", cmd)


def run_session():
    global BOT_NAME
    token = refresh_token()
    master, route, name = connect_master(token)
    BOT_NAME = name
    worker, joined = connect_worker(route)
    mover = Mover(worker, joined.get("spawnX", 0), joined.get("spawnY", 0))
    write_state(mover, "online")
    th = threading.Thread(target=reader, args=(worker, mover), daemon=True)
    th.start()
    # real client waits ~4s after world_joined (snapshot ingest) before first position packet
    stop.wait(4.0)
    mover_thread = threading.Thread(target=mover.loop, daemon=True)
    mover_thread.start()

    # If farm was active before this reconnect, resume it at last known tile.
    if farm_manager.is_active():
        farm_manager.on_reconnect(mover)

    last_refresh = time.time()
    next_beat = 0.0
    next_state = 0.0
    while not stop.is_set():
        if not th.is_alive() or not mover_thread.is_alive():
            log.warning("connection thread died, reconnecting")
            break
        now = time.time()
        if now - last_refresh > 3000:
            try:
                refresh_token()
                last_refresh = time.time()
            except Exception as e:
                log.warning("token refresh failed: %s", e)
        if now >= next_beat:
            try:
                send(worker, {"type": "heartbeat"})
            except Exception as e:
                log.warning("heartbeat send failed: %s", e)
                break
            next_beat = now + HEARTBEAT_SEC
        if now >= next_state:
            write_state(mover, "online")
            next_state = now + 0.25
        poll_commands(mover)
        stop.wait(0.1)

    mover.alive = False
    write_state(mover, "offline")
    try:
        worker.close()
    except Exception:
        pass
    try:
        master.close()
    except Exception:
        pass


def main():
    write_state(None, "offline")
    backoff = 5
    while not stop.is_set():
        try:
            run_session()
            backoff = 5
        except Exception as e:
            log.warning("session error: %s", e)
        if stop.is_set():
            break
        log.info("reconnecting in %ss", backoff)
        stop.wait(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop.set()
