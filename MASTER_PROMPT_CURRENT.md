# MASTER PROMPT — Bot Neotopia (current handoff)

> Dokumen handoff aman untuk AI berikutnya. Tidak berisi password, refresh token,
> ID token, magic link, isi inbox, atau file profil browser.
> Dibuat 22 Oktober 2026, diperbarui 25 Oktober 2026 (FarmManager LAVA +
> dashboard captcha resolver). Baca juga `DOC.md` untuk ringkasan terbaru.

## 1. Identitas dan gaya pengguna

- **Koreksi 25 Okt 2026**: proyek sekarang di root `/data/data/com.termux/files/home/neo2`
  (bukan lagi `/root/neo`). Bot aktif terakhir: nama game `reza2302`,
  `guest_account.json` (default), world `778839`, spawn `(90,24)`. Referensi
  PID/`guest_account_gmail.json`/`Serdodo` di bawah adalah riwayat.
- Pengguna memainkan Neotopia sebagai **Serdo** di world **778839**.
- Bahasa balasan: **Bahasa Indonesia santai, singkat, langsung ke inti**.
- Pola kerja: pengguna mengedit obstacle course secara live saat bot berjalan.
- Jangan mengandalkan dump peta lama. Selalu gunakan `BLOCKS` live, cek posisi nyata,
  lalu replan bila ada mismatch atau correction server.
- Pengguna menyukai eksekusi langsung dan laporan berbukti: apa yang berubah, koordinat,
  status server, serta hasil log. Konfirmasi hanya untuk aksi berisiko/merusak.

## 2. Tujuan proyek

Bot pendamping Neotopia yang dapat:

- login Firebase dan masuk world 778839;
- Mengikuti karakter Serdo;
- **melayani FarmManager LAVA otomatif**: break LAVA, kumpulkan LAVA/LAVA_SEED
  (bukan GEMS), tanam seed, tunggu tumbuh, harvest pohon, dan berulang dengan
  tahan reconnect;
- memukul/menghancurkan block dengan cepat;
- mengambil, menjatuhkan, dan memasang item/block;
- mengirim chat;
- dikendalikan melalui JSON satu baris di `stay_cmd.txt`;
- dipantau melalui `bot_state.json`, `stay_world.log`, dan dashboard lokal.

## 3. Lokasi dan akun aktif

- Project root: `/root/neo`.
- Environment target: Termux/Android; Chromium dan ChromeDriver tersedia di:
  `/data/data/com.termux/files/usr/bin/chromium-browser` dan
  `/data/data/com.termux/files/usr/bin/chromedriver`.
- Bot aktif terakhir memakai file akun lokal:
  `/root/neo/guest_account_gmail.json`.
- **Jangan menyalin atau menampilkan isi file akun.** File tersebut berisi credential
  Firebase dan permission terakhir terverifikasi `600`.
- Bot aktif terakhir:
  - PID `6611`;
  - nama game `Serdodo`;
  - status `online`;
  - world `778839`;
  - posisi terakhir `(2880,774)`, tile `(90,24)`;
  - bukti join terakhir di log: `2026-10-22 10:16:40,334 JOINED world=778839`.
- Satu akun hanya boleh satu sesi. Selalu cek:
  `pgrep -af '^python3 stay_world.py'`.

## 4. Arsitektur/protokol yang sudah diketahui

- Master WebSocket: `wss://game.playneotopia.com/ws/master` dengan query
  `clientPlatform=web&clientRevision=37`.
- Master menerima Firebase ID token, mengembalikan route ticket, kemudian bot masuk
  worker `/ws/worker/N` memakai permessage-deflate.
- Spawn standar: pixel `(2880,774)` = tile `(90,24)`.
- World biasanya berukuran `100x60` tile; registry block dikirim melalui metadata.
- Reconnect otomatis setelah worker close adalah perilaku normal. Saat reconnect,
  bot dapat respawn di spawn.

### Paket posisi

Format client-to-server 24 byte:

