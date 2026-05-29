# Run the dashboard on your computer (5 steps)

You'll see the dashboard in your browser at **http://localhost:8050**.

### Before you start
Install **Docker Desktop** (free): https://www.docker.com/products/docker-desktop/
Open it once so it's running (you'll see the whale icon).

### Step 1 — Get the project
Open a terminal (Mac: *Terminal*; Windows: *PowerShell*) and run:
```bash
git clone https://github.com/ramishaheen/trding-.git
cd trding-
git checkout claude/modest-carson-pVcY9
```

### Step 2 — Add your settings file
```bash
cp .env.example .env
```
Open `.env` in any text editor and paste your BingX keys into these two lines:
```
FREQTRADE__EXCHANGE__KEY=your_api_key
FREQTRADE__EXCHANGE__SECRET=your_secret_key
```
> 🔒 This file stays on your computer and is never uploaded.
> Please use **freshly created** BingX keys (spot only, withdrawals off).

### Step 3 — Start everything
```bash
docker compose up -d
```
First time takes a few minutes (it downloads and builds). It stays running in the background.

### Step 4 — Open the dashboard
Go to **http://localhost:8050** in your browser. 🎉
(It refreshes itself every few seconds. Hover the little **?** icons for plain-English help.)

### Step 5 — When you're done
```bash
docker compose down
```
This stops everything safely.

---

### Good to know
- **Paper mode by default.** The bot does *not* trade real money until you deliberately turn it on (see `docs/BROWSER_EXECUTION.md`). Until then the dashboard shows live market data with practice trades.
- **The big red STOP button** halts all trading instantly.
- **Nothing is published online.** Everything runs on your machine.

### If something looks off
- Dashboard won't open? Make sure Docker Desktop is running, then `docker compose up -d` again.
- See the logs: `docker compose logs -f dashboard`
- Restart just the dashboard: `docker compose restart dashboard`
