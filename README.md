# Internship Application Automation

Researches each company, builds an application email tailored to what that
company actually does, and lets you **review every draft before anything is
sent**.
Sending happens from a local dashboard, paced and capped to protect your Gmail
account's reputation.

## How it works

```
companies.xlsx / .csv
        │
        ▼
 ┌──────────────────┐     ┌──────────────────┐
 │  Research (AI)   │ ──▶ │  Composer (code) │   python main.py
 │  picks CV areas, │     │  builds the email│   (never sends)
 │  quotes the site │     │  from CV text    │
 │  — then verified │     │  — no AI         │
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

### What each email says

In the order a recruiter reads it:

1. **Who you are, and what interests you about this company**, quoting what
   they do in their own words — or, when nothing reliable was found about
   them, a plain "I'm really interested in joining the team at <Company>".
2. **How your experience matches their work**: the one or two areas of your
   CV that fit them (offensive security, SOC, reverse engineering, agentic
   AI, machine learning, cloud, software…), each backed by a concrete result.
   Left out when nothing matched.
3. **Your other strong points**, never repeating a project already cited.
4. **The internship ask**: end-of-study, Bac+5, starting February 2027.
5. **Closing**, with your CV attached.

No tool names: the email says what you built, the CV lists the tools. All the
wording lives in `specializations.json`.

## Setup

1. **Install Python 3.10+.**

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Pick an AI provider.** Any OpenAI-compatible endpoint works — Groq
   (free tier, the default), OpenRouter, Together, a local Ollama. You only
   ever set three values: `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`.

   Unsure which model, or on a provider not listed here? Let the repo tell you:
   ```bash
   python check_models.py --list   # what does this endpoint offer?
   python check_models.py --all    # which of them researches accurately?
   python check_models.py --show   # ...and the emails that research produces
   ```
   Each candidate researches four fixed test websites — including an empty
   login page, where the only right answer is "nothing here" — and the script
   recommends the most accurate model that invented nothing.

4. **Start the dashboard and fill in the setup form:**
   ```bash
   python dashboard/app.py
   ```
   Open <http://127.0.0.1:5050> and complete "Workspace setup": your AI key,
   your name and target role, your companies file, your CV, and your Gmail
   connection. It writes everything to a local `.env` (you can also copy
   `.env.example` to `.env` and edit it by hand).

   You only need the AI key, your profile, and the companies file to start
   **preparing** drafts. Gmail and the CV are needed only when you send.

5. **Connect Gmail**, either way:
   - **Sign in with Google (recommended).** One-time setup in
     [Google Cloud Console](https://console.cloud.google.com/):
     1. Create a project and enable the **Gmail API**.
     2. On the **OAuth consent screen**, choose *External* and add your Gmail
        address as a **test user**.
     3. Under **Clients**, create a client of type **Web application** with the
        authorised redirect URI `http://127.0.0.1:5050/oauth/callback`.
     4. Download its JSON and upload it in the dashboard (Gmail account → Set up
        Google sign-in), then click **Sign in with Google** and tick every
        permission.

     Your address is filled in automatically from the Google account. No app
     password is needed, and bounce checking works through the same connection.
     While the app is in *Testing* mode Google expires the sign-in after 7 days;
     the dashboard then asks you to sign in again.
   - **App password.** Needs 2-Step Verification on your Google account; create
     one at <https://myaccount.google.com/apppasswords>.

### Keeping the emails truthful

Free AI models embellish when asked to write, so here **the AI never writes
the email**. It answers two narrow research questions, and code checks both
answers against the company's website text before using them:

- **Which CV areas fit** — picked from a closed list, kept only if the site
  actually contains that area's keywords.
- **One phrase describing what they do** — kept only if its words are really
  on the site, it quotes no number the site doesn't, and the sentence it
  cites is found there too.

Anything that fails is dropped and the email falls back to its standard
wording. A weak model can make an email *less specific*; it can't make it say
something untrue. `agents/composer.py` then assembles the email from your CV
text, and `agents/draft_guard.py` checks the result one last time (invented
numbers, flattery like "industry leader", leftover placeholders).

Each company's page in the dashboard shows exactly why its email says what it
says: the phrase used, the site sentence it came from, and which CV areas
matched.

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
- **Rebuild draft** — rebuild the email from the research already on file and
  your current `specializations.json`. Instant, no AI call — use it after
  editing your wording.

## Checking for bounces

Gmail usually accepts a send even if the mailbox is dead; the rejection arrives
later as a bounce email in your own inbox. While the dashboard is running it
scans for these automatically every `BOUNCE_CHECK_MINUTES` (default 30) for
three days after each send. Undelivered emails show as **Not delivered** in red,
with the mail server's reason, and a red banner counts them. You can also press
**Check bounces now**, or run:

```bash
python bounce_checker.py
```

It scans for delivery-failure notifications, matches the failed address back to
the right company, and marks it `bounced` (shown as *Not delivered*). Only companies you actually sent to
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
├── pipeline.py              # concurrent research/compose pools, resume rules
├── check_models.py          # discover + score models on any provider
├── agents/
│   ├── research_agent.py    # scrapes the site, asks the AI, verifies the answers
│   ├── composer.py          # builds the email from CV text (no AI)
│   └── draft_guard.py       # final check for invented claims
├── ai_client.py             # OpenAI-compatible client, retries, rate limit
├── db.py                    # SQLite storage, send jobs, recovery sweeps
├── cache_store.py           # per-company on-disk cache (resumability)
├── sender_worker.py         # background sender: pacing, daily cap
├── mail_service.py          # send wrapper returning structured results
├── mailer.py                # Gmail API / SMTP + error classification
├── google_auth_helper.py    # OAuth token storage and Gmail API
├── bounce_checker.py        # marks bounced applications
├── specializations.json     # all email wording, from your CV (EDIT THIS)
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
