# Internship Application Automation

Two-agent pipeline that researches each company, writes a personalized
application email around a fixed core pitch (your RedBox / cybersecurity /
agentic-AI story), sends it with your CV attached via Gmail, and tracks
everything in a local dashboard.

## How it works

```
companies.xlsx / .csv
        │
        ▼
 ┌─────────────────┐     ┌──────────────────┐     ┌─────────┐
 │  Research Agent  │ ──▶ │   Writer Agent   │ ──▶ │  Mailer │ ──▶ Gmail
 │  (scrapes site,  │     │ (core pitch +    │     │ (SMTP + │
 │  matches 0+      │     │  0-2 short extra │     │  CV     │
 │  extra mentions) │     │  mentions woven  │     │  attach)│
 └─────────────────┘     │  in naturally)   │     └─────────┘
                          └──────────────────┘
        │                         │                    │
        └─────────────────────────┴────────────────────┘
                                   ▼
                         applications.db (SQLite)
                                   ▼
                    Dashboard (python dashboard/app.py)
```

Your **core identity** (RedBox project, CTF wins, professional experience,
the AI-from-security-passion story) is fixed in `specializations.json` and
appears in every email, unchanged. The research agent only decides whether
0, 1, or 2 short **extra mentions** (cloud / software dev / data — or
whatever you configure) genuinely fit a given company, and the writer agent
weaves those in as a sentence or two — it never restructures or shrinks the
core pitch.

## Pipeline architecture (why it's fast)

The three stages above don't run one-at-a-time per company anymore — they
run as an **overlapping pipeline**, decoupled by small queues:

```
                 ┌───────────────────────┐     ┌───────────────────────┐
companies ──────▶│ research pool (N       │────▶│ writer pool (M         │───▶ ready_queue ───▶ sender
                 │ threads): scrape site  │     │ threads): draft email  │    (max 3 in-flight)  (main thread,
                 │ + AI research call      │     │ via AI writer call     │                        paced sends)
                 └───────────────────────┘     └───────────────────────┘
                         always working, never blocked on the send pacing delay
```

**Why this matters:** the old version did scrape → research call → write
call → send → *sleep 45–120s* → repeat, all strictly in order — most of the
runtime was spent doing nothing but sleeping for the anti-spam delay, with
every company's scrape/AI latency piled on top of that, serially. Now, the
research and writer pools keep preparing the *next* companies in the
background while the sender is asleep out that mandatory delay. By the time
the delay ends, the next email is usually already fully drafted and waiting
— the sender rarely has to wait on scraping or AI at all, only on the
pacing itself (which is the one wait that actually needs to happen).

**Resumable by default:** every finished research result and drafted email
is saved the instant it's ready, both to `applications.db` and to a small
per-company JSON file under `cache/`. If you stop the script, hit the daily
send cap, or it crashes, re-running it reuses that work instantly instead
of re-scraping / re-calling the AI — this is the biggest speed-up on any
second run, and it also means research/writing keeps prepping tomorrow's
batch in the background even after today's send cap is hit.

**Safe by design:**
- Bounded queues (`READY_QUEUE_SIZE`) keep memory flat regardless of list
  size — nothing accumulates unbounded in memory.
- Every stage is wrapped so one bad company (dead website, malformed AI
  response, bad email address) can never crash the run or get silently
  dropped — it's marked `failed`/`retry_later` and the pipeline continues.
- Transient errors (network blips, rate limits, timeouts) are retried with
  backoff before falling back, instead of immediately degrading.
- SQLite runs in WAL mode with a busy-timeout so the concurrent worker
  threads never collide on the database.
- Every company ends in exactly one outcome; the run summary's counts are
  cross-checked against the input list so nothing is ever silently lost.

**Tuning** (in `.env`, see `.env.example`): `RESEARCH_WORKERS` (default 3),
`WRITER_WORKERS` (default 2), `READY_QUEUE_SIZE` (default 3). The defaults
are a safe, conservative balance — raise `RESEARCH_WORKERS` if you have a
long company list and want research to finish well ahead of sending; there's
rarely a reason to raise `READY_QUEUE_SIZE` since drafting far ahead of the
pacing delay has no benefit.

## Setup (one-time)

