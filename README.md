# Internship Application Automation

Researches each company, writes a personalized application email around your
fixed core pitch, and lets you **review every draft before anything is sent**.
Sending happens from a local dashboard, paced and capped to protect your Gmail
account's reputation.

## How it works

```
companies.xlsx / .csv
        │
        ▼
 ┌──────────────────┐     ┌──────────────────┐
 │  Research agent  │ ──▶ │   Writer agent   │   python main.py
 │  (scrapes site,  │     │ (core pitch +    │   (never sends)
 │  matches 0-2     │     │  matched extra   │
 │  extra mentions) │     │  sentences)      │
 └──────────────────┘     └──────────────────┘
        │                          │
        └──────────┬───────────────┘
                   ▼
            applications.db  ──▶  Dashboard: review, edit, send
                   ▲                         │
                   └─────────────────────────┘
                     sender worker (paced, daily cap, CV attached)
```

Two separate programs, on purpose:

- **`python main.py`** — preparation only. Researches and drafts. It never
  sends, so you can run it on a big list and walk away.
- **`python dashboard/app.py`** — review and send. You read the drafts, edit
  anything you want, pick which ones go out, and the background sender sends
  them with a randomized delay between each and a daily cap.

Your **core identity** (RedBox, CTF wins, professional experience, the
AI-from-security story) lives in `specializations.json` and appears in every
email. The research agent only decides whether 0, 1, or 2 short **extra
mentions** (cloud / software dev / data — configurable) genuinely fit a given
company, and the writer weaves those in near-verbatim. It never restructures
or shrinks the core pitch.

## Setup

1. **Install Python 3.10+.**

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start the dashboard and fill in the setup form:**
   ```bash
   python dashboard/app.py
   ```
   Open <http://127.0.0.1:5050> and complete "Workspace setup": your AI key,
   your name and target role, your companies file, your CV, and your Gmail
   connection. It writes everything to a local `.env` (you can also copy
   `.env.example` to `.env` and edit it by hand).

   You only need the AI key, your profile, and the companies file to start
   **preparing** drafts. Gmail and the CV are needed only when you send.

4. **Connect Gmail**, either way:
   - **Sign in with Google (recommended).** Put your OAuth client file
     (`client_secret_*.json` or `credentials.json`) in the project folder, then
     click "Sign in with Google". No app password needed, and bounce checking
     works through the same connection.
   - **App password.** Needs 2-Step Verification on your Google account; create
     one at <https://myaccount.google.com/apppasswords>.

### Companies file

Any `.csv` or `.xlsx` with at least an `email` column. Also understood:
`company_name`, `website`, `name`/`contact_name`, and contact-export sheets
with an `attributes` JSON column like `{"Company": "Rtone"}`.

Missing a company name or website? Both are guessed from the email domain.
Addresses are lowercased and de-duplicated, so one mailbox never gets two
applications.

## Running it

**Prepare drafts** (no email leaves your account):

```bash
python main.py --limit 10      # first 10 companies that still need work
python main.py                 # everything still pending
```

Useful flags:
- `--companies path/to/file.xlsx` — use a specific file
- `--limit N` — only prepare N companies this run
- `--all` — include rows already prepared
- `--research-workers N` / `--writer-workers N` — override the `.env` defaults

You can also press **Start preparation** in the dashboard, which runs the same
thing in the background and streams the log into the Activity log panel.

**Review and send** in the dashboard:
1. The funnel at the top shows where every company is: to prepare, ready to
   review, sending, sent, problems. Click one to filter the table.
2. Open a company to read the email exactly as the recipient will see it —
   headers, body, CV attachment — and edit it if you want.
3. Tick the rows you're happy with and press **Send selected**. You get a
   confirmation listing exactly who is about to be emailed.

Safe to stop and re-run at any time: sent companies are skipped, and finished
research/drafts are reused from `applications.db` and `cache/` instead of
being redone.

### Per-company actions

- **Skip** — leave a company out of preparation and sending, reversibly.
- **Regenerate** — rewrite the draft using the research already on file (no
  re-scraping, no new research call).

## Checking for bounces

Gmail usually accepts a send even if the mailbox is dead; the rejection arrives
later as a bounce email in your own inbox. Press **Check bounces now** in the
dashboard, or run:

```bash
python bounce_checker.py
```

It scans for delivery-failure notifications, matches the failed address back to
the right company, and marks it `bounced`. Only companies you actually sent to
can be marked this way.

## Avoiding spam flags

Built in:
- **Personalized content per email** — no two bodies are identical.
- **Randomized delay** between sends (`MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS`).
- **Daily cap** (`MAX_EMAILS_PER_DAY`), counted in UTC.
- **Proper formatting** — real headers, plain text, a genuine PDF attachment,
  no tracking pixels or link shorteners.

Worth doing yourself:
- **Warm up.** Start at 10–15 a day for the first few days, then raise it.
- **Reply promptly.** Engagement tells Gmail the account is legitimate.
- **Check your list.** A high bounce rate hurts your reputation more than
  volume does.

## Project structure

```
.
├── main.py                  # preparation (research + write). Never sends.
├── pipeline.py              # concurrent research/writer pools, resume rules
├── agents/
│   ├── research_agent.py    # scrapes the site, matches extra mentions
│   └── writer_agent.py      # assembles the email
├── ai_client.py             # OpenAI-compatible client, retries, rate limit
├── db.py                    # SQLite storage, send jobs, recovery sweeps
├── cache_store.py           # per-company on-disk cache (resumability)
├── sender_worker.py         # background sender: pacing, daily cap
├── mail_service.py          # send wrapper returning structured results
├── mailer.py                # Gmail API / SMTP + error classification
├── google_auth_helper.py    # OAuth token storage and Gmail API
├── bounce_checker.py        # marks bounced applications
├── specializations.json     # your core pitch + extra mentions (EDIT THIS)
├── dashboard/
│   ├── app.py               # Flask dashboard (python dashboard/app.py)
│   ├── templates/
│   └── static/
└── tests/                   # pytest suite
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Each test runs against its own temporary database, cache, and `.env` — the
suite never touches your real data or credentials, and never makes a network
call.

## Troubleshooting

- **"missing required .env values"** — finish the dashboard setup form, or copy
  `.env.example` to `.env` and fill it in.
- **Gmail rejects the login** — you're using your normal password instead of an
  App Password, or 2-Step Verification is off. Or just connect with Google
  instead.
- **"Sign in with Google" doesn't appear** — the OAuth client file isn't in the
  project folder. Use an app password, or download the file from Google Cloud
  Console.
- **Emails look generic** — the company's website was missing or blocked
  scraping, so the research agent had nothing to read. The company's detail page
  shows exactly what was found.
- **A row is stuck "Retry later" after a restart** — the process was killed
  mid-send and we can't tell whether that message went out. Check your Gmail
  Sent folder before re-sending it.
- **Everything says "Ready" but nothing sends** — check the daily cap on the
  Sent card; the sender pauses once it's reached and resumes the next day.
