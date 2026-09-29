# Deploying

One Linux server with Docker runs everything, with one command: PostgreSQL
(`db`), the app (`app`: website + background worker) and, with a domain,
Caddy for automatic HTTPS. Only ports 80 and 443 are exposed.

```
Internet ──443──▶ caddy ──▶ app  (gunicorn + worker.py, one container)
                             │
                             └──▶ db (PostgreSQL 17, volume)
```

## 1. Server

- A small VM is enough to start: 2 vCPU, 2–4 GB RAM, Ubuntu 24.04, Docker.
- A domain name with an `A` record pointing at the server.
- Outbound SMTP (465/587) must not be blocked by the host — some clouds block
  it on new accounts; ask support to open it if sending fails.

## 2. Configuration

```bash
git clone <repo> apply && cd apply
cp .env.example .env
python3 manage.py generate-keys        # paste both lines into .env
```

In `.env`: `POSTGRES_PASSWORD`, `SECRET_KEY`, `ENCRYPTION_KEYS`,
`ADMIN_USERNAME`, `ADMIN_PASSWORD` (use a long password on a public
server), and for HTTPS `DOMAIN`, `PUBLIC_BASE_URL=https://<DOMAIN>`,
`TRUST_PROXY=1`.

**Back up `ENCRYPTION_KEYS` somewhere safe.** Every user's saved API keys and
mail passwords are encrypted with it.

## 3. Start

```bash
docker compose --profile https up -d --build
```

Open `https://<DOMAIN>` and sign in with the admin username. Caddy fetches
the TLS certificate on the first request.

### Bringing over an existing single-user install

```bash
docker compose run --rm -v /path/to/old-install:/legacy:ro app \
  python manage.py import-legacy --email you@example.com --root /legacy
```

The account is created as a regular user who signs in with Google; every row
keeps its original id and is verified after the import.

## 4. Google sign-in (optional)

Users can always send with a Gmail **app password** or any **SMTP** server —
that scales to any number of users with no Google review.

For one-click "Sign in with Google":

1. Google Cloud Console → enable the **Gmail API**.
2. OAuth consent screen → *External*. Scopes: `gmail.send`, `userinfo.email`,
   `openid`.
3. Credentials → OAuth client → *Web application*, redirect URI
   `https://<DOMAIN>/oauth/callback` (the same URI serves sign-in and Gmail connection).
4. Upload the client JSON in **Admin → Platform** (stored encrypted), or set
   `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

While the Google app is in **Testing**, only listed test users (max 100) can
sign in and tokens expire after 7 days. To open it to everyone, submit the
app for verification: `gmail.send` is a *sensitive* scope (a review, no fee).
Keep **"Read email"** off in Admin → Platform for a public launch —
`gmail.readonly` is *restricted* and requires a paid yearly security
assessment; bounce detection keeps working through app passwords (IMAP).

## 5. Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f app` |
| Update | `git pull && docker compose --profile https up -d --build` |
| Backup DB | `docker compose exec db pg_dump -U internship internship \| gzip > backup-$(date +%F).sql.gz` |
| Restore DB | `gunzip -c backup.sql.gz \| docker compose exec -T db psql -U internship internship` |
| Reset a password | `docker compose exec app python manage.py set-password --email user@x.com` |
| Rotate encryption key | put a new key FIRST in `ENCRYPTION_KEYS` (keep the old one after a comma), restart, run `python manage.py rotate-keys`, then remove the old key |

Schedule the backup daily (cron) and copy it off the server.

## Security model, in short

- Passwords: Argon2id. Sessions: server-side, 256-bit random token in an
  HttpOnly/Secure/SameSite cookie, only its SHA-256 stored; idle timeout 7
  days, absolute 30 days; revoked on password change, suspension, role change.
- Brute force: per-account lockout with exponential back-off, per-IP and
  per-account rate limits (in the database, so they hold across processes).
- CSRF: per-session token on every state-changing request + Origin check.
- Isolation: every query is scoped to the signed-in user; another user's row
  is a 404. Admins manage accounts but never see keys, drafts or CVs.
- Secrets at rest: Fernet (AES + HMAC), bound to owner and name, key only in
  the environment.
- SSRF: company websites, custom AI endpoints and mail servers are user
  input; private, loopback, link-local and metadata addresses are refused,
  checked again on the connected socket and on every redirect.
- Headers: nonce-based CSP, no framing, nosniff, strict referrer, HSTS,
  `no-store` on every page.
- Uploads: size-capped, type checked by content (magic bytes), XML parsed
  with defusedxml, CVs stored in the database and served only to their owner
  as attachments.
- Audit log of sign-ins, failures and every admin action.
