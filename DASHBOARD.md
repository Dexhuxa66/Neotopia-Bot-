# Neotopia Bot Dashboard

Dashboard ringan untuk mengendalikan beberapa akun bot tanpa mengubah protokol
command lama di `stay_cmd.txt`.

## Jalankan

```bash
python3 -m pip install -r requirements-dashboard.txt
python3 -m uvicorn dashboard_server:app --host 0.0.0.0 --port 8787
```

Buka `http://127.0.0.1:8787/` di browser.

`guest_account.json` yang sudah ada dipasang sebagai bot `default`, tetapi tidak

## Endpoint utama

- `GET /bots` — daftar bot dan status.
- `POST /bots/generate` — buat akun guest baru via generator
  (`neotopia_tool.py`); body opsional `{"start": true}`.
- `POST /bots/:id/start` dan `/stop` — kelola bot yang dibuat dashboard
  (launch `stay_world.py` per bot, command/state/log terpisah).
- `GET /world/:id` — map, block IDs, pemain, dan posisi bot.
- `POST /bots/:id/move` — `{"dx": 64}`.
- `POST /bots/:id/break` — `{"x": 79, "y": 28, "delay": 0.16, "max": 30}`.
- `POST /bots/:id/place` — `{"x": 79, "y": 28, "blockType": "DIRT"}`.
- `POST /bots/:id/drop` — `{"itemType": "PHONE", "count": 1}`.
- `POST /bots/:id/collect` — `{}` untuk item terdekat atau `{"id": "drop_..."}`.
- `POST /bots/:id/msg` — `{"text": "halo"}`.
- `POST /bots/:id/gotoplayer` — `{"name": "Serdo"}`.
- `POST /bots/:id/findpath` (alias `path`, `goto`) — `{"x": 80, "y": 28}`;
  perintah `goto_tile` nyata, bot berjalan ke target.
- `POST /bots/:id/planpath` — `{"x": 80, "y": 28}` preview A* read-only dari
  map live (state snapshot); mengembalikan `start`, `goal`, `path` tanpa
  mengirim aksi ke bot.
- `GET /captcha/:id` — baca challenge hCaptcha aktif dari `bot_state.json`
  (`active`, `challengeId`, `siteKey`, `cData`, `action`, `attempts`).
- `POST /captcha/:id/resolve` — `{"challengeId","cData","token"}`; append
  `{"cmd":"captcha_resolve",...}` ke `stay_cmd.txt`.
- `GET /captcha` — shortcut ke bot `default`.
- `WS /ws` — snapshot daftar bot setiap 500 ms.

## UI Captcha Resolver

`/static/captcha.html` (terjangkau lewat tombol "Captcha" di dashboard) memuat
widget hCaptcha yang di-load dari `GET /captcha/:id`:

- Widget auto re-render HANYA saat `challengeId` rotasi (bukan tiap poll 4 detik).
- Setelah solve, tombol "Inject token" memanggil `POST /captcha/:id/resolve`
  dengan token hCaptcha yang baru.
- Lihat `DOC.md` §4 untuk status: unlock belum berhasil karena server
  memrotasi `challengeId` setiap ~30 detik dan format packet
  `captcha_resolved` belum terverifikasi.

`generate` butuh API key Firebase: environment `NEO_FIREBASE_API_KEY` atau
field `api_key` di `guest_account.json`. Contoh:

```bash
NEO_FIREBASE_API_KEY=AIza... python3 -m uvicorn dashboard_server:app --port 8787
curl -s -X POST http://127.0.0.1:8787/bots/generate -H 'Content-Type: application/json' -d '{}'
```

`drop` membaca inventory live dari `inventory_snapshot`, menolak item yang tidak
tersedia serta `FIST`/`WRENCH`, lalu mengirim packet `dropped_item` di posisi
sekitar 20 px dari bot. Hasil authoritative diproses dari `drop_ack` atau
`drop_reject`.

Saat event `dropped_item` masuk dalam jarak 32 px, bot otomatis mengirim
`pickup_item` (kecuali item yang dijatuhkan oleh bot sendiri). Command manualnya:

```json
{"cmd":"collect"}
{"cmd":"pickup","id":"drop_..."}
```

`place` memvalidasi koneksi, batas tile, tile kosong, block type yang dikenal,
dan jarak kurang dari tiga tile sebelum mengirim `block_update`. Server game
tetap menjadi validator final. `msg` memakai field packet client `text`, bukan
field lama `message`.

## Catatan operasional

- Jangan menjalankan `capture_*.py` ketika bot aktif.
- Jangan memulai bot `default` dari dashboard jika `stay_world.py` manual sedang
 berjalan dengan akun yang sama.
- Tiap bot baru memiliki command, state, dan log sendiri.
