# 🏆 Crypto Spot Competition Tracker

A free, always-on tool that automatically finds **live crypto spot & wallet-swap trading competitions** and sends them to your **Telegram**, with a live dashboard. It runs by itself on GitHub twice a day — your laptop can be off.

There are **two modes**:

| Mode | What it is | Cost | When it runs |
|------|-----------|------|--------------|
| **A — Auto scan** | Finds competitions using plain rules (no AI). Updates the dashboard + Telegram. | **Free, forever** | Automatically, twice a day |
| **B — Smart scan** | Uses AI (Claude) to confirm exact dates & write a one-line note. Optional. | A few cents per click | Only when **you** click the button |

> You can use this tool **completely free** with just Mode A. Mode B is a bonus you can switch on later.

---

## Part 1 — Get it running (free, ~15 minutes, one time)

You don't need to know how to code. Just follow along.

### Step 1 — Make a free GitHub account
1. Go to [github.com](https://github.com) and sign up (free).
2. Create a new repository (a "project folder in the cloud"):
   - Click the **+** (top-right) → **New repository**.
   - Name it e.g. `comp-tracker`.
   - Choose **Public** (required for the free plan to run this automatically).
   - Click **Create repository**.
3. Upload the files: on the new repo page click **uploading an existing file**, then in your file explorer open the `crypto-comp-tracker` folder, select **everything inside it** (all the files *and* the `.github` folder — on Windows these are all visible), and drag them onto the GitHub page. Type a short message and click **Commit changes**.

> **Important:** upload the *contents* of `crypto-comp-tracker` so that `scanner.py` and the `.github` folder land at the **top level** of the repo — not inside a sub-folder. GitHub only runs the automation when `.github/workflows/` is at the repo root.

### Step 2 — Create your Telegram bot (2 minutes)
1. In Telegram, search for **@BotFather** and open a chat.
2. Send `/newbot`, follow the prompts, pick a name.
3. BotFather gives you a **token** that looks like `8375131758:AAHx...`. Copy it.

### Step 3 — Get your Telegram chat ID
1. In Telegram, search for **@userinfobot** and press **Start**.
2. It replies with your **Id** (a number like `1925514908`). Copy it.
3. **Important:** open a chat with *your own bot* (the one you made in Step 2) and press **Start** / send it any message. A bot can't message you until you've messaged it first.

### Step 4 — Add your secrets to GitHub
In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add these two:

| Name | Value |
|------|-------|
| `TELEGRAM_BOT_TOKEN` | the token from Step 2 |
| `TELEGRAM_CHAT_ID` | the number from Step 3 |

(Type the names **exactly** as shown.)

### Step 5 — Turn on the dashboard (GitHub Pages)
1. **Settings → Pages**.
2. Under **Build and deployment → Source**, choose **Deploy from a branch**.
3. Set **Branch** to **main** and folder to **/ (root)**, then click **Save**.
4. Your dashboard will live at `https://YOUR-USERNAME.github.io/comp-tracker/` a minute or two after the first scan runs.

### Step 6 — Turn on the automation
1. Go to the **Actions** tab. If it asks, click **"I understand my workflows, enable them."**
2. Click **"Mode A — auto scan"** on the left → **Run workflow** → **Run workflow** (green button).
   - **Do this once now** so you don't have to wait 12 hours for the first scan.
3. In ~3–5 minutes you'll get a Telegram message and your dashboard will be live. 🎉

**That's it.** From now on it scans automatically at **9:00 AM and 9:00 PM Pakistan time**, every day, for free.

---

## Part 2 — Reading the dashboard

- **Tabs**: **Live** (running now) · **Upcoming** (announced, not started) · **Ended** (kept 7 days).
- **Search, Sort & Group**: search any name/venue/prize; sort by time left, prize, venue, safety, type or newest; group by time left, venue, tier or type.
- **Countdown chip** on each card: red = under 24 hours, amber = under 3 days, green = later, grey = ended/dates unknown. The **↗** opens the official page in one tap; **Details ▾** shows dates, notes and warnings.
- **Click a venue name** (e.g. *Binance*) to see only that venue; clear with the banner's ✕.
- **⚠ verify dates** means a date was found but its timezone wasn't stated — check the official page (or ask for a smart scan).
- **🔔 Health** (top-right) opens the Source Health page: every source's status in plain English. Nothing is ever hidden from you.
- **⚙ Settings** holds the optional AI smart-scan controls (model choice + GitHub token).
- **PKT/UTC** and **Light/Dark** toggles are in the toolbar. Times default to Pakistan time.
- 📱 **On your phone**: open the dashboard, then *Add to Home screen* — it installs like an app with its own icon.
- The page quietly re-checks for new data every 10 minutes, so a tab left open stays current.

---

## Part 3 — Optional: turn on Smart Scan (Mode B, costs a few cents)

Smart scan uses AI to nail down exact dates and add a one-line note. **Only runs when you click.** Skip this whole section if you don't want it — Mode A works fine without it.

### 3a — Get an Anthropic API key
1. Go to [console.anthropic.com](https://console.anthropic.com) and sign up (this is separate from a Claude subscription).
2. Add a little credit under **Billing** — **$5 is plenty** (each smart scan costs only a few cents).
3. Under **API Keys**, create a key (starts with `sk-ant-...`). Copy it.
4. In your GitHub repo: **Settings → Secrets → Actions → New secret**, name it `ANTHROPIC_API_KEY`, paste the key.

### 3b — Make a token so the dashboard button can start a scan
The button needs permission to start the smart-scan job. This uses a **fine-grained token** limited to just that one action.
1. GitHub → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Repository access:** Only select repositories → pick your `comp-tracker` repo.
3. **Permissions → Repository permissions → Actions → Read and write.**
4. Generate and copy the token.
5. On your dashboard, open **⚙ Settings**, type your `owner/repo` (e.g. `yourname/comp-tracker`) under *Connection*, paste the token, click **Save for this session**.
   - The token stays **only in your browser tab** (it's forgotten when you close it) and can do nothing except start this one job.

### 3c — Use it
Pick **Haiku** (cheapest) or **Sonnet** (smarter) from the dropdown and click **Run smart scan**. Results appear on the dashboard in 1–3 minutes (refresh the page).

> **Cost reminder:** Mode A is always free. Mode B costs roughly **$0.05–0.15** per click on Haiku, **$0.30–0.80** on Sonnet. You choose the model each time. Tokens/keys expire eventually — if the button says "token rejected," just make a new fine-grained token (Step 3b) and save it again.

---

## Part 4 — Everyday tweaks

### Add or remove a competition source
Open **`sources.yaml`** and edit. To pause a source, set `enabled: false`. To add one, copy a block:
```yaml
  - name: "My New Exchange"
    venue: SomeVenue
    method: html            # html | json_api | telegram | playwright | bybit_api
    url: "https://example.com/announcements"
    reliability: mixed
    enabled: true
    stale_days: 5
    parse_notes: "what to expect here"
```
Commit the change — the next scan uses it.

### Change the schedule
In `.github/workflows/scan.yml`, edit the `cron` line. It's in **UTC**. `0 4,16 * * *` = 04:00 & 16:00 UTC = 09:00 & 21:00 PKT. [crontab.guru](https://crontab.guru) helps.

### If a source says "blocked"
Some exchanges block requests coming from data centers (GitHub's servers). That's normal and honest — the other sources keep working. Options: leave it (the aggregator often catches the same competitions), or host the scan from a different region/proxy (advanced). A `blocked` source is **not** a broken tool.

### If the scanner ever crashes
You'll get a Telegram "⚠ scan crashed" message, and the dashboard header turns red if data is more than ~26 hours old — so you always know if something needs attention.

### If you pause the project for 2+ months
GitHub auto-disables schedules on inactive repos. Just open the **Actions** tab and re-enable, then run once manually.

---

## What each file is (for the curious)

| File | Job |
|------|-----|
| `sources.yaml` | the list of places to look |
| `config.yaml` | filters, keywords, safety tiers, timings, models |
| `scanner.py` | the boss — runs the scan, both modes |
| `fetchers.py` | downloads pages (with retries + browser fallback) |
| `parsers.py` | reads pages into competition records + finds dates |
| `classify.py` | keeps only spot/onchain comps; rates venue safety |
| `smart.py` | Mode B only — the AI enrichment |
| `render.py` | writes the dashboard's data file (`comp-data.js`) each scan |
| `notify.py` | sends Telegram alerts |
| `index.html` | the dashboard app (static — updated only when the design changes) |
| `comp-data.js` | the data the dashboard displays (auto-updated every scan) |
| `_ds/` | the dashboard's design system (styles + self-hosted fonts) |
| `manifest.json` / `icon-*.png` | phone "Add to Home screen" app identity |
| `data.json` / `seen.json` | the pipeline's results & memory (auto-managed) |

---

## Honest limitations (please read once)

- **Announcement date ≠ competition date.** Listings often show only when a post was published, not when the competition ends. Mode A opens each new competition's page and searches for real dates, but it only marks them **confirmed** when the page clearly states a timezone. Everything else shows **⚠ verify** — run a smart scan or check the official page.
- **Some sources may be blocked from GitHub's servers** (Binance and Cloudflare/Akamai-fronted sites are the usual suspects). The dashboard's health strip always tells you the truth.
- **This is not financial advice.** It finds competitions; it doesn't tell you to trade. Always confirm details on the official page before joining.
