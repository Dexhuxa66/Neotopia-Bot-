#!/usr/bin/env python3
"""Small REST/WebSocket control plane for one or more Neotopia bots.

Each managed bot keeps the existing stay_world.py command protocol. The
dashboard owns bot records and starts one isolated worker process per account;
this keeps the legacy single-bot behavior intact while allowing multiple
accounts to be controlled from one server.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
BOT_SCRIPT = BASE_DIR / "stay_world.py"
STATIC_DIR = BASE_DIR / "dashboard"
DATA_DIR = Path(os.environ.get("NEO_DASHBOARD_DIR", BASE_DIR / "dashboard_data"))
REGISTRY_FILE = DATA_DIR / "bots.json"
DEFAULT_ACCOUNT = BASE_DIR / "guest_account.json"
DEFAULT_CMD = BASE_DIR / "stay_cmd.txt"
DEFAULT_STATE = BASE_DIR / "bot_state.json"
DEFAULT_LOG = BASE_DIR / "stay_world.log"
DEFAULT_WORLD = os.environ.get("NEO_WORLD", "778839")
FIREBASE_API_KEY_ENV = "NEO_FIREBASE_API_KEY"

DATA_DIR.mkdir(parents=True, exist_ok=True)


def atomic_json_write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temp.open("w") as fh:
        json.dump(payload, fh, indent=2)
    temp.replace(path)


def read_json(path: Path, default=None):
    try:
        with path.open() as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def firebase_api_key():
    value = os.environ.get(FIREBASE_API_KEY_ENV)
    if value:
        return value
    account = read_json(DEFAULT_ACCOUNT, {}) or {}
    return account.get("api_key")


def create_guest_account(api_key):
    request = urllib.request.Request(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}",
        data=json.dumps({"returnSecureToken": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    account = {
        "api_key": api_key,
        "id_token": result["idToken"],
        "refresh_token": result["refreshToken"],
        "uid": result["localId"],
    }
    return account


class BotManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen] = {}
        self.load()

    def load(self):
        with self.lock:
            data = read_json(REGISTRY_FILE, {"bots": []}) or {"bots": []}
            self.bots = {str(item["id"]): item for item in data.get("bots", [])}
            if "default" not in self.bots and DEFAULT_ACCOUNT.exists():
                self.bots["default"] = {
                    "id": "default",
                    "name": "Guest_FABQQ",
                    "world": DEFAULT_WORLD,
                    "account_file": str(DEFAULT_ACCOUNT),
                    "cmd_file": str(DEFAULT_CMD),
                    "state_file": str(DEFAULT_STATE),
                    "log_file": str(DEFAULT_LOG),
                    "managed": False,
                }
                self.save()

    def save(self):
        atomic_json_write(REGISTRY_FILE, {"bots": list(self.bots.values())})

    def _record(self, bot_id):
        record = self.bots.get(str(bot_id))
        if not record:
            raise HTTPException(status_code=404, detail="bot not found")
        return record

    def _state(self, record):
        return read_json(Path(record["state_file"]), {}) or {}

    def _managed_process(self, bot_id):
        process = self.processes.get(bot_id)
        if process is not None and process.poll() is not None:
            self.processes.pop(bot_id, None)
            process = None
        return process

    def _external_process_alive(self, record):
        if record.get("managed", True):
            return False
        for entry in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                command = entry.read_bytes().replace(b"\0", b" ").decode(errors="ignore")
            except OSError:
                continue
            if "stay_world.py" in command and str(os.getpid()) not in command:
                return True
        return False

    def public_record(self, record):
        with self.lock:
            state = self._state(record)
            process = self._managed_process(record["id"])
            running = process is not None or self._external_process_alive(record)
            state_status = state.get("status")
            status = "online" if running and state_status in (None, "online") else (
                "starting" if running else "offline"
            )
            position = state.get("position")
            if position is None and "x" in state and "y" in state:
                position = {"x": state["x"], "y": state["y"]}
            return {
                "id": record["id"],
                "name": state.get("name") or record.get("name") or record["id"],
                "status": status,
                "world": state.get("world", record.get("world", DEFAULT_WORLD)),
                "position": position,
                "updatedAt": state.get("updatedAt"),
                "managed": bool(record.get("managed", True)),
            }

    def list_public(self):
        with self.lock:
            return [self.public_record(record) for record in self.bots.values()]

    def world(self, bot_id):
        with self.lock:
            record = self._record(bot_id)
            state = self._state(record)
            if not state:
                state = {
                    "botId": record["id"],
                    "name": record.get("name", record["id"]),
                    "status": self.public_record(record)["status"],
                    "world": record.get("world", DEFAULT_WORLD),
                    "position": None,
                    "worldSize": {"width": 100, "height": 60},
                    "blocks": {},
                    "blockNames": {},
                    "inventory": [],
                    "gems": 0,
                    "inventoryVersion": 0,
                    "maxSlots": 10,
                    "droppedItems": [],
                    "players": [],
                }
            state["status"] = self.public_record(record)["status"]
            return state

    def generate(self):
        api_key = firebase_api_key()
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail=f"missing Firebase API key; set {FIREBASE_API_KEY_ENV}",
            )
        try:
            account = create_guest_account(api_key)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"guest generation failed: {exc}") from exc

        bot_id = account["uid"]
        bot_dir = DATA_DIR / bot_id
        account_file = bot_dir / "guest_account.json"
        record = {
            "id": bot_id,
            "name": f"Guest_{bot_id[-6:]}",
            "world": DEFAULT_WORLD,
            "account_file": str(account_file),
            "cmd_file": str(bot_dir / "stay_cmd.txt"),
            "state_file": str(bot_dir / "bot_state.json"),
            "log_file": str(bot_dir / "stay_world.log"),
            "managed": True,
        }
        with self.lock:
            atomic_json_write(account_file, account)
            self.bots[bot_id] = record
            self.save()
        return self.public_record(record)

    def start(self, bot_id):
        with self.lock:
            record = self._record(bot_id)
            if self._managed_process(record["id"]):
                return self.public_record(record)
            account_file = Path(record["account_file"])
            if not account_file.exists():
                raise HTTPException(status_code=400, detail="bot account file is missing")
            Path(record["cmd_file"]).parent.mkdir(parents=True, exist_ok=True)
            Path(record["log_file"]).parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(record["log_file"], "a")
            env = os.environ.copy()
            env.update({
                "NEO_ACCOUNT": str(account_file),
                "NEO_CMD_FILE": record["cmd_file"],
                "NEO_STATE_FILE": record["state_file"],
                "NEO_LOG_FILE": record["log_file"],
                "NEO_BOT_ID": record["id"],
                "NEO_BOT_NAME": record.get("name", record["id"]),
                "NEO_WORLD": record.get("world", DEFAULT_WORLD),
            })
            try:
                process = subprocess.Popen(
                    [sys.executable, str(BOT_SCRIPT)],
                    cwd=str(BASE_DIR),
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log_handle.close()
            self.processes[record["id"]] = process
            return self.public_record(record)

    def stop(self, bot_id):
        with self.lock:
            record = self._record(bot_id)
            process = self._managed_process(record["id"])
            if process is None:
                raise HTTPException(status_code=409, detail="bot is not managed or already stopped")
            process.terminate()
            return {"ok": True, "id": record["id"]}

    def _active(self, record):
        return (self._managed_process(record["id"]) is not None
                or self._external_process_alive(record))

    def send_command(self, bot_id, action, payload):
        with self.lock:
            record = self._record(bot_id)
            if not self._active(record):
                raise HTTPException(status_code=409, detail="bot is offline")
            payload = payload or {}
            command = self._command_for(action, payload)
            command_file = Path(record["cmd_file"])
            command_file.parent.mkdir(parents=True, exist_ok=True)
            with command_file.open("a") as fh:
                fh.write(json.dumps(command, separators=(",", ":")) + "\n")
            return {"ok": True, "botId": record["id"], "command": command}

    @staticmethod
    def _number(payload, key, integer=False):
        if key not in payload:
            raise HTTPException(status_code=422, detail=f"missing field: {key}")
        try:
            return int(payload[key]) if integer else float(payload[key])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"invalid field: {key}") from exc

    def plan_path(self, bot_id, goal_x, goal_y):
        """Compute a live A* path for the dashboard; read-only, no bot action sent."""
        import stay_world
        with self.lock:
            record = self._record(bot_id)
            state = self._state(record)
            position = state.get("position")
            if not position:
                raise HTTPException(status_code=409, detail="bot has no live position")
            width = int((state.get("worldSize") or {}).get("width") or stay_world.WORLD_W)
            height = int((state.get("worldSize") or {}).get("height") or stay_world.WORLD_H)
            start = (int(position.get("tile", [0, 0])[0]),
                     int(position.get("tile", [0, 0])[1]))
            goal = (int(goal_x), int(goal_y))
            blocks = {int(k): int(v) for k, v in (state.get("blocks") or {}).items()}
            names = {int(k): v for k, v in (state.get("blockNames") or {}).items()}
            old_world_w, old_world_h = stay_world.WORLD_W, stay_world.WORLD_H
            old_blocks, old_names_by_id, old_names_to_id = stay_world.BLOCKS, stay_world.NAME_BY_ID, stay_world.NAME_TO_ID
            with stay_world.world_lock:
                stay_world.WORLD_W = width
                stay_world.WORLD_H = height
                stay_world.BLOCKS.clear()
                stay_world.BLOCKS.update(blocks)
                stay_world.NAME_BY_ID = names
                stay_world.NAME_TO_ID = {name: tid for tid, name in names.items()}
                try:
                    path = stay_world.find_path(start, goal)
                finally:
                    stay_world.WORLD_W = old_world_w
                    stay_world.WORLD_H = old_world_h
                    stay_world.BLOCKS.clear()
                    stay_world.BLOCKS.update(old_blocks)
                    stay_world.NAME_BY_ID = old_names_by_id
                    stay_world.NAME_TO_ID = old_names_to_id
        return {
            "start": list(start),
            "goal": list(goal),
            "path": [[direction, kind, list(dest)] for direction, kind, dest in path]
                    if path is not None else None,
        }

    def _command_for(self, action, payload):
        action = str(action).lower()
        if action == "move":
            command = {"cmd": "move", "dx": self._number(payload, "dx")}
            if "dy" in payload:
                command["dy"] = self._number(payload, "dy")
            return command
        if action in ("break", "mine"):
            return {
                "cmd": "mine",
                "x": self._number(payload, "x", integer=True),
                "y": self._number(payload, "y", integer=True),
                "delay": self._number(payload, "delay") if "delay" in payload else 0.16,
                "max": self._number(payload, "max", integer=True) if "max" in payload else 30,
            }
        if action == "place":
            block_type = str(payload.get("blockType", payload.get("type", "DIRT"))).strip()
            if not block_type:
                raise HTTPException(status_code=422, detail="missing field: blockType")
            command = {
                "cmd": "place",
                "x": self._number(payload, "x", integer=True),
                "y": self._number(payload, "y", integer=True),
                "blockType": block_type,
            }
            if isinstance(payload.get("block"), dict):
                command["block"] = payload["block"]
            return command
        if action in ("msg", "say"):
            text = str(payload.get("text", payload.get("message", "")))
            if not text.strip():
                raise HTTPException(status_code=422, detail="missing field: text")
            return {"cmd": "msg", "text": text}
        if action in ("drop", "drop_item"):
            item_type = str(payload.get("itemType", payload.get("type", ""))).strip()
            if not item_type:
                raise HTTPException(status_code=422, detail="missing field: itemType")
            count = self._number(payload, "count", integer=True) if "count" in payload else 1
            if count < 1:
                raise HTTPException(status_code=422, detail="count must be >= 1")
            return {"cmd": "drop", "itemType": item_type, "count": count}
        if action in ("pickup", "collect"):
            command = {"cmd": "collect"}
            if payload.get("id"):
                command["id"] = str(payload["id"])
            if payload.get("itemType"):
                command["itemType"] = str(payload["itemType"])
            return command
        if action in ("gotoplayer", "follow"):
            name = str(payload.get("name", "")).strip()
            if not name:
                raise HTTPException(status_code=422, detail="missing field: name")
            return {"cmd": "goto_player", "name": name}
        if action in ("findpath", "path", "goto"):
            return {
                "cmd": "goto_tile",
                "x": self._number(payload, "x", integer=True),
                "y": self._number(payload, "y", integer=True),
            }
        raise HTTPException(status_code=404, detail=f"unsupported action: {action}")


    @staticmethod
    def _challenge_for(record):
        state = record and read_json(Path(record["state_file"]), {}) or {}
        captcha = state.get("captcha") or {}
        last = captcha.get("lastChallenge")
        if not last or not captcha.get("active"):
            return None
        return {
            "botId": record["id"],
            "provider": last.get("provider", "hcaptcha"),
            "challengeId": last.get("challengeId", ""),
            "siteKey": last.get("siteKey", ""),
            "cData": last.get("cData") or last.get("challengeId", ""),
            "action": last.get("action", ""),
            "durationMs": last.get("durationMs"),
            "attempts": last.get("attempts"),
        }

    def get_captcha(self, bot_id):
        with self.lock:
            record = self._record(bot_id)
            challenge = self._challenge_for(record)
        if not challenge:
            return {"ok": True, "botId": bot_id, "active": False, "challenge": None}
        return {"ok": True, "botId": bot_id, "active": True, "challenge": challenge}

    def resolve_captcha(self, bot_id, challenge_id, token, c_data=None):
        token = str(token or "").strip()
        if not token:
            raise HTTPException(status_code=422, detail="missing field: token")
        with self.lock:
            record = self._record(bot_id)
            command = {
                "cmd": "captcha_resolve",
                "challengeId": str(challenge_id or "").strip(),
                "token": token,
            }
            if c_data:
                command["cData"] = str(c_data)
            command_file = Path(record["cmd_file"])
            command_file.parent.mkdir(parents=True, exist_ok=True)
            with command_file.open("a") as fh:
                fh.write(json.dumps(command, separators=(",", ":")) + "\n")
        return {"ok": True, "botId": record["id"], "command": command}


manager = BotManager()
app = FastAPI(title="Neotopia Bot Dashboard", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/bots")
async def bots():
    return {"bots": manager.list_public()}


@app.post("/bots/generate")
async def generate_bot(payload: dict | None = None):
    record = manager.generate()
    if (payload or {}).get("start", True):
        try:
            manager.start(record["id"])
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"bot start failed: {exc}") from exc
    return manager.public_record(manager.bots[record["id"]])


@app.post("/bots/{bot_id}/start")
async def start_bot(bot_id: str):
    return manager.start(bot_id)


@app.post("/bots/{bot_id}/stop")
async def stop_bot(bot_id: str):
    return manager.stop(bot_id)


@app.get("/world/{bot_id}")
async def world(bot_id: str):
    return manager.world(bot_id)


@app.post("/bots/{bot_id}/planpath")
async def plan_path_endpoint(bot_id: str, payload: dict | None = None):
    payload = payload or {}
    if "x" not in payload or "y" not in payload:
        raise HTTPException(status_code=422, detail="missing field: x or y")
    result = manager.plan_path(bot_id, payload["x"], payload["y"])
    return {"ok": True, "botId": bot_id, **result}


@app.post("/bots/{bot_id}/{action}")
async def bot_action(bot_id: str, action: str, payload: dict | None = None):
    return manager.send_command(bot_id, action, payload or {})


@app.get("/captcha/{bot_id}")
async def captcha_status(bot_id: str):
    return manager.get_captcha(bot_id)


@app.post("/captcha/{bot_id}/resolve")
async def captcha_resolve(bot_id: str, payload: dict | None = None):
    payload = payload or {}
    token = payload.get("token") or payload.get("captchaResponse") or payload.get("response")
    if not token:
        raise HTTPException(status_code=422, detail="missing field: token")
    return manager.resolve_captcha(
        bot_id,
        payload.get("challengeId"),
        token,
        payload.get("cData"),
    )


@app.get("/captcha", include_in_schema=False)
async def any_captcha():
    return manager.get_captcha("default")


@app.websocket("/ws")
async def websocket_state(socket: WebSocket):
    await socket.accept()
    try:
        while True:
            await socket.send_json({"type": "snapshot", "bots": manager.list_public()})
            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, RuntimeError):
        return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("dashboard_server:app", host="0.0.0.0", port=8787, reload=False)