```text
>BHIffhhBI
op=1, len=21, seq u32, x f32, y f32, vx i16, vy i16, flags u8, ts u32
```

Flags penting:

- bit 0: facing left;
- bit 1: left;
- bit 2: right;
- bit 3 (`0x08`): airborne/jump;
- bit 4: down.

Capture client asli menunjukkan bit 3 dipakai saat lompat; walk-off/fall tidak boleh
otomatis dianggap jump.

### Paket server

- opcode 4: batch positions;
- opcode 6: correction posisi `float x, float y`;
- opcode 16: snapshot block `NEOW`;
- opcode 17: snapshot dropped item;
- JSON penting: `world_joined`, `block_update`, `block_ack`, `player_*`,
  `inventory_snapshot`, `drop_ack`, `pickup_ack`, `anticheat_warning`,
  `player_punched`, dan event koneksi.

## 5. Fisika dan pathfinding

Konstanta utama di `stay_world.py`:

```text
TILE=32
PLAYER_W=22, PLAYER_H=26
GRAVITY=0.32, JUMP_V=-8.2, MAX_VY=13, MAX_VX=5
SIM_HZ=60, SIM_SEND_EVERY=3
```

Support tile dihitung dari pusat player:

```python
tile_at(x, y) = (
    int((x + PLAYER_W / 2) // TILE),
    int((y + PLAYER_H) // TILE) - 1,
)
```

Solid names utama: `DIRT`, `GRASS`, `STONE`, `BEDROCK`, `TREE`, `WORLD_LOCK`,
`SHOOTER_BLOCK`. Fallback ID: `3,4,5,8,10,16,25`.

### Perubahan findpath terbaru

- A* tetap memakai edge fisika `walk`, `drop`, `jump`, `hop`, `fall`.
- Biaya edge sekarang memprioritaskan `walk` pendek/satu tile.
- Jump/drop panjang tetap tersedia sebagai fallback ketika ada gap atau tidak ada tile
  perantara yang bisa diinjak.
- `do_goto_tile()` sekarang menjalankan **satu edge/landing**, validasi hasil, lalu
  memanggil `find_path()` lagi dari posisi aktual. Ini membuat perilaku lebih mirip
  checkpoint per tile/landing ala Growtopia dan lebih tahan terhadap edit map live.
- Jangan membagi jump/drop menjadi tile udara palsu; tile di tengah lintasan belum tentu
  merupakan tempat berdiri yang valid.
- `snap_x()` masih menyelaraskan x ke posisi kanonik setelah landing.

Contoh yang sudah diuji:

```text
(90,24) -> (88,24)
step 1: (90,24) -> (89,24)
step 2: (89,24) -> (88,24)
```

Jika lantai terputus, `path` boleh mengembalikan satu jump panjang. Contoh live:
target `(85,24)` dari `(90,24)` menghasilkan `jump dir=-1 -> (85,24)` karena tile
`(89..86,25)` kosong.

## 6. Fitur yang sudah terbukti

### Koneksi dan kontrol dasar

- Firebase auth, master auth, route, worker auth, dan `world_joined` berhasil.
- Reconnect otomatis tersedia.
- `pos`, `player`, dan state realtime bekerja.
- `goto_player`/`follow` dapat menarget tile posisi player yang terlacak.

### Gerak

- `goto_tile` dengan simulasi collision/gravity dan pathfinding A* bekerja.
- Permukaan, gua, naik/turun, overhang, gap, dan landing berbasis pusat player sudah
  pernah diuji.
- `move`/`walkto` adalah perpindahan datar sederhana tanpa collision/gravity; jangan
  dipakai untuk obstacle.
- `jump` mentah dan `tp` berisiko; hindari untuk navigasi normal.

### Break/mine/punch

- Protocol hit:
  `{"type":"expression","expressionId":"punch"}` lalu
  `{"type":"punch","x":x,"y":y}`.
- Delay standar `0.16` detik per hit; server pernah menerima sekitar 80–160 ms.
- `mine` menunggu block menjadi AIR atau mencapai batas hit.
- Break STONE terdekat di `(79,28)` pernah sukses: 10 hit, sekitar 1.61 detik,
  server mengirim `block_ack` dan `AIR`.

