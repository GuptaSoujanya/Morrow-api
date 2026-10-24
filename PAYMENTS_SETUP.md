# Payments setup: Razorpay for India

Morrow uses the existing dashboard for plan selection and billing. Razorpay Standard Checkout collects the customer's payment details; the backend verifies payment and subscription state before granting paid module access. PostgreSQL remains authoritative for the user's role and plan.

Use **Test Mode first**. No Razorpay merchant account, API credentials, live plan IDs, or payment approval are supplied with this repository. The initial plans are:

| Morrow plan | Monthly price | Razorpay plan amount | Currency |
| --- | ---: | ---: | --- |
| Free | ₹0 | No checkout or provider plan | INR |
| Plus | ₹499 | `49900` paise | INR |
| Pro | ₹999 | `99900` paise | INR |

Users provide their own Claude or Kiro subscription. Morrow's prices do not include AI subscriptions or credits. Administrators already have all module permissions; use an ordinary synthetic account when testing paid-access changes.

## Merchant account and activation

Create an account in the [Razorpay Dashboard](https://dashboard.razorpay.com/). Test Mode is available before completion of live KYC and simulates payments without moving money. Test API keys can be generated before adding a website. [Test/live overview](https://razorpay.com/docs/payments/quickstart/), [API key requirements](https://razorpay.com/docs/payments/dashboard/account-settings/api-keys).

Before accepting real payments, complete Razorpay's account activation, applicable identity/business verification, settlement bank verification, and website verification. Individual/unregistered businesses are supported, but requirements depend on the selected business type. Enter identity documents and bank details directly in Razorpay's dashboard. Approval and the available payment methods depend on the merchant account. [Account setup](https://razorpay.com/docs/payments/set-up), [KYC requirements](https://razorpay.com/docs/payments/business-types-kyc-documents).

Subscriptions support cards and UPI AutoPay, subject to supported networks, banks, apps, and account enablement. Enable Flash Checkout as described in Razorpay's subscription prerequisites. [Subscriptions prerequisites](https://razorpay.com/docs/payments/subscriptions/), [Supported payment methods](https://razorpay.com/docs/payments/subscriptions/supported-payment-methods/).

## 1. Configure test credentials

In Razorpay, select **Test Mode**, then **Account & Settings → API Keys → Generate Key**. Keep the test key ID and secret together; test key IDs begin with `rzp_test_`.

Edit the existing private `morrow-api/.env` locally. Preserve its database, session, authentication, email, and storage settings. Add:

```dotenv
BILLING_PROVIDER=razorpay
RAZORPAY_KEY_ID=rzp_test_REPLACE_LOCALLY
RAZORPAY_KEY_SECRET=REPLACE_LOCALLY
RAZORPAY_WEBHOOK_SECRET=REPLACE_WITH_A_SEPARATE_RANDOM_SECRET
RAZORPAY_TOTAL_COUNT=120
```

`RAZORPAY_TOTAL_COUNT=120` creates up to 120 billing cycles for these monthly plans unless the subscription is cancelled earlier. The supported range is 1–120 cycles. It is not an upfront charge for 120 months.

The key secret and webhook secret are backend-only credentials. Do not put them in chat, Git, frontend code, screenshots, or command-line arguments. The webhook secret is a separate value chosen for signature verification, not the API key secret. Standard Checkout receives the public key ID and subscription ID; it never receives either secret.

Keep `PUBLIC_ORIGIN` set to the existing dashboard origin, for example `http://localhost:8100` if that is the origin already configured for local authentication. It is not the API's port or the webhook tunnel address. On a hosted deployment it must be the dashboard's HTTPS origin.

### If Choose plan is disabled

The purchase button stays unavailable until the running API has both Razorpay credentials, a webhook signing secret, and a matching Razorpay plan ID for the selected paid plan. Saving keys in `.env` alone does not finish setup. Follow steps 2–4 below, including registering the webhook in Razorpay and recreating the API container with the existing Compose files so it loads the updated environment.

If Razorpay returns **401 Unauthorized**, verify that `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` belong to the same Test Mode key pair. Replace both together; a secret from a previous or regenerated key will not authenticate the current key ID. Never paste the secret into frontend code or chat. A failed authentication check must be resolved before creating or linking provider plans. See [Razorpay API authentication](https://razorpay.com/docs/api/authentication/).

Administrators see **Included in your access** because they already have every module. Test payment with an ordinary account once setup is complete. Accounts whose access was assigned by an administrator also cannot start a purchase while that override is in effect.

## 2. Apply the billing migration and restart the API

Run these commands from `morrow-api` after configuring the environment:

```sh
uv sync --locked
docker compose build migrate api
docker compose run --rm migrate
docker compose up -d api
```

The migration command applies `006_razorpay_billing` and grants the runtime database role the required access. It is safe to rerun the migration command; do not edit previously applied migrations. Preserve any additional Compose files already used by this installation, including the Firebase credentials overlay, when running Compose commands.

Rebuild the frontend with `npm run build` in `froned-dashbord`. When applying this update to an already running installation, restart its dashboard server with the existing `npm start` configuration during an idle period, so the API proxy also loads the Razorpay signature-header forwarding change. Keep the existing dashboard port and Firebase settings.

For an API run directly on the host, use the existing host configuration and artifact location instead:

```sh
uv run morrow migrate
uv run uvicorn morrow_api.app:create_app --factory --host 127.0.0.1 --port 8200 --no-access-log
```

Use one API instance for the configured port. Do not start a second host API while the container already owns port 8200. Existing installations must preserve `.env`; `morrow init-local` is only for first-time setup and refuses to replace an existing configuration. Do not switch artifact roots while reusing the same database.

## 3. Create and connect monthly plans

In the Razorpay **Test Mode** dashboard, create two subscription plans:

- **Morrow Plus:** INR ₹499, monthly, interval `1`.
- **Morrow Pro:** INR ₹999, monthly, interval `1`.

Razorpay's API represents these amounts as `49900` and `99900` paise. If using the API independently, the plan fields are `period: "monthly"`, `interval: 1`, and an `item` containing the name, amount, and `currency: "INR"`. Creating plans in the dashboard is sufficient; Morrow does not require a separate plan-creation CLI command. [Plan API](https://razorpay.com/docs/api/payments/subscriptions/create-plan/).

Copy each `plan_…` ID into the corresponding plan in Morrow's existing **People & access** editor. Enter an administration reason and save. The backend checks that the provider plan matches the Morrow amount, currency, and monthly recurrence. Leave Free without a provider plan.

This integration uses quantity `1` with the fixed monthly price. Do not attach trials, add-ons, or offers to these subscriptions; those billing models are not implemented in Morrow's entitlement calculation.

Test and live Razorpay plans are separate. A test `plan_…` ID must not be reused when switching to live credentials. Changing a display name does not change the amount customers are charged; price changes require a matching application/database change and a new provider plan.

## 4. Configure the signed webhook

Register this endpoint in the matching Razorpay environment:

```text
https://YOUR_DASHBOARD_DOMAIN/api/v1/billing/razorpay/webhook
```

Set its signing secret to the same value as `RAZORPAY_WEBHOOK_SECRET`. The production proxy must forward this exact route to the API. It is a public provider callback authenticated by the Razorpay webhook signature, not a browser-login endpoint.

For local testing only, use an HTTPS development tunnel that forwards the webhook route to `http://127.0.0.1:8200/api/v1/billing/razorpay/webhook`. Razorpay cannot deliver a webhook to your machine's `localhost` URL directly. Keep the dashboard's `PUBLIC_ORIGIN` unchanged. Remove the temporary webhook/tunnel when testing is finished, and configure the permanent HTTPS endpoint separately for launch.

Register these events supported by the integration:

```text
subscription.authenticated
subscription.activated
subscription.charged
subscription.completed
subscription.updated
subscription.pending
subscription.halted
subscription.cancelled
subscription.paused
subscription.resumed
invoice.paid
payment.captured
payment.failed
refund.processed
```

They trigger reconciliation of provider state; an event name alone does not grant a plan. See [Razorpay subscription webhooks](https://razorpay.com/docs/payments/subscriptions/subscribe-to-webhooks).

Run `uv run morrow reconcile-billing` from `morrow-api` on a five-minute timer to recover missed renewal notifications. The command uses the restricted application database role and verifies current invoices and payments. It prints only aggregate counts and exits unsuccessfully if any account could not be reconciled, so the timer can alert and retry. It does not create subscriptions or charge customers. For the container installation, build the maintenance image after code changes and run:

```sh
docker compose --profile maintenance build admin
docker compose --profile maintenance run --rm admin reconcile-billing
```

Keep only one scheduled reconciliation job active. The timer is deployment configuration; installing this code does not create a background job on your machine.

Cancellation stops the Razorpay subscription immediately to prevent future debits; Morrow keeps access through the already verified paid invoice period. If the provider response is interrupted, the cancellation intent stays in PostgreSQL and the UI shows confirmation pending. Refresh or the scheduled job checks the provider again. It reports cancellation confirmed only after the provider confirms a terminal state. This avoids depending on an ambiguous scheduled-cancellation flag.

## 5. Test payment and access together

Sign in to Morrow with an ordinary test user and open **Plans**. Select Plus or Pro and complete Razorpay's test checkout using its documented subscription test instruments. A real card is not needed for test transactions. [Test subscriptions](https://razorpay.com/docs/payments/subscriptions/test/).

The checkout handler sends `razorpay_payment_id`, `razorpay_subscription_id`, and `razorpay_signature` to `POST /api/v1/billing/razorpay/verify`. The API checks the signed result and retrieves provider evidence, including the captured payment/invoice and paid subscription period. A checkout success message, redirect, subscription authorization, or client-supplied plan name alone does not grant paid access.

The dashboard checks the server's billing status and refreshes account access after confirmation. If confirmation is delayed, allow the webhook to arrive and use the billing refresh action. An interrupted subscription-creation request blocks another checkout until refresh can recover the original provider subscription, avoiding an accidental second subscription. Do not manually grant a plan to make a failed payment test appear successful.

Check these outcomes before launch:

1. A successful test payment activates only the purchased plan for that user.
2. Closing or failing checkout leaves the user without newly purchased access.
3. A successful renewal extends the paid period; a failed collection does not grant an unpaid period.
4. Replayed or delayed provider callbacks do not create duplicate entitlement or restore expired access.
5. Cancelling renewal preserves already-paid access through the confirmed term, then returns the user to Free.

Use Razorpay's Test Mode **Charge this now** control to simulate later collection outcomes. Test-mode card tokens expire, so follow the current provider testing instructions when running renewal scenarios. Saved application files remain subject to the existing private-file rules after a downgrade.

## Cancellation and plan changes

`POST /api/v1/billing/cancel` stops the provider subscription and keeps Morrow access until the current verified paid period ends. A newly created or merely authorized subscription has no paid access to retain. Morrow does not implement immediate prorated upgrades or downgrades for Razorpay subscriptions. Cancel renewal, keep the current plan through its confirmed paid term, then select the new plan after that term ends.

Changing an administrator-assigned plan is separate from cancelling a provider subscription. Administrative access overrides do not stop billing. Accounts with an administrator-managed plan cannot start a new purchase that would be hidden by that override; use a normal account with subscription-managed access to test checkout. The integration does not promise a hosted receipt URL; payment records remain available in the Razorpay merchant dashboard.

## Costs to confirm before going live

Razorpay's standard domestic gateway pricing is generally **2% platform fee plus 18% GST on that fee**, including standard UPI gateway payments. Standard pricing advertises no setup fee, annual maintenance charge, or minimum monthly fee. Some payment methods and optional services have different rates. [Official pricing](https://razorpay.com/pricing/).

Recurring billing has an additional fee. As checked on **24 October 2026**, the [Subscriptions product page](https://razorpay.com/subscriptions/) advertises a **limited-time 0.5% add-on** per subscription payment, in addition to the underlying payment fee and GST. Razorpay's [pricing explanation](https://razorpay.com/blog/razorpay-payment-gateway-pricing-explained) also lists a **0.99%** recurring charge. Confirm the rate applicable to your merchant account instead of treating the promotional figure as a permanent guarantee. That explanation also describes a temporary new-merchant promotion with a separate KYC processing charge; do not assume a promotional offer makes onboarding or live payments entirely free.

For illustration only, if the 2% gateway rate and 0.5% subscription offer both apply, fees including 18% GST would be approximately ₹14.72 on ₹499 and ₹29.47 on ₹999. This estimate excludes any different instrument rate or optional-service charge. Test Mode uses simulated payments; it is not evidence that real payments have no fees.

Stripe remains an alternative for an approved account, but [new Stripe accounts in India are invite-only](https://support.stripe.com/questions/stripe-accounts-are-invite-only-in-india?locale=en-GB). This is why Razorpay is the practical first integration for India-focused cards and UPI AutoPay.

## Go-live checklist

Keep billing in Test Mode until the advertised paid workflows work for individual users. The [current README](README.md#what-works) describes legacy workflows that still run only for the configured workspace owner; payment integration does not remove that limitation.

When the product and merchant account are ready:

1. Confirm Razorpay activation, approved website, enabled subscription methods, and the actual fee schedule.
2. Create live monthly plans matching Morrow's amounts and currency.
3. Replace the API keys with the matching `rzp_live_…` key pair in the private backend configuration.
4. Configure a live webhook with its own matching signing secret and the permanent HTTPS callback URL.
5. Apply the migration, recreate/restart the API, then save the live `plan_…` mappings in People & access. Complete this configuration before opening live checkout to users.
6. Verify the dashboard uses the intended provider/environment and both live plan mappings pass validation.
7. Confirm real captured payment, server-recorded paid access, cancellation behavior, and provider settlement separately before wider rollout.

Production should use the existing HTTPS dashboard origin, secure cookies, restricted allowed hosts, and the private storage/database deployment described in [README.md](README.md#deployment-shape). Do not publish credentials, private artifacts, browser profiles, or user data in the source repository.

## Existing Stripe compatibility

Use `BILLING_PROVIDER=stripe` to keep Stripe Checkout and its existing billing portal. Stripe continues to use `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `price_…` plan mappings, and `/api/v1/billing/webhook`.

Switching the selected provider does not cancel existing subscriptions or migrate their mandates. Keep the appropriate credentials and webhook handling for subscriptions that are still running on their original provider. Configure Stripe and Razorpay endpoints separately; their payloads and signatures are not interchangeable.
