# Apply — internship applications that sound like you

A multi-user web app for students. It researches each company, builds an
application email from **your own CV** — in **English or French**, whichever
the company speaks — and lets you **review every draft before anything is
sent**. Sending is paced and capped per account to protect each mailbox's
reputation.

```
 your CV ──▶ Profile (AI draft, every claim checked against the CV, you approve)
                                   │
 companies list ──▶ Research (AI) ─┼─▶ Composer (code, no AI) ──▶ drafts (EN/FR)
   (CSV/XLSX)     reads each site, │     your approved wording        │
                  quotes it,       │     + an email style             ▼
                  verifies it      │                          review, edit, send
                                   ▼                          (paced, daily cap)
                        PostgreSQL, per-user isolated
```

## For users

1. **Create an account** (an administrator approves it, depending on the
   platform's setting).
2. **Settings → AI provider**: paste your own key (Groq has a free tier). Your
   usage never competes with anyone else's.
3. **Profile**: upload your CV and answer three questions (what you're looking
   for, from when). The AI drafts your profile in English and French from the
   CV only. Every sentence mentioning a number or a name your CV doesn't
   contain is flagged until you fix or confirm it. Pick an **email style**
   and preview it on a real company.
4. **Settings → Companies**: import a CSV/Excel file (only an `email` column is
   required). You see what's usable and which rows are wrong before anything
   is saved; you can add more files later.
5. **Start preparation** on the tracker. Drafts appear as they're ready.
6. **Settings → Email account**: sign in with Google, use a Gmail app
   password, or any SMTP server. Upload the CV to attach.
7. Review, edit, star, and **send**. Each company's page has an **EN | FR**
   switch.

### Email styles

| Style | For |
|---|---|
| Specialist match | The proven default: what they do (quoted from their site) → your strongest matching experience → your other highlights |
| Short & direct | Busy recruiters: four short paragraphs |
| Project first | One standout achievement, in the first line |
| Formal | Large companies, public institutions, traditional sectors |
| Research & R&D | Laboratories and research internships |

Each exists in English and French. The French wording never assumes the
reader's or the applicant's gender.

### Which language each email is in

1. Your choice on that company's page (EN | FR switch), else
2. your default (Profile → Language: automatic / always English / always French), else
3. the language of the company's own website, else
4. French for a `.fr` domain, else English.

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

A `.csv` or `.xlsx` with a header row. Only `email` is required; also read:
`company_name`, `website`, `contact_name` (and common English/French names for
them: *company, entreprise, site web, nom…*). Missing names and websites are
guessed from the email domain. Addresses are lowercased and de-duplicated,
within the file and against your existing list.

## Checking for bounces

Gmail usually accepts a send even if the mailbox is dead; the rejection arrives
later as a bounce email in your own inbox. The worker scans each user's inbox
automatically (Settings → Sending pace, default every 30 minutes) for three
days after each send. Undelivered emails show as **Not delivered** in red,
with the mail server's reason, and a red banner counts them. You can also press
**Check bounces now**. Reading the inbox needs Google sign-in with the "Read
email" permission, or a saved app password (IMAP).

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

## For administrators

The platform has one administrator: the account named by `ADMIN_USERNAME` /
`ADMIN_PASSWORD` in `.env`. It signs in with that **username** (not Google)
and is the only account that can open the admin panel. Changing the values
in `.env` and restarting updates it (and signs it out everywhere).

- **Admin → Users**: approve sign-ups, create accounts, suspend/reactivate,
  reset passwords, sign a user out everywhere, delete an account and all its
  data (confirmed by typing the address). Usage counts per user; never their
  keys or drafts.
- **Admin → Platform**: who can sign up; the Google OAuth client; whether
  Google sign-in also asks for inbox access (bounce detection).
- **Admin → Audit log**: sign-ins, failures, every admin action.

## Running it — one command

```bash
cp .env.example .env          # fill it in (python manage.py generate-keys for the keys)
docker compose up -d --build
```

Open **http://127.0.0.1:5050**. Two containers start: `internship_db`
(PostgreSQL, data in a Docker volume) and `internship_app` (the website and
the background worker). `docker compose down` stops them; the data stays.
Logs: `docker compose logs -f app`.

Users sign in with **Continue with Google** (one click also connects Gmail
for sending) or an email and password. The admin signs in with its username.

On a server with a domain, add HTTPS with `docker compose --profile https up
-d --build` — see [DEPLOY.md](DEPLOY.md).

### Without Docker (development)

```bash
pip install -r requirements.txt
python dashboard/app.py       # SQLite in data/ unless DATABASE_URL is set
```

### Moving a single-user install into an account

```bash
docker compose run --rm -v "$PWD:/legacy:ro" app python manage.py import-legacy --email you@example.com --root /legacy
```

Reads `applications.db`, `cache/`, `.env`, `token.json`, the CV, the companies
file and `specializations*.json` — without modifying them — into that account:
same application ids, every event and send job, keys and tokens encrypted.
The hand-written wording stays exactly as it was ("Your own wording"). The
account is a regular user who then signs in with Google.

## Project structure

```
dashboard/            Flask app: tracker, company pages, settings, profile,
                      sign-in (auth_routes), admin (admin_routes),
                      request security (security.py)
worker.py             background worker: preparation runs, sending, bounces
pipeline.py           research + drafting for one user, concurrently
agents/               research (AI, verified), composer (no AI), draft guard
drafting.py           one function that builds a draft (pipeline, rebuild, EN/FR)
email_templates.py    the five styles, English and French
profiles.py           CV text, AI profile draft, grounding checks
language.py           language detection and French specifics
db.py / database.py   per-user data access / schema (PostgreSQL or SQLite)
accounts.py           users, Argon2 passwords, sessions, rate limits, audit
vault.py              encryption of stored secrets
user_config.py        per-user settings, secrets, CV
safe_http.py          outbound requests that can't reach private networks
company_import.py     CSV/XLSX validation
mailer.py / mail_service.py / google_auth_helper.py / bounce_checker.py
manage.py             operator commands; migrate_legacy.py the importer
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q                                   # SQLite, ~1 minute
TEST_DATABASE_URL=postgresql+psycopg://…/internship_test pytest -q   # PostgreSQL
```

Each test gets its own empty database and a fixed test environment; a
tripwire fails the run if any test touches the real `.env`, database, token
or OAuth client file. No test reaches the network.