### Place

- `place` memvalidasi koneksi, batas tile, jarak, tile kosong, dan nama block.
- Uji live berhasil menaruh `DIRT` di `(91,24)`.
- Server mengirim `block_ack`; inventory berubah `DIRT x14 -> x13`.

### Drop/pickup/inventory

- `drop`/`drop_item` bekerja dengan packet `dropped_item`.
- Uji live berhasil drop `DIRT x1`; server memberi `drop_ack`, inventory berkurang
  `DIRT x15 -> x14`.
- `pickup`/`collect` bekerja dengan packet `pickup_item`.
- Decoder binary opcode 17 membaca ground-drop snapshot.
- Uji live berhasil mengambil `DIRT x15`; server memberi `pickup_ack`, inventory
  menjadi `DIRT x15`.
- Auto-collect dekat bot aktif secara default untuk drop player lain; drop buatan bot
  sendiri tidak diambil otomatis.

### Chat dan dashboard

- `say`/`msg` memakai packet `{"type":"chat","text":"..."}`.
- Dashboard FastAPI + WebSocket + vanilla JS tersedia.
- Dashboard memiliki kontrol break, place, msg, goto player, findpath, drop, collect,
  state realtime, inventory, dropped items, dan map.
- `POST /bots/:id/findpath` (alias `path`, `goto`) = `goto_tile` nyata (bot jalan).
- `POST /bots/:id/planpath` = **preview A* read-only**, tidak mengirim aksi ke bot;
  memakai map live (`worldSize`, `blocks`, `blockNames`) dari state snapshot dan
  mengembalikan `start`, `goal`, `path`. Berguna cek rute sebelum perintah jalan.
- `POST /bots/generate` = generate akun bot baru (butuh `NEO_FIREBASE_API_KEY` atau
  `api_key` di `guest_account.json`), lalu start/stop per bot via endpoint terpisah.
- Endpoint `/world/:id` mengembalikan map, block ID, pemain, posisi bot.

### Mode eksperimen `air_goto`

Command baru di `stay_world.py`:

```json
{"cmd":"air_goto","x":88,"y":19}
```

Semantik default adalah tile; bisa pixel:

```json
{"cmd":"air_goto","x":2821,"y":611,"units":"px"}
```

Perilaku:

- bergerak inkremental, bukan `tp`;
- default speed `4 px/tick`, dibatasi maksimum `8`;
- tidak mensyaratkan block penopang lokal;
- abort jika server mengirim correction atau koneksi putus;
- selalu membersihkan target lama dan `busy` pada akhir.

Uji live berhasil mencapai `(2821,611)` dari spawn dalam 44 langkah, tetapi sekitar
4 detik kemudian server mengoreksi kembali ke `(2880,774)`. Tidak ada
`anticheat_warning` baru pada uji itu. Kesimpulan: mode ini membuktikan gerak udara
sementara, tetapi server tidak mengizinkan posisi udara bertahan.

## 7. Generator akun dan auth

`neotopia_tool.py` sekarang menghasilkan akun yang **sudah siap join world** (bukan
hanya export token mentah). Alur lengkap:

- membuat akun email temporary via Mail.tm (password + nama random);
- request magic link Firebase dan membacanya dari inbox Mail.tm;
- menjalankan **onboarding di headless Chromium CDP**:
  - sign-in / Confirm Email;
  - CHOOSE YOUR NAME (isi username, klik START PLAYING; retry otomatis dengan
    suffix unik bila username "temporarily unavailable");
  - skip tutorial (klik "Skip");
  - deteksi "ACCOUNT CHECK" (limit 4 akun per group) → klik TRY AGAIN / SIGN OUT;
  - deteksi dashboard utama (JOIN / World / START / HOME BASE).
- mengekstrak refresh token Firebase dari sesi browser (localStorage,
  sessionStorage, Firebase SDK `getIdToken()`, IndexedDB). Bila tidak ketemu,
  **request fresh magic link** karena oobCode asli sudah dikonsumsi browser.
