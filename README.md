# Antigravity Discord Gateway

Gateway Discord bergaya **Hermes Agent** untuk mengontrol **Google Antigravity CLI (`agy`)** langsung dari Discord (HP maupun desktop) lewat dedicated channel atau DM.

## Fitur
- **Semua Channel atau Dedicated Channel & DM**: Secara default (jika `DISCORD_CHANNEL_IDS` dikosongkan), bot langsung aktif di **semua channel** di server (plus DM), dan tiap channel punya sesi `conversation_id` masing-masing.
- **Multi-Server & Multi-Bot (`@mention` Mode + Backfill)**: Aktifkan `DISCORD_REQUIRE_MENTION=true` dengan `DISCORD_FREE_RESPONSE_GUILD_IDS` (server utama bebas chat tanpa tag, server lain wajib `@mention`). Bot otomatis diam jika user men-tag bot lain (seperti `@Hermes`), dan otomatis menarik pesan terbaru yang terlewat (*missed-message backfill*) saat disummon atau setelah bot lain membalas inline.
- **Persistent Session (`--conversation`)**: Setiap channel menyimpan `conversation_id` sendiri di `state.json`, jadi chat nyambung terus.
- **Live Streaming & Auto-Split**: Progress dan jawaban di-stream real-time ke Discord, otomatis dipecah jika lebih dari 2000 karakter tanpa merusak blok kode.
- **Whitelist Keamanan**: Hanya `DISCORD_ALLOWED_USER_IDS` di `DISCORD_CHANNEL_IDS` (atau DM jika diizinkan) yang bisa memicu agent.
- **Lampiran File/Gambar**: File/screenshot yang dikirim di Discord otomatis diunduh ke `.gateway_uploads/` dan diteruskan ke Antigravity.
- **Share Hermes `SOUL.md`, `MEMORY.md`, & `USER.md` + `AGY_MEMORY.md`**: Bisa langsung pakai persona dan memori Hermes kamu (mode `readonly` anti-bentrok atau `readwrite`), dilengkapi persistent memory khusus Agy (`AGY_MEMORY.md`) serta MCP server (`agy_gateway/mcp_memory.py`) agar model bisa mandiri mencari sesi lama & menyimpan fakta lintas `/new`.
- **Cross-Channel `#mention`**: Mention `#channel-lain` (atau thread) di dalam chat, bot otomatis menarik pesan terakhir dari channel tersebut sebagai konteks.
- **Session Search (`/search` / `!search`) & Compact (`/compact` / `!compact`)**: Cari histori percakapan dari database FTS5 Antigravity (`conversation_summaries.db`) dan Hermes (`state.db`), atau ringkas sesi panjang ke context baru yang ringan.
- **Pemilihan Model Interaktif**: Ketik `/model` atau `!model` untuk membuka dropdown pilihan model (atau gunakan autocomplete langsung di `/model <nama>`).
- **Cek Sisa Limit (`/usage` / `/quota`)**: Pantau sisa kuota 5-jam dan mingguan untuk grup model Gemini (termasuk Argon) dan Claude/GPT langsung dari Discord, plus pemakaian context window aktif di `/status`.
- **Commands (`/` atau `!`)**: `/new`, `/compact`, `/stop`, `/status`, `/model`, `/mode`, `/workdir`, `/resume`, `/reload`, `/curator`, `/search`, `/usage`, `/quota`, `/help`.

---

## 1. Setup Discord Bot (Sekali Saja)
1. Buka [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Masuk tab **Bot** → **Add Bot** → **Reset Token** → simpan token-nya.
3. Di tab **Bot**, aktifkan **Message Content Intent** di bagian *Privileged Gateway Intents*.
4. Masuk tab **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `View Channels`, `Send Messages`, `Read Message History`, `Attach Files`
5. Buka URL yang muncul dan invite bot ke server Discord kamu.
6. Aktifkan **Developer Mode** di Discord app (Settings → Advanced), lalu klik kanan profilmu → **Copy User ID**, dan klik kanan channel tujuan → **Copy Channel ID**.

---

## 2. Konfigurasi & Jalankan Gateway

```bash
# 1. Jalankan wizard setup interaktif (mirip `hermes gateway setup`)
./.venv/bin/python gateway.py setup

# 2. Jalankan bot gateway
./.venv/bin/python gateway.py start
```

### Menjalankan 24/7 di Background (`systemd`)
Wizard `setup` bisa langsung membuatkan `agy-gateway.service`, atau jalankan:
```bash
./.venv/bin/python gateway.py service
```
Lalu aktifkan dengan:
```bash
sudo cp agy-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agy-gateway
sudo journalctl -u agy-gateway -f
```
