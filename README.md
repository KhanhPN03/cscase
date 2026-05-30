# 🎮 CSGOCASES Promo Code Monitor

> **Fully automated, 100% free** — monitors Reddit, Twitter/X, YouTube, Telegram, and the CSGOCASES website for promo codes, then sends instant Gmail alerts. Runs on GitHub Actions every 5 minutes with zero cost.

---

## ✨ Features

| Feature | Detail |
|---|---|
| 📡 **5 platform monitors** | CSGOCASES site, Reddit, Twitter/X, YouTube, Telegram |
| 🤖 **Fully automated** | GitHub Actions cron every 5 minutes |
| 📧 **Rich HTML emails** | Dark-mode email with code highlighted, source link, snippet |
| 💾 **State persistence** | `state.json` committed back to repo — no database needed |
| 🔁 **Deduplication** | Never sends the same alert twice |
| 🔍 **Smart extraction** | Context-aware regex finds codes even in messy text |
| 💸 **100% free** | No paid APIs, no Docker, no external services |
| 🔧 **Configurable** | All targets overridable via environment variables |

---

## 🗂️ Project Structure

```
cscase/
├── main.py                          # Orchestrator entry point
├── monitors/
│   ├── base_monitor.py              # Abstract base + HTTP helpers
│   ├── csgocases_site_monitor.py    # Scrapes csgocases.com directly
│   ├── reddit_monitor.py            # Reddit JSON API (no key needed)
│   ├── twitter_monitor.py           # Twitter via Nitter RSS (no key)
│   ├── youtube_monitor.py           # YouTube Atom RSS (no key)
│   └── telegram_monitor.py         # Telegram public channels
├── utils/
│   ├── code_extractor.py            # Regex promo code detection engine
│   ├── email_sender.py              # Gmail SMTP notifier
│   ├── logger.py                    # Structured colored logger
│   └── state.py                     # JSON state persistence
├── state.json                       # Persisted state (auto-updated)
├── requirements.txt
├── .env.example
└── .github/workflows/monitor.yml    # GitHub Actions cron workflow
```

---

## 🚀 Quick Setup (15 minutes)

### Step 1 — Fork / Clone

```bash
git clone https://github.com/YOUR_USERNAME/cscase.git
cd cscase
```

### Step 2 — Generate a Gmail App Password

1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** (required)
3. Search for **"App passwords"** → Create one
4. App name: `CSGOCASES Monitor`
5. Copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

### Step 3 — Add GitHub Repository Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `GMAIL_USER` | `your_email@gmail.com` |
| `GMAIL_APP_PASS` | The 16-char app password |
| `NOTIFY_EMAIL` | Where alerts are sent (can be same address) |

### Step 4 — Enable the workflow

Push to `main` — GitHub Actions will auto-enable. The first run fires within 5 minutes.

To trigger immediately: **Actions → CSGOCASES Promo Code Monitor → Run workflow**

### Step 5 — Test your email

In the workflow, click **Run workflow** and set `test_email` = `true`. You'll get a confirmation email within seconds.

---

## 🏃 Running Locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in the env file
cp .env.example .env
# Edit .env with your Gmail credentials

# 3. Normal run
python main.py

# 4. Dry run (no emails sent)
python main.py --dry-run

# 5. Send a test email
python main.py --test-email
```

---

## ⚙️ Configuration

All settings are controlled via environment variables (or the `.env` file locally).

| Variable | Default | Description |
|---|---|---|
| `GMAIL_USER` | *(required)* | Your Gmail address |
| `GMAIL_APP_PASS` | *(required)* | Gmail App Password |
| `NOTIFY_EMAIL` | Same as GMAIL_USER | Alert recipient |
| `REDDIT_SUBREDDITS` | `CSGOcases,GlobalOffensive,csgo,csgobetting` | Subreddits to watch |
| `TWITTER_ACCOUNTS` | `csgocases,CSGOCasescom` | Twitter accounts (no @) |
| `TWITTER_SEARCH_TERMS` | `csgocases promo code,...` | Twitter search queries |
| `NITTER_INSTANCES` | `nitter.privacydev.net,...` | Nitter fallback chain |
| `YOUTUBE_CHANNELS` | `UCvNgM...:CSGOCASES Official` | `CHANNEL_ID:Name` pairs |
| `TELEGRAM_CHANNELS` | `csgocases,csgo_promo_codes` | Public Telegram channels |
| `REQUEST_TIMEOUT` | `20` | HTTP timeout (seconds) |
| `STATE_FILE` | `state.json` | State file path |

---

## 📧 Email Preview

The alert email includes:
- 🏆 **Promo code(s)** — large, highlighted, copy-paste ready
- 📡 Source platform & direct link to the original post
- ✍️ Author / account name
- 📝 Context snippet from the post
- 🔗 One-click button to open CSGOCASES

---

## 🔍 How Code Detection Works

The `code_extractor.py` runs two passes:

1. **Context-aware** — looks for keywords like `promo code`, `use code`, `redeem` immediately followed by an ALL-CAPS token
2. **Broad scan** — catches standalone ALL-CAPS 4-20 character tokens if pass 1 finds nothing

A blocklist of 100+ common false positives (`FREE`, `CSGO`, `HTTP`, etc.) prevents noise.

---

## 📡 Platform Coverage

| Platform | Method | API Key? |
|---|---|---|
| CSGOCASES Website | Direct HTML scrape | ❌ None |
| Reddit | JSON API (`/new.json`) | ❌ None |
| Twitter/X | Nitter RSS (multi-instance) | ❌ None |
| YouTube | Atom RSS feed | ❌ None |
| Telegram | Public channel HTML (`t.me/s/`) | ❌ None |

---

## 💰 Cost Breakdown

| Component | Cost |
|---|---|
| GitHub Actions (cron, 5-min interval) | **$0** (2,000 free minutes/month; each run ≈ 30s) |
| Gmail SMTP | **$0** |
| All scraped APIs | **$0** |
| **Total** | **$0 / month** |

> **Note:** 5-min intervals × 24h × 30 days = ~8,640 runs/month × ~0.5 min = ~4,320 minutes/month — well within the free 2,000-minute limit for public repos (**public repos get unlimited free minutes**).

---

## 🛠️ Extending the Monitor

### Add a new monitor

1. Create `monitors/my_platform_monitor.py` extending `BaseMonitor`
2. Implement `fetch_new_items(state)` returning a list of `FindingResult`
3. Add your class to the `MONITORS` list in `main.py`

### Add a new subreddit

Set the `REDDIT_SUBREDDITS` secret in GitHub to include your subreddit.

---

## 🐛 Troubleshooting

| Problem | Solution |
|---|---|
| No emails received | Check `GMAIL_APP_PASS` — use an App Password, NOT your Gmail password |
| `SMTPAuthenticationError` | 2-Step Verification must be enabled on your Google account |
| Workflow not running | Check Actions tab → ensure workflow isn't disabled |
| `state.json` not committed | Ensure the workflow has `contents: write` permission |
| Nitter feeds empty | Nitter instances go down — add more to `NITTER_INSTANCES` |
| False positives | Edit `_BLOCKLIST` in `utils/code_extractor.py` |

---

## 📄 License

MIT — free to use, modify, and deploy.