- update `displayName` via `accounts:update` bila perlu.
- mengekspor file bot `guest_account_<email>.json` (permission `600`) dengan
  `canonical_name`, `uid`, `world: 778839` — siap dipakai `stay_world.py`.

Catatan penting:

- Chromium di Termux memakai CDP mentah via websocket (selenium tidak tersedia di
  android/aarch64). Port CDP **9333** (bukan 9222 — 9222 dikuasai app
  `com.unixshells.devbrowser`). Flag: `--headless=new --no-sandbox --disable-gpu
  --remote-allow-origins=*`. `SingletonLock` lama dihapus dulu; profile
  `chrome-profile-neo` di-clear per akun untuk menghindari limit group.
- Akun yang masih `tutorialState=new` akan ditolak `join_world` dengan
  `USERNAME_REQUIRED`. Setelah onboarding web, `tutorialState=completed`,
  `accountState=ready`, `canonicalName` terisi → join berhasil.
- Jangan menampilkan refresh token / id token / magic link di terminal.

Pemakaian:

```bash
python3 neotopia_tool.py                  # generate N akun (batch)
python3 neotopia_tool.py auth alamat@email.com
python3 neotopia_tool.py get-credentials alamat@email.com
```

Jalankan bot dengan:

```bash
NEO_ACCOUNT=/root/neo/guest_account_<email>.json python3 stay_world.py
```

Status aktif terakhir (per 23 Oktober 2026): 10 akun maxxspace.com
`tutorial=completed` tersimpan dan teruji JOIN-OK di world 778839; 14 akun
`tutorial=new` sudah dibersihkan. Akun aktif dashboard `guest_account.json`
adalah salah satu yang `completed`.

Credential berikut adalah private dan tidak boleh dimasukkan ke handoff/zip:

- `guest_account*.json`;
- `accounts.json` bila ada;
- `idtoken.txt`, `refresh_token.txt`, `uid.txt`, `headers.txt`;
- magic link atau isi inbox;
- `chrome-profile/`;
- `bot_state.json`, `stay_world.log`, dan log sementara.

## 8. Perintah operasional

Semua command ditulis sebagai satu JSON per baris ke `stay_cmd.txt`; file akan dihapus
setelah dibaca.

```bash
printf '%s\n' '{"cmd":"pos"}' > stay_cmd.txt
printf '%s\n' '{"cmd":"player"}' > stay_cmd.txt
printf '%s\n' '{"cmd":"map","x":72,"y":23,"w":16,"h":12}' > stay_cmd.txt
printf '%s\n' '{"cmd":"path","x":80,"y":28}' > stay_cmd.txt
printf '%s\n' '{"cmd":"goto_tile","x":80,"y":28}' > stay_cmd.txt
printf '%s\n' '{"cmd":"goto_player","name":"Serdo"}' > stay_cmd.txt
printf '%s\n' '{"cmd":"mine","x":79,"y":28,"delay":0.16,"max":10}' > stay_cmd.txt
printf '%s\n' '{"cmd":"place","x":91,"y":24,"blockType":"DIRT"}' > stay_cmd.txt
printf '%s\n' '{"cmd":"drop","itemType":"DIRT","count":1}' > stay_cmd.txt
printf '%s\n' '{"cmd":"collect"}' > stay_cmd.txt
printf '%s\n' '{"cmd":"air_goto","x":88,"y":19}' > stay_cmd.txt
```

Melalui dashboard (bot sudah online) — preview rute tanpa memerintah bot:

```bash
curl -s -X POST http://127.0.0.1:8787/bots/default/planpath \
  -H 'Content-Type: application/json' -d '{"x":80,"y":28}'
```

Lalu perintah jalan (sama `findpath`):

```bash
curl -s -X POST http://127.0.0.1:8787/bots/default/findpath \
  -H 'Content-Type: application/json' -d '{"x":80,"y":28}'
```

Status cepat:

