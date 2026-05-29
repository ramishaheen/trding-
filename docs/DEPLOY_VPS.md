# Run the bot 24/7 on your Hostinger VPS

This puts the bot on an always-on server, so the safety kill-switch and risk
limits keep running even when your laptop is off. You then open the dashboard
from your phone or computer.

> Use a **fresh** BingX API key (spot only, withdrawals OFF). Start in paper
> mode; real trading stays OFF until you press TURN ON in the dashboard.

## 1. Connect to your VPS
In Hostinger **hPanel → VPS**, note your server's **IP address** and **root
password** (or set up SSH). Then from your computer:

- **Mac/Linux:** open Terminal and run `ssh root@YOUR_VPS_IP`
- **Windows:** open PowerShell and run `ssh root@YOUR_VPS_IP`
  (or use the **Browser terminal** button in hPanel — no install needed)

Type the password when asked.

> Tip: Hostinger offers a **Ubuntu 24.04 with Docker** template. If you used it,
> you can skip step 2.

## 2. Install Docker (once)
```bash
curl -fsSL https://get.docker.com | sh
apt-get update && apt-get install -y git
```

## 3. Get the project
```bash
git clone https://github.com/ramishaheen/trding-.git
cd trding-
git checkout claude/modest-carson-pVcY9
```

## 4. Point your subdomain at the VPS
In your DNS provider for **ohmycompany.ai** (Hostinger hPanel → Domains → DNS, or
wherever the domain is managed), add an **A record**:
- **Type:** A
- **Name / Host:** `trade`   (this makes `trade.ohmycompany.ai`)
- **Value / Points to:** your VPS IP address
- **TTL:** default

Wait a few minutes for it to take effect (you can check with `ping trade.ohmycompany.ai`).

## 5. Add your settings
```bash
cp .env.example .env
nano .env
```
Fill in (save with **Ctrl+O, Enter, Ctrl+X**):
```
FREQTRADE__EXCHANGE__KEY=your_fresh_bingx_key
FREQTRADE__EXCHANGE__SECRET=your_fresh_bingx_secret
DASHBOARD_PASSWORD=pick-a-long-unique-password
DASHBOARD_COOKIE_SECRET=any-long-random-string
DASHBOARD_DOMAIN=trade.ohmycompany.ai
```
`DASHBOARD_PASSWORD` is the single password anyone must enter to log in — it's
what stops strangers from reaching your TURN ON / STOP buttons.

## 6. Start everything (with the public HTTPS proxy)
```bash
docker compose --profile public up -d
```
First time downloads/builds (a few minutes) and Caddy automatically gets a free
HTTPS certificate for your subdomain. Check it's up: `docker compose ps`.

## 7. Lock down the firewall
In **hPanel → VPS → Firewall**, allow only:
- **22** (SSH)
- **80** and **443** (the website / HTTPS)

Block everything else. The dashboard itself is bound to localhost and only
reachable through the HTTPS proxy — not as a raw port.

## 8. Open it
On any browser (phone or computer): **https://trade.ohmycompany.ai**
You'll get the login page → type your password → the live dashboard. It's in
**paper mode** until you turn real trading on.

> No domain yet? You can still reach it securely without one: run
> `ssh -L 8050:localhost:8050 root@YOUR_VPS_IP` from your computer and open
> `http://localhost:8050`.

## Turning real trading ON / OFF
See `docs/GO_LIVE.md`. In short: in `.env` set `LIVE_BROWSER_TRADING_ENABLED=on`,
restart (`docker compose --profile live-browser up -d`), then in the dashboard
press **▶ TURN ON** and type `ARM`. **⏸ TURN OFF** or **■ STOP** anytime.

## Everyday commands
```bash
docker compose ps                 # what's running
docker compose logs -f dashboard  # watch the dashboard
docker compose logs -f executor   # watch trade decisions (live profile)
docker compose restart            # restart everything
docker compose down               # stop everything
git pull && docker compose up -d --build   # update to the latest code
```

## Safety recap
- Paper mode by default; real orders need the deploy flag **and** the ARM switch
  **and** governor approval. The kill switch overrides all of it.
- Keep your `.env` private (it holds your keys + dashboard password). It is never
  committed to git.
