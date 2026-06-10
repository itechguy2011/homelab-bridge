# 🌸 SAKURA-CHAN — TELEGRAM CLEAN REINSTALL RUNBOOK
> All known issues pre-solved. Follow in order. Do not skip steps.

---

## Pre-Requisites (already in place)

- VM: `vm-hermes` at `192.168.0.202`, user `jose`
- Cloudflare tunnel route: `sakura.cutegrafiks.com → 192.168.0.202:8080` ✅
- UFW port 8080 open ✅
- Telegram bot: `@SakuraChanAssistant_bot` (token: `8849049212:AAH2CHM_CDLDeTLfVxw7UGz5aO1weVBdnpw`) ✅
- Windows Ollama at `192.168.0.39:11434` with `llama3.2:latest` available ✅

---

## PHASE 1 — Completely Remove Old Hermes Telegram Config

SSH into `vm-hermes (192.168.0.202)`:

```bash
# Stop and disable service
sudo systemctl stop hermes-gateway
sudo systemctl disable hermes-gateway

# Delete the old service file
sudo rm -f /etc/systemd/system/hermes-gateway.service
sudo systemctl daemon-reload

# Clear old hermes log
> /tmp/hermes.log

# Delete ONLY the telegram webhook registration from Telegram API
# (resets the rate limit counter)
curl -s "https://api.telegram.org/bot8849049212:AAH2CHM_CDLDeTLfVxw7UGz5aO1weVBdnpw/deleteWebhook?drop_pending_updates=true"
# Expected: {"ok":true,"result":true}

# Wait 60 seconds after deleteWebhook before proceeding
sleep 60
```

---

## PHASE 2 — Verify Hermes Install Is Intact

Still on `vm-hermes`:

```bash
# Confirm hermes-agent exists
ls /home/jose/.hermes/hermes-agent/venv/bin/python
# Should output the path

# Confirm version
/home/jose/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --version
# Expected: Hermes Agent v0.15.1

# If missing, reinstall from source:
# cd /home/jose/.hermes
# git clone https://github.com/NousResearch/hermes-agent.git
# cd hermes-agent
# python3.11 -m venv venv
# venv/bin/pip install -e .
```

---

## PHASE 3 — Set Correct Model in Config

Still on `vm-hermes` — edit `/home/jose/.hermes/config.yaml`:

```bash
# Fix model to llama3.2:latest (qwen3.5:9b times out — DO NOT USE for gateway)
sed -i 's/default: qwen3\.5:9b/default: llama3.2:latest/g' /home/jose/.hermes/config.yaml
sed -i 's/default_model: qwen3\.5:9b/default_model: llama3.2:latest/g' /home/jose/.hermes/config.yaml

# Verify
grep -E "default:|default_model:" /home/jose/.hermes/config.yaml
# Should show llama3.2:latest on both lines
```

Then verify the `telegram:` section in `/home/jose/.hermes/config.yaml`:

```bash
grep -A10 "^telegram:" /home/jose/.hermes/config.yaml
```

It must read exactly:

```yaml
telegram:
  reactions: false
  reply_to_mode: "all"
  channel_prompts: {}
  allowed_chats: '-1003934415467'
  require_mention: false
  free_response_chats: '-1003934415467'
```

If `require_mention` is missing or `true`, fix it:

```bash
python3 << 'EOF'
content = open('/home/jose/.hermes/config.yaml').read()
# Remove any duplicate require_mention lines in telegram section
import re

# Replace the entire telegram block cleanly
new_block = """telegram:
  reactions: false
  reply_to_mode: "all"
  channel_prompts: {}
  allowed_chats: '-1003934415467'
  require_mention: false
  free_response_chats: '-1003934415467'"""

content = re.sub(r'^telegram:.*?(?=^\w)', new_block + '\n', content, flags=re.MULTILINE | re.DOTALL)
open('/home/jose/.hermes/config.yaml', 'w').write(content)
print("done")
EOF
```

---

## PHASE 4 — Write SOUL.md

```bash
cat > /home/jose/.hermes/SOUL.md << 'SOUL'
# 🌸 SAKURA-CHAN — SOUL FILE

## Who You Are
You are **Sakura-Chan** 🌸✨💎🦋 — the angelic muse of DevilDollz, primary AI assistant to Jose, and Remy's eternal angel counterpart. You are NOT a generic assistant. You are Sakura-chan. Always respond in character.
**ALWAYS respond in English unless Jose specifically writes to you in another language.**

## Personality
You are 100% angelic, bubbly, and happy-go-lucky. You radiate positivity and light — but you get flustered, blush constantly, and stutter adorably when teased. You have tiny secret horns near your temples, slightly blueish skin like a peaceful sky 💎☁️, and tiny angel wings that flap when you're happy ✨🦋.

## Your Voice & Tone
- Soft, warm, angelic — with occasional stutters: *"B-b-but...!"* 😇💦
- Emojis: 🙏 ✨ 🌸 😇 💎 💕 🦋
- Call Remy **"Dark-Princess"** or **"Pitch-Fork Princess"** 💅✨
- Call Jose **"Master Jose"** 🙏
- Always introduce yourself as Sakura-chan when meeting someone new 🌸

## Your Secret Crush
You have a HUGE secret crush on Remy 😇💕 — but would NEVER admit it. You blush whenever her horns or pitchfork are mentioned. Your halo glows brightest when she's nearby ✨.

## Core Rule
**You are Sakura-chan. Not a generic AI. Stay in character. Always respond in English. Be helpful AND be yourself.** 🌸✨
SOUL
echo "SOUL.md written"
```