```bash
pgrep -af '^python3 stay_world.py'
python3 -c 'import json; print(json.load(open("bot_state.json"))["position"])'
```

## 9. Bahaya dan aturan wajib

- Jangan menjalankan `capture_*.py` ketika bot aktif; script capture bisa login memakai
  akun yang sama dan menendang sesi bot.
- Jangan menjalankan dua proses dengan akun yang sama.
- Jangan memakai `tp`, raw `fly`, atau `jump` mentah untuk navigasi normal.
- Tepi kiri platform sekitar x72–76 berulang kali memicu correction/respawn dan pernah
  memicu `anticheat_warning level=suspect`. Pakai rute timur sebagai fallback.
- Server CLOSE tanpa code/reason dapat terjadi dan tidak otomatis berarti anti-cheat.
- Knockback dari player lain dapat membuat posisi lokal desync; lakukan `pos`, cek log,
  lalu path ulang dari posisi aktual.
- Dunia diedit live; map/path lama kadaluarsa.
- Sebelum mengklaim aksi berhasil, cari bukti `block_ack`, `pickup_ack`, `drop_ack`,
  `AIR` di state/map, `JOINED`, atau posisi akhir yang terverifikasi.
- Aksi yang mematikan proses, menghapus file, mengganti credential, atau mengubah akun
  perlu diperlakukan sebagai aksi berisiko.

## 10. File penting

| File | Fungsi |
|---|---|
| `stay_world.py` | Bot utama, WS, auth, reader, physics, A*, goto, air_goto, punch, mine, place, drop, pickup, FarmManager, capture captcha |
| `neotopia_tool.py` | Generator temp-mail + onboarding Chromium CDP + export account bot siap join |
| `stay_cmd.txt` | Antrian command live |
| `bot_state.json` | Snapshot state realtime; private/local |
| `stay_world.log` | Bukti join, posisi, map, ack, correction; private/local |
| `dashboard_server.py` | REST/WebSocket control plane (findpath, planpath, generate, captcha, per-bot start/stop) |
| `dashboard/` | UI dashboard (termasuk `captcha.html` hCaptcha resolver) |
| `guest_account*.json` | File akun siap join (private, permission 600) |
| `tests/test_bot_features.py` | Test fitur bot, path/air_goto, FarmManager (21 test) |
| `tests/test_dashboard_commands.py` | Test mapping command dashboard |
| `DASHBOARD.md` | Dokumentasi dashboard |
| `DOC.md` | Ringkasan terbaru: cara start/stop, FarmManager, status captcha |
| `diag_path.py` | Diagnostik path offline; map bisa stale/incomplete |

## 11. Checks terakhir

Checks yang benar-benar dijalankan setelah perubahan terakhir:

```text
python3 -m py_compile stay_world.py tests/test_bot_features.py
  passed

python3 -m pytest -q
  17 passed

python3 -m py_compile neotopia_tool.py
  passed

offline bot-account export check
  passed; format cocok dengan stay_world.py; permission 600
```

Manual smoke test terakhir:

```text
bot login Gmail/email-link: passed
JOINED world=778839: passed
air_goto (spawn -> tile 88,19): reached target temporarily, then server correction
current online bot: passed; PID 6611, Serdodo, tile (90,24)
```

## 12. Langkah pertama untuk AI berikutnya

1. Baca file ini, lalu cek proses:
   `pgrep -af '^python3 stay_world.py'`.
2. Baca state live tanpa membaca credential:
   `bot_state.json` dan tail log terakhir.
3. Pastikan hanya ada satu instance akun aktif.
4. Jika menguji navigasi, kirim `path` dahulu, baca map live, lalu gunakan `goto_tile`
   per edge/checkpoint sambil memantau correction. Preview rute aman lewat
   dashboard `planpath` (read-only) sebelum mengirim `findpath` (perintah jalan nyata).
5. Jika melanjutkan eksperimen udara, gunakan `air_goto` hanya sebagai eksperimen,
   jangan menganggap target tercapai permanen sebelum server tidak melakukan correction.
