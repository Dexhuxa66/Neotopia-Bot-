Neotopia Handoff Bundle
=======================

Isi utama:
- MASTER_PROMPT_CURRENT.md: konteks proyek terbaru untuk AI berikutnya.
- stay_world.py: bot utama.
- neotopia_tool.py: generator temp-mail dan exporter account bot.
- dashboard_server.py + dashboard/: dashboard REST/WebSocket.
- tests/: test fitur bot/dashboard.
- DASHBOARD.md dan diag_path.py: dokumentasi/diagnostik.

Credential sengaja TIDAK disertakan:
- guest_account*.json
- accounts.json
- idtoken.txt, refresh_token.txt, uid.txt, headers.txt
- chrome-profile/
- bot_state.json, stay_world.log, stay_cmd.txt

Setelah mengekstrak bundle:
1. Baca MASTER_PROMPT_CURRENT.md.
2. Salin credential/account secara lokal jika memang berwenang; jangan masukkan ke git.
3. Jalankan test: python3 -m pytest -q
4. Cek bot live dengan pgrep -af '^python3 stay_world.py'.