1. **Install Python 3.10+** if you don't have it already.

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure your `.env` file:**
   ```bash
   cp .env.example .env
   ```
   Then open `.env` and fill in:
   - `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com)
   - `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` — see instructions inside `.env.example`
     (you need a Gmail **App Password**, not your normal password)
   - `YOUR_NAME`, `YOUR_TARGET_ROLE`
   - `CV_FILE_PATH` — path to your CV PDF (place it in this folder and just
     name it, e.g. `CV_FILE_PATH=./my_cv.pdf`)

4. **Add your companies.** Edit `companies_template.csv` (or rename it
   `companies.xlsx` and use Excel) with your real list. Required columns:
   `company_name`, `email`, `website` (website is optional but strongly
   recommended — without it, the research agent has nothing to read and
   falls back to a generic email).

5. **(Optional) Customize your pitch further.** `specializations.json`
   already contains your core pitch and three extra-mention categories
   (cloud, software_dev, data). Edit the wording anytime, or add more
   categories following the same structure.

That's it — steps 1-4 are the only setup required. Everything else runs
automatically.

## Running it

**Test first with a dry run** (generates emails but sends nothing):
```bash
python main.py --dry-run --limit 3
```
Check the dashboard (see below) to review the drafted emails before sending
anything for real.

**Send for real:**
```bash
python main.py
```

Useful flags:
- `--companies path/to/file.xlsx` — use a different companies file (default: `companies.xlsx`)
- `--limit N` — only process the first N companies (good for testing)
- `--dry-run` — research + write, but don't send
- `--research-workers N` / `--writer-workers N` — override the `.env` defaults
  for this run (see "Pipeline architecture" above)

The script is safe to stop (Ctrl+C) and re-run at any time: already-`sent`
companies are automatically skipped, and any company already researched or
drafted is loaded from the on-disk cache instead of redone (tracked in
`applications.db` + `cache/`), so you never double-send and never redo
work. If the daily send cap is hit, sending stops but research/writing keep
running in the background to prep for tomorrow, and the next run picks up
exactly where it left off.

## The dashboard

```bash
python dashboard/app.py
```
Then open **http://127.0.0.1:5050**. Shows:
- Live counts: sent, failed, bounced, pending, etc.
- Every company's status, matched extra mentions, and subject line
- Click into any company for the full timeline: what the research agent
  found, why it matched (or didn't match) an extra mention, the generated
  email, and the final outcome/error if any.

Just reload the page to see updates while `main.py` is running.

## Checking for bounces ("this email no longer exists")

Gmail usually accepts a send even if the mailbox is dead — the "no such
user" rejection often comes back **later** as an automated bounce email in
your own inbox, not as an instant error. Run this after sending (an hour or
two later, or once a day):

```bash
python bounce_checker.py
```

It scans your inbox for delivery-failure notifications, matches the failed
address back to the right company, and updates its status to `bounced` in
the dashboard. This is best-effort (bounce message formats vary slightly
by provider) but catches the common cases.

*Note: some failures ARE caught instantly at send time* (e.g. clearly
malformed addresses) — those show up immediately as `failed` with an error
message, no bounce-checking needed.

## Avoiding spam flags

Sending many similar emails from a personal Gmail account can get flagged.
Built-in protections:
- **Personalized content per email** (via the writer agent) — identical
  bulk content is one of the biggest spam triggers, and this pipeline
  never sends the same body twice.
- **Randomized delay** between sends (`MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS`
  in `.env`) instead of firing them all at once.
- **Daily send cap** (`MAX_EMAILS_PER_DAY`) so you don't blast hundreds in one go.
- **Proper email formatting** — real headers, plain text, a genuine PDF
  attachment (no tracking pixels, link shorteners, or other bulk-mailer tells).

What you should also do manually (can't be fully automated):
- **Warm up your account.** Start with `MAX_EMAILS_PER_DAY=10-15` for your
  first few days of sending, then increase gradually. A brand-new burst of
  100+ emails on day one from a normal Gmail account is the single most
  common way to get flagged or rate-limited.
- **Reply to any responses promptly.** Engagement (opens, replies) tells
  Gmail your account is legitimate.
- **Double-check your company list.** A high bounce rate (invalid
  addresses) hurts your sender reputation more than volume does — a
  quick manual scan of `companies.xlsx` for obviously wrong emails before
  a big run is worth the two minutes.

## Project structure

```
.
├── main.py                  # orchestrator — run this to send applications
├── db.py                    # SQLite storage (applications + event log)
├── mailer.py                # Gmail SMTP sending + CV attachment + error handling
├── bounce_checker.py        # run separately to detect bounced emails
├── specializations.json     # your core pitch + extra-mention config (EDIT THIS)
├── companies_template.csv   # example input — rename/replace with your real list
├── .env.example             # copy to .env and fill in
├── requirements.txt
├── agents/
│   ├── research_agent.py    # scrapes company site, matches extra mentions
│   └── writer_agent.py      # generates the final email
└── dashboard/
    ├── app.py                # Flask dashboard (python dashboard/app.py)
    ├── templates/
    └── static/style.css
```

## Troubleshooting

- **"missing required .env values"** — you haven't filled in `.env` yet
  (copy from `.env.example`).
- **"Gmail authentication failed"** — you're using your normal Gmail
  password instead of an App Password, or 2-Step Verification isn't
  enabled on your Google account (required for App Passwords).
- **Emails going out with a generic/empty company context** — the
  `website` column is missing or the site blocked scraping; the pipeline
  still sends (using your core pitch alone) rather than failing, so check
  the dashboard's research panel per company if emails look too generic.
- **CV attachment missing/wrong file** — check `CV_FILE_PATH` in `.env`
  points to the right file.
