# Morrow API

A backend-only FastAPI service for the existing `froned-dashbord` application: accounts, PostgreSQL, private artifacts, and subscriptions. All screens live in `froned-dashbord` and use its existing navigation and visual system. This repository serves JSON APIs and private file content; it does not build or serve another frontend.

For India payments by INR, UPI AutoPay, and cards, follow [PAYMENTS_SETUP.md](PAYMENTS_SETUP.md). Razorpay uses the existing Plans & billing screen; Stripe remains supported for existing installations.

For the first 100 users, use the [Firebase setup guide](FIREBASE_SETUP.md). It covers free Google/email authentication, verification and recovery emails, optional Resend delivery, and preserving the imported administrator workspace. No Firebase project is connected yet; the current local configuration remains usable with local accounts.

## What works

- Email signup, verification, login, logout, password recovery, and Google OpenID Connect.
- Optional Firebase managed identity with verified token exchange, project/UID binding, revocation checks, and explicit account migration. PostgreSQL remains authoritative for roles and paid access.
- File, SMTP, or Resend mail delivery with HTML/plaintext messages, retry backoff, and expired-link cleanup.
- Revocable sessions in HttpOnly cookies, CSRF checks, Argon2 passwords, single-use email links, and persistent authentication throttling.
- User and administrator roles. Modules are checked on the server against the current plan on every request.
- Profiles with version conflicts, application history with idempotent writes, module records, and authenticated artifact uploads/downloads.
- Tenant-scoped onboarding drafts, platform account labels, and a selected private PDF resume, with version conflicts and computed profile/resume readiness.
- PostgreSQL row-level security with a non-owner runtime role. Each user has a separate tenant.
- Administrator inspection of users’ profiles, application history, records, and artifacts; access and role/plan changes require a reason and write an audit entry. The runtime role cannot update or delete audit entries.
- Stripe hosted subscription Checkout, the customer portal, and signed, replay-safe webhooks. Entitlements come from retrieved subscription state; the checkout return URL cannot grant access.
- Razorpay subscription Checkout, owned subscription status, payment reconciliation, and end-of-period cancellation. Signed webhooks and server-fetched paid invoices/captured payments determine access; authorization alone does not unlock modules.
- A same-origin integration with the existing dashboard for signup, login, profiles, files, pricing, account settings, and administration.

The resume, research, learning, automation, outreach, and insights modules have plan gates and storage APIs, but their legacy workflows have **not yet been migrated to individual user runtimes**. Legacy shared workflows remain restricted to the configured workspace owner. Claude/Kiro selection saves a preference; it does not connect an AI session. Keep payments in test mode until the advertised paid workflows are ready.

## Simple starting plans

| Plan | Monthly price | Module access |
| --- | ---: | --- |
| Free | ₹0 | Profile, applications, private files |
| Plus | ₹499 | Free + resume studio, research, learning |
| Pro | ₹999 | Plus + automations, outreach, insights |

Every user supplies their own Claude or Kiro subscription. No AI subscription or credits are bundled. Administrators have all module permissions. They can edit plan names, descriptions, included modules, and matching provider plan/price IDs. The monetary defaults are seeded in the database; changing a price requires a deliberate database migration and a matching new provider plan/price.

Administrator-assigned plans override subscription entitlements. Editing access does not cancel billing. Razorpay renewals can be cancelled in Plans & billing; Stripe customers use its billing portal. Expired paid access falls back to Free. Existing private files remain available after a downgrade.

## Run locally

Requirements: Docker Compose, Python 3.12+, and uv. This API has no Node or frontend build requirement. Follow the existing dashboard's instructions to install and build its UI.

```sh
uv sync --locked
uv run morrow init-local
docker compose up -d --build
```

`init-local` creates a private `.env` with unique database and session secrets. If this workspace already has `.env`, preserve it and skip that command. Migrations run as a separate owner job before the API starts. The API listens at <http://127.0.0.1:8200>; readiness is at `/health/ready` and API documentation is at `/docs`.

The dashboard defaults to a free local beta without accounts or billing. To use this API,
start `npm start -- --mode accounts` in `froned-dashbord`, with
`MORROW_API_URL=http://127.0.0.1:8200` and `MORROW_WORKSPACE_OWNER_EMAIL=admin@example.com`
in its ignored `.env.local`, then open <http://127.0.0.1:8100/#/login>. Set
`MORROW_MODE=accounts` to persist that choice. See the [dashboard setup](../froned-dashbord/README.md).
In accounts mode, the dashboard proxies `/api/v1/*` to the API, so browser requests,
cookies, CSRF checks, Google callbacks, and account screens share the dashboard origin.
`PUBLIC_ORIGIN` is the dashboard's origin (default `http://127.0.0.1:8100`), not the API's listening address.

Create the first administrator with an interactive password prompt:

```sh
uv run morrow create-user --email admin@example.com --name Administrator --admin
```

For this local implementation, `admin@example.com` is the selected test owner. If bootstrapped by the agent, its generated password is in `.runtime/bootstrap-admin.txt` (private and ignored by Git). Do not use that test email for a public launch; provision an administrator at an email you control first.

