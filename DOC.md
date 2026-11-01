# Neotopia Bot — Dokumentasi & Cara Melanjutkan

Bot otomasi farming LAVA untuk game Neotopia, jalan di Termux (Android).
Diperbarui terakhir: 2026-10-25.

---

## 1. Cara Menjalankan

```bash
cd /data/data/com.termux/files/home/neo2

# 1. Jalankan bot utama (engine + FarmManager)
nohup python3 stay_world.py >> stay_world.log 2>&1 &

# 2. Jalankan dashboard (kontrol + map + captcha UI) — opsional tapi berguna
nohup python3 dashboard_server.py >> /tmp/dash.log 2>&1 &
```

Dashboard tersedia di `http://127.0.0.1:8787` (atau `http://<IP_termux>:8787` dari PC).
Tombol "Captcha" di dashboard → buka `captcha.html`.

Hentikan semua:
```bash
pkill -f "python3 stay_world.py"
pkill -f "dashboard_server.py"
```

## 2. Arsitektur / File

| File | Fungsi |
|------|--------|
| `stay_world.py` | Engine bot: WS ke server, `Mover` (bergerak/jalan), `FarmManager` (farming LAVA), `poll_commands` (baca `stay_cmd.txt`), `handle_message` (process packet server, capture `bot_captcha`) |
| `dashboard_server.py` | FastAPI control plane: `/bots`, `/world/{id}`, `/planpath`, `/captcha/{id}`, `/captcha/{id}/resolve`, mount static `dashboard/` |
| `dashboard/index.html` + `app.js` + `styles.css` | UI dashboard (map live, kontrol bot, generate guest) |
| `dashboard/captcha.html` | UI hCaptcha resolver (widget + inject token ke `stay_cmd.txt`) |
| `tests/test_bot_features.py` | 21 unit tests (farm, path, place/drop/pickup, captcha) |
| `guest_account.json` | Akun bot (Firebase: `id_token`, `refresh_token`, `uid`, `api_key`) |
| `stay_cmd.txt` | Antrean command (JSON per baris). Bot baca+hapus tiap poll. Dashboard append ke sini |
| `bot_state.json` | Snapshot state bot (posisi, inventory, farm, captcha, blocks, drops) |
| `stay_world.log` | Log bot |

## 3. FarmManager (Farming LAVA)

State machine otomati farming pohon LAVA. Konstanta:
- `HOME_TILE = (10,24)` — posisi standby bot
- `BREAK_TILE = (9,24)` — tempat nge-break LAVA
- `PLANT_START..PLANT_END = 11..50` (y=24) — barisan tanam seed
- `DROP_ZONE = (1..40, 26)` — zona overflow LAVA_SEED (max 1500/koordinat)
- `SEED_OVERFLOW_THRESHOLD = 200`, `GROW_TIME = 33.0s`

Alur: IDLE → DROPPING_SEEDS (jika seed≥200) → BREAKING → COLLECTING_BREAK
→ PLANTING → WAITING_GROWTH → HARVESTING → RETURN_HOME → ulang.

Reconnect-resilient: `farm_manager` global module-level, state tidak hilang saat
`run_session()` restart. `on_reconnect(mover)` goto `current_target_tile` lalu lanjut stage.

Pilih LAVA & LAVA_SEED saja — **GEMS dan item lain tidak di-take**
(`FARM_ITEMS = {"LAVA","LAVA_SEED"}`).

Command (append ke `stay_cmd.txt`):
```bash
echo '{"cmd":"farm","action":"start"}'  >> stay_cmd.txt
echo '{"cmd":"farm","action":"stop"}'   >> stay_cmd.txt
echo '{"cmd":"farm","action":"status"}' >> stay_cmd.txt
```

## 4. Captcha / breaker_guard  ⚠️ BELUM SELESAI

Server mengunci gameplay (`verification_required`) lewat hCaptcha
`breaker_guard` saat mendeteksi aksi bot (place/break cepat). Bot capture
event `bot_captcha` ke `CAPTCHA` global + `bot_state.json.captcha`.

**Yang sudah jadi:**
- `stay_world.py` — handler `bot_captcha`/`verification_required`,
  command `captcha_resolve` (kirim WS `type:"captcha_resolved"`),
  `CAPTCHA` kini only-latest-challenge (perna bug dict grow jadi stale).
- `dashboard_server.py` — `GET /captcha/{id}` (baca challenge),
  `POST /captcha/{id}/resolve` (append `{"cmd":"captcha_resolve",...}` ke `stay_cmd.txt`).
- `dashboard/captcha.html` — widget hCaptcha (auto re-render saat challengeId
  rotasi, tidak re-render tiap poll), tombol "Inject token".

**Yang BELUM jalan (hambatan utama):**
1. **Server rotasi `challengeId` setiap ~30 detik.** hCaptcha token
   single-use & ter-bind ke challengeId tertentu. Alur manual
   "solve → inject" selalu telat: saat token sampai, server sudah
   ganti challenge lama → server tolak → game tetap locked →
   challenge baru muncul. Loop `bot_captcha` terus terjadi.
2. **Format packet `captcha_resolved` belum terverifikasi** benar.
   Saat ini kirim `{"type":"captcha_resolved","provider":"hcaptcha",
   "challengeId","cData","response","token"}` — field names ini
   belum tentu cocok dengan ekspektasi server (asumsi, tidak di-test
   terhadap client asli).

**Jalan keluar (pilih salah satu):**
- **A. Hentikan farm saat breaker_guard aktif** — bot idle → breaker_guard
  tidak memicu captcha → tidak ada lock. Paling bersih untuk sementara.
- **B. Packet capture dari client resmi** — jalankan game client APK,
  capture packet `captcha_resolved` yang benar-benar diterima server saat
  solve di UI resmi → tahu field persis. Lalu sesuaikan `stay_world.py`.
- **C. Auto-solve + inject within window** — buat loop yang langsung
  POST token begitu `bot_captcha` baru diterima (sebelum 30s habis).
  Perlu auto hCaptcha solver + format packet yang benar dulu (lihat B).

## 5. Unit Tests

```bash
cd /data/data/com.termux/files/home/neo2
python3 -m pytest tests/test_bot_features.py -q
# → 21 passed
```

## 6. Catatan Lain

- Bot connect sebagai `reza2302` (uid dari `guest_account.json`).
- World default `778839` (env `NEO_WORLD`), spawn `(90,24)`.
- `do_combo()` adalah cara break yang terbukti jalan (place+3 punch).
  FarmManager BREAKING memakai `do_combo`.
- Log bot banyak `bin opcode=15` (data server binary) — filter dengan
  `grep -v "bin opcode" stay_world.log` saat debug.