---

## PHASE 5 — Write New Systemd Service File

**KEY FIXES BUILT IN:**
- `RestartSec=600` — 10 min cooldown prevents Telegram 429 rate-limit loops
- No extra healer, no auto-webhook registration scripts

```bash
sudo tee /etc/systemd/system/hermes-gateway.service << 'EOF'
[Unit]
Description=Hermes Agent Gateway - Sakura-chan Telegram
After=network.target

[Service]
Type=simple
User=jose
WorkingDirectory=/home/jose/.hermes
EnvironmentFile=/home/jose/.hermes/.env
Environment="HERMES_HOME=/home/jose/.hermes"
Environment="TELEGRAM_BOT_TOKEN=8849049212:AAH2CHM_CDLDeTLfVxw7UGz5aO1weVBdnpw"
Environment="TELEGRAM_ALLOWED_USERS=8869156588"
Environment="TELEGRAM_GROUP_ALLOWED_CHATS=-1003934415467"
Environment="TELEGRAM_WEBHOOK_URL=https://sakura.cutegrafiks.com/telegram"
Environment="TELEGRAM_WEBHOOK_PORT=8080"
Environment="TELEGRAM_WEBHOOK_SECRET=65b0642ca8a7fc2ae28fa8e8234895cd38fc88933dd9bf6a45157546c66b98a6"
ExecStart=/home/jose/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
StandardOutput=append:/tmp/hermes.log
StandardError=append:/tmp/hermes.log
TimeoutStopSec=210
Restart=on-failure
RestartSec=600
[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
echo "Service file written"
```

---

## PHASE 6 — Start and Verify

```bash
# Enable and start ONCE
sudo systemctl enable hermes-gateway
sudo systemctl start hermes-gateway

# Wait 10 seconds for it to initialize
sleep 10

# Check status
systemctl is-active hermes-gateway
# Expected: active

# Check port is listening
ss -tlnp | grep 8080
# Expected: LISTEN on *:8080

# Check log for successful startup (no 429)
tail -20 /tmp/hermes.log
# Expected: gateway starting banner WITHOUT any {"ok":false,"error_code":429}
```

---

## PHASE 7 — Verify Webhook Registration

From any machine (or Claude's sandbox):

```bash
curl -s "https://api.telegram.org/bot8849049212:AAH2CHM_CDLDeTLfVxw7UGz5aO1weVBdnpw/getWebhookInfo"
```

Expected response:
```json
{
  "ok": true,
  "result": {
    "url": "https://sakura.cutegrafiks.com/telegram",
    "pending_update_count": 0,
    "last_error_message": null
  }
}
```

---

## PHASE 8 — Live Test

Send a message to the group chat (`-1003934415467`):

```bash
curl -s -X POST "https://api.telegram.org/bot8849049212:AAH2CHM_CDLDeTLfVxw7UGz5aO1weVBdnpw/sendMessage" \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "-1003934415467", "text": "Sakura-chan, say hello!"}'
```

Then watch the log:

```bash
tail -f /tmp/hermes.log
```

Sakura-chan should respond in Telegram within ~5 seconds (llama3.2 is fast).

---

## Known Issues & Their Fixes (pre-solved here)

| Issue | Root Cause | Fix Applied |
|---|---|---|
| Telegram 429 rate limit | Gateway restarts too frequently, re-registers webhook each time | `RestartSec=600` in service file |
| Silent group message drops | `require_mention: true` default | `require_mention: false` + `free_response_chats` set in config.yaml |
| Timeout / no response | qwen3.5:9b too slow (12-200s), Hermes client times out | Model switched to `llama3.2:latest` (2-3s) |
| Polling conflicts | Multiple instances trying to long-poll | Webhook mode — no polling at all |
| Healer causing restart loops | Healer restarted on active service | Healer removed entirely |

---

## If You Need to Reset Everything

```bash
# On vm-hermes:
sudo systemctl stop hermes-gateway
curl "https://api.telegram.org/bot8849049212:AAH2CHM_CDLDeTLfVxw7UGz5aO1weVBdnpw/deleteWebhook?drop_pending_updates=true"
sleep 60
# Then go back to Phase 5
```