The API container receives only the restricted runtime database credential. PostgreSQL and the API bind to loopback. Artifacts and development email live in the `morrow-api_private-data` Docker volume. Git contains code and configuration templates, not profiles, application history, passwords, or private files.

## Connect email, Google, and payments

Edit `.env`, then recreate the API with `docker compose up -d api`.

**Recommended managed authentication:** set up `AUTH_PROVIDER=firebase` using [FIREBASE_SETUP.md](FIREBASE_SETUP.md). Firebase handles Google sign-in and authentication emails. The following direct Google and local signup/recovery settings apply to `AUTH_PROVIDER=local`.

**Email:** use `MAIL_BACKEND=resend`, `RESEND_API_KEY`, and a verified-domain `MAIL_FROM`; or set `MAIL_BACKEND=smtp` with `SMTP_HOST`, `SMTP_PORT=587`, `SMTP_USERNAME`, and `SMTP_PASSWORD` for STARTTLS. Local signup and recovery queue messages in PostgreSQL. Run `docker compose run --rm admin send-mail` from a one-minute timer for retries. Delivery failures use durable backoff and stop after five attempts; expired authentication links are discarded. Resend requests use stable idempotency keys. The default file backend writes private `.eml` previews and sends nothing externally.

**Google:** create a Web application OAuth client and set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Register the redirect URI exactly as `PUBLIC_ORIGIN/api/v1/auth/google/callback`, for example `http://127.0.0.1:8100/api/v1/auth/google/callback` locally. This public callback is forwarded by the dashboard to the API. Configure the Google consent screen/test users. The flow uses state, nonce, PKCE, and verified Google email identities. An existing password account is not automatically linked by matching email; those users continue with email/password. Provider failures return to the dashboard sign-in screen without exposing tokens. See [Google’s OpenID Connect documentation](https://developers.google.com/identity/openid-connect/openid-connect).

**Razorpay (India):** set `BILLING_PROVIDER=razorpay` and follow [PAYMENTS_SETUP.md](PAYMENTS_SETUP.md) for test keys, monthly INR plans, webhook events, and verification. API keys, webhook signing secret, and matching plan IDs are all required before checkout becomes available.

**Stripe:** select `BILLING_PROVIDER=stripe` and begin with test credentials. Set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`. Create monthly recurring INR prices of 49900 and 99900 minor units for Plus and Pro. In **People & access**, enter an administration reason and save each `price_…` ID in its plan. The server checks currency, amount, and monthly recurrence. Configure the Stripe customer portal for the intended cancellation/plan-change behavior.

Register `PUBLIC_ORIGIN/api/v1/billing/webhook` for `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted`. Locally, use `stripe listen --forward-to localhost:8200/api/v1/billing/webhook` and save the listener’s signing secret. Use the endpoint’s own signing secret in production. Webhooks reconcile current Stripe state so delayed events cannot restore a canceled plan. Checkout and customer-portal returns go to the dashboard's `/#/plans` screen. See [Stripe subscription Checkout](https://docs.stripe.com/billing/subscriptions/build-subscriptions) and [webhook setup](https://docs.stripe.com/webhooks).

Firebase, Google, email-provider, and real payment credentials are not included or fabricated. Provider behavior is exercised with test adapters and signed synthetic webhook events; live delivery/payment/provider sign-in require your accounts.

## Import the existing workspace

Create the destination account first. The maintenance container shares the API’s artifact volume and mounts the existing workspace read-only at `/source`:

```sh
docker compose --profile maintenance build admin
docker compose --profile maintenance run --rm admin import-workspace --source /source --owner-email admin@example.com
docker compose --profile maintenance run --rm admin import-workspace --source /source --owner-email admin@example.com --apply
```

The first command after building previews counts; `--apply` imports. It snapshots the existing SQLite ledger, preserves its five known tables as legacy records, normalizes applications, copies the profile, and copies allowlisted resumes, research, reports, and progress files. Credentials, `.env`, browser cookies, session directories, and raw agent traces are excluded. Files larger than the configured upload limit are reported and skipped. Original files and databases are unchanged. Repeating an unchanged import does not duplicate applications or artifacts. A changed application with the same imported ID raises a conflict rather than silently rewriting history.

When running the API directly on the host, use `uv run morrow import-workspace --source .. --owner-email admin@example.com --apply` instead; that writes into the host’s configured artifact root. Do not mix host and container artifact roots for the same database.

## Existing dashboard integration

The existing `froned-dashbord/server.py` checks the account service at `MORROW_API_URL` (default `http://127.0.0.1:8200`) and proxies its `/api/v1/*` endpoints. Set `MORROW_WORKSPACE_OWNER_EMAIL=admin@example.com` when starting it. Sign in inside the existing dashboard; ordinary accounts use tenant-scoped profile, application, and artifact APIs. Only the named administrator can use the original shared local workspace workflows. Authenticated identity and current plan modules are available at `/api/v1/me`; the dashboard's `/api/session` adds local-workspace availability without exposing data from other users. If the account service is unavailable, protected access fails closed.

Account navigation uses dashboard hash routes: `/#/login`, `/#/signup`, `/#/overview`, `/#/verify?token=…`, `/#/reset?token=…`, and `/#/plans`. Email links and OAuth/payment return URLs are built from `PUBLIC_ORIGIN`. There is no separate `/account` UI on the API.

The original automation tools remain restricted to the configured workspace owner. The `localhost:8000` predecessor and bot scripts are not public services. Import is a snapshot, not a live synchronization bridge. New bot executions will continue writing the legacy ledger until their storage adapters are migrated. All users share the existing dashboard UI while their account records and private artifacts remain separate.

### Onboarding API

`GET /api/v1/onboarding` returns `version`, `platforms`, `account_hints`, `resume_artifact_id`, safe selected-resume metadata, the current versioned `profile`, `completed_at`, and computed `readiness`. An account without a draft starts at version `0`. Readiness is recalculated when profile fields change or the selected resume is deleted. A previous completion timestamp records when setup was saved; it does not certify current browser authentication or runner availability.

`PUT /api/v1/onboarding` replaces the preferences with `expected_version`, `platforms`, `account_hints`, nullable `resume_artifact_id`, and optional `completed` (default `false`). Supported platform IDs are `naukri`, `linkedin`, `hirist`, `instahyre`, `cutshort`, `wellfound`, and `google`. A conflicting version returns `409` without replacing saved data. Account hints are plain display labels or email addresses, up to 254 characters; passwords, cookies, browser state, and client-supplied verification flags are not fields in this API.

Save facts through the existing versioned `/api/v1/profile` API. The minimum fields for completing onboarding are nonempty strings `full_name`, `email`, `phone`, `current_location`, and a nonempty list of strings in `titles`. Upload the PDF through `POST /api/v1/artifacts?filename=resume.pdf&category=resumes`, then select its returned ID. Selection checks the tenant, category, PDF filename/content type, checksum, and PDF signature. Artifact content stays in private artifact storage.

Incomplete drafts are allowed. `completed: true` requires the minimum profile, an owned PDF resume, and at least one selected platform. Repeated completed saves retain their original completion timestamp; saving with `completed: false` returns the record to a draft. No cookie or browser session is uploaded by these endpoints. Browser authentication and session retention are handled separately on the user's local runner. Apply migration `005_onboarding` before starting the updated API; the runtime database role has tenant-scoped access to the new state table.

## Deployment shape

Start with one modest Linux server: an HTTPS reverse proxy serving the existing dashboard, this backend API container, and PostgreSQL on a private network. Forward `/api/v1/*` to the API, including public auth and signed billing-webhook endpoints, and keep browser-facing routes on a single origin. Keep accounts, plans, billing, and workspace storage together for now. Extract a queue and tenant-aware worker only when enabling multiuser bot execution; each job must carry a tenant ID and use isolated credentials, files, and browser state.

Before public deployment, set `PUBLIC_ORIGIN` to the HTTPS origin, `COOKIE_SECURE=true`, and `ALLOWED_HOSTS` to the actual hostnames. Keep owner credentials out of the API. Run migrations separately. Restrict proxy forwarding to trusted infrastructure and apply edge request limits. Use encrypted disk/backups for local artifact storage, or set `ARTIFACT_BACKEND=s3` with a private bucket and workload credentials. The S3 adapter requires all four public-access-block flags and writes AES256-encrypted objects; it never returns public object URLs. The default local file backend uses restrictive permissions but does not itself encrypt the host disk.

Back up PostgreSQL and artifacts together and exercise restoration. Schedule queued-mail retries and tombstoned artifact cleanup (`morrow purge-artifacts`). A failed database commit after an object write can leave an unreferenced private object; add an inventory-based orphan cleanup policy before operating at scale. This first version does not implement usage quotas, MFA, or tenant-aware bot scheduling.

## Development and verification

```sh
uv sync --locked
uv run pytest -q --tb=short
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

Database tests create randomly named isolated databases and remove them afterward. Set `TEST_DATABASE_OWNER_URL` in CI; locally they use the owner URL from `.env`. Tests cover tenant isolation at API and PostgreSQL levels, private artifacts, CSRF, session revocation, recovery, administrator audit/role controls, Stripe replay and out-of-order events, private S3 behavior, and repeatable imports.

For an isolated synthetic API used by the existing dashboard's browser tests:

```sh
uv run python scripts/preview.py
```

The preview API listens on port 8201 and expects the dashboard at <http://127.0.0.1:8102>. Start `froned-dashbord/server.py --port 8102` with `MORROW_API_URL=http://127.0.0.1:8201` to use it. Preview accounts are `preview.user@example.com` and `preview.admin@example.com`, both with password `Preview-only-passphrase-2026!`. These are development fixtures in a separate database, never production credentials. Run browser checks from `froned-dashbord`; backend tests also verify that no second UI is mounted and account links return to the dashboard.
