# Neotopia Bot & Dashboard

An automation bot, control panel, and management suite for **Neotopia**, optimized for Termux (Android) and Linux environments.

---

## Features

- **Robust WebSocket Client**: Handles Firebase authentication, Master/Worker routing, heartbeat, and automatic reconnection.
- **Physics-Aware Pathfinding**: A* pathfinding utilizing physical edges (`walk`, `drop`, `jump`, `hop`, `fall`) and stepwise checkpoint movement (`goto_tile`).
- **LAVA FarmManager**: Automated state machine for farming LAVA trees (breaking, collecting seeds/lava, planting, waiting for growth, harvesting, and overflow management).
- **FastAPI Control Plane & Dashboard**: Real-time web dashboard for bot monitoring, live map visualization, path planning (`planpath`), movement execution (`findpath`), inventory tracking, and hCaptcha resolver interface.
- **Account Generator Tool (`neotopia_tool.py`)**: End-to-end account creation via Temp-Mail and headless Chromium CDP onboarding (username selection, tutorial skipping, and Firebase credential export).
- **Comprehensive Unit Testing**: Suite of unit tests covering pathfinding, farming state machine, inventory actions, and dashboard command mapping.

---

## Architecture & Project Structure

| File / Directory | Description |
|---|---|
| `stay_world.py` | Main bot engine: WebSocket connection, physics mover, `FarmManager`, command polling, and game packet handler. |
| `dashboard_server.py` | FastAPI server providing REST endpoints and WebSocket stream for live bot monitoring and control. |
| `dashboard/` | Web dashboard frontend (`index.html`, `app.js`, `styles.css`, and `captcha.html` for hCaptcha resolution). |
| `neotopia_tool.py` | Temp-mail generator and headless Chromium CDP onboarding tool for creating ready-to-join bot accounts. |
| `tests/` | Unit test suite (`test_bot_features.py`, `test_dashboard_commands.py`). |
| `stay_cmd.txt` | Single-line JSON command queue consumed and cleared by the bot during runtime. |
| `bot_state.json` | Real-time state snapshot (position, inventory, farm state, blocks, dropped items). |

---

## Installation

### Prerequisites
- Python 3.8+
- `pip`

### Dependencies
```bash
# Install core dependencies
pip install -r requirements-dashboard.txt
```

---

## Usage

### 1. Running the Bot Engine
```bash
nohup python3 stay_world.py >> stay_world.log 2>&1 &
```

### 2. Running the Control Dashboard
```bash
nohup python3 dashboard_server.py >> /tmp/dash.log 2>&1 &
```
Open `http://127.0.0.1:8787/` in your browser.

### 3. Sending Commands
Append single-line JSON commands to `stay_cmd.txt`:
```bash
echo '{"cmd":"farm","action":"start"}' >> stay_cmd.txt
echo '{"cmd":"goto_tile","x":80,"y":28}' >> stay_cmd.txt
```

---

## FarmManager (LAVA Farming)

The `FarmManager` automates the complete LAVA tree farming cycle:
1. `IDLE` / `HOME`
2. `DROPPING_SEEDS` (if seed count exceeds overflow threshold)
3. `BREAKING` (using combo place + punch)
4. `COLLECTING_BREAK` (selective pickup of `LAVA` and `LAVA_SEED`, ignoring gems/other items)
5. `PLANTING`
6. `WAITING_GROWTH`
7. `HARVESTING`

Control commands:
```json
{"cmd":"farm","action":"start"}
{"cmd":"farm","action":"stop"}
{"cmd":"farm","action":"status"}
```

---

## Unit Tests

Run the test suite using pytest:
```bash
python3 -m pytest -q
```

---

## Known Limitations & Captcha Notice

- **hCaptcha (`breaker_guard`)**: Rapid mining/placing triggers server-side anti-cheat hCaptcha challenges. While capture events and resolver UI (`captcha.html`) are implemented, automatic token injection currently faces challenges due to frequent `challengeId` rotation (~30s) and strict single-use token binding.
- **Air Movement (`air_goto`)**: Experimental free-air movement works temporarily but will be corrected by server anti-cheat if maintained too long. Normal gameplay relies on validated ground pathfinding.

---

## License

This project is intended for educational and automation research purposes.
