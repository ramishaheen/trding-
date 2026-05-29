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

## 4. Add your settings
```bash
cp .env.example .env
nano .env
```
Fill in (arrow-keys to move, then save with **Ctrl+O, Enter, Ctrl+X**):
```
FREQTRADE__EXCHANGE__KEY=your_fresh_bingx_key
FREQTRADE__EXCHANGE__SECRET=your_fresh_bingx_secret
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=pick-a-long-unique-password
```
The dashboard password is important — it's what stops strangers from reaching
your TURN ON / STOP buttons.

## 5. Start everything
```bash
docker compose up -d
```
First time downloads and builds (a few minutes). It keeps running after you log
out. Check it's up: `docker compose ps`.

## 6. Lock down access (important)
Only the dashboard (port **8050**) needs to be reachable; everything else is
internal. In **hPanel → VPS → Firewall**, allow only:
- **port 22** (SSH) — ideally from your IP only
- **port 8050** (dashboard) — ideally from your IP only

Block everything else. (FreqUI on 8080 is already bound to localhost and not
public.)

## 7. Open the dashboard
On any browser: **http://YOUR_VPS_IP:8050**
Log in with the `DASHBOARD_USER` / `DASHBOARD_PASSWORD` you set. You'll see the
live screen. It's in **paper mode** until you decide otherwise.

> More secure option (no public port at all): instead of opening 8050, run
> `ssh -L 8050:localhost:8050 root@YOUR_VPS_IP` from your computer, then open
> `http://localhost:8050`. The dashboard is then only reachable through your SSH
> session.

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