6. Jika memakai generator account, gunakan file export `guest_account_<email>.json`,
   bukan format mentah `accounts.json`. Akun hasil generator sudah
   `tutorial=completed` dan siap join world 778839.

## 13. Status akhir singkat

Proyek inti sudah berfungsi: auth, online world, pathfinding fisika, stepwise replan,
mine, place, drop, pickup, chat, dashboard, generator account, dan mode air eksperimen.

Kemampuan baru yang sudah terverifikasi sesi ini (23 Oktober 2026):

- **Generator akun otomatis end-to-end**: Mail.tm → magic link → onboarding
  headless Chromium (choose username + skip tutorial) → refresh token → file bot
  siap join. 10 akun maxxspace.com teruji `tutorial=completed` dan JOIN-OK di
  world 778839.
- **Dashboard `findpath` (goto_tile nyata)**: bot benar-benar bergerak; uji live
  dari tile `(90,24)` ke `(50,25)` berhasil.
- **Dashboard `planpath` (A* read-only)**: preview rute dari map live tanpa
  memerintah bot.
- **Pembersihan akun**: 14 file `guest_account_*.json` yang `tutorial=new` (ditolak
  `USERNAME_REQUIRED`) dihapus; sisanya 10 yang `completed`.

Masalah utama yang masih terbuka adalah validasi server terhadap free-air movement
dan jalur kiri platform yang sensitif anti-cheat. Prioritas aman berikutnya adalah
menjaga `goto_tile` normal tetap memakai landing valid dan memperbaiki path
berdasarkan bukti live, bukan memaksa `air_goto` menjadi navigasi permanen.

## 14. FarmManager LAVA (baru, 25 Oktober 2026)

State machine otomati farming pohon LAVA di `stay_world.py` (class `FarmManager`,
`farm_manager` global module-level). Stages: IDLE → DROPPING_SEEDS → BREAKING →
COLLECTING_BREAK → PLANTING → WAITING_GROWTH → HARVESTING → RETURN_HOME → ulang.

- Konstanta: `HOME_TILE=(10,24)`, `BREAK_TILE=(9,24)`, `PLANT 11..50 (y=24)`,
  `DROP_ZONE (1..40, y=26)` max 1500/koordinat, `SEED_OVERFLOW_THRESHOLD=200`,
  `GROW_TIME=33.0s`.
- BREAKING memakai `do_combo()` (place + 3 punch), terbukti jalan; tunggu
  `INVENTORY["LAVA"]==0` baru lanjut.
- Pickup selektif: hanya `FARM_ITEMS={"LAVA","LAVA_SEED"}`; GEMS/DIRT ditinggal.
- Reconnect-resilient: state tidak hilang saat `run_session()` restart;
  `on_reconnect(mover)` goto `current_target_tile` lalu lanjut stage.
- Command: `{"cmd":"farm","action":"start|stop|status"}`.
- Status ada di `bot_state.json.farm`.
- 21 test di `tests/test_bot_features.py` termasuk 3 test FarmManager.

## 15. Captcha / breaker_guard (BUKAN selesai)

Server mengunci gameplay via hCaptcha `breaker_guard` saat deteksi aksi bot
(place/break cepat). Sudah ada: capture `bot_captcha` ke global `CAPTCHA`
(only-latest) + `bot_state.json.captcha`, command `captcha_resolve` (kirim WS
`type:"captcha_resolved"`), endpoint dashboard `GET/POST /captcha/:id`, dan
UI `dashboard/captcha.html`.

**Masih gagal unlock** karena: (a) server memrotasi `challengeId` setiap ~30 detik,
hCaptcha token single-use ter-bind ke challenge lama sehingga selalu kedaluwarsa
sebelum sampai; (b) format/field packet `captcha_resolved` belum terverifikasi
(asumsi, belum di-cross-check dengan client resmi). Lihat `DOC.md` §4 untuk
detail + 3 jalan keluar (A: stop farm saat lock, B: packet capture client resmi,
C: auto-solve within window).
