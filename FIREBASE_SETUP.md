# Firebase authentication for the first 100 users

The main dashboard now uses **Firebase Authentication** for project **`morrow-91e98`**. Open **[http://localhost:8100/#/login](http://localhost:8100/#/login)**, choose **Continue with Google**, and use **`semrisoujanya2003@gmail.com`** for administrator access. The existing workspace has been linked to this verified Google identity; the old `admin@example.com` password and sessions have been revoked. Administrators have access to all nine modules regardless of the billing plan label. A Free label on this administrator does not limit access or represent a paid Stripe subscription.

**Google and Email/Password are enabled, `localhost` is authorized, and email-enumeration protection is enabled.** The public web configuration is saved in the ignored `morrow-api/.env`. Live Google sign-in and the application session exchange succeeded in the isolated setup workspace. The main API is healthy, its runtime credential reads the verified owner, and the main dashboard's Google button opens Google's sign-in page. Unauthenticated private API requests return `401`. The owner must now complete a fresh sign-in on the main dashboard. Firebase inbox delivery is not yet verified, and app notifications still use private email previews.

Cloud checks confirmed that project `morrow-91e98` is active and its project number is `889389012510`. The dedicated service account `morrow-auth-runtime@morrow-91e98.iam.gserviceaccount.com` has `roles/firebaseauth.viewer`. Its ignored `.runtime/firebase-admin.json` credential has permissions `0600`, and `.env` points `FIREBASE_CREDENTIALS_FILE` to that file. An Admin SDK lookup verified the owner's enabled, verified Firebase identity, and the Docker credential-read check passed. The audited link preserved the existing user and tenant IDs, administrator role, **292 applications, 635 artifacts, one profile, and 2,754 records**. Exactly one `admin.firebase.link` audit entry records the migration.

The temporary setup preview on port **8103** and its API on port **8203** have been stopped. Its separate database `morrow_firebase_preview` contains an ordinary Free account, separate from the administrator workspace. Use **port 8100** for your imported data and administrator access.

## Recommended services

Use **Firebase Authentication** for Google sign-in, email/password accounts, verification emails, and password resets. Keep PostgreSQL for profiles, roles, plan entitlements, Stripe subscriptions, audit logs, and private artifacts. Keep the existing Morrow dashboard as the only frontend.

| Service | Current free allowance | Use here |
| --- | --- | --- |
| Firebase Authentication, Spark | 1,000 verification emails/day; 150 password resets/day. Identity Platform Spark permits 3,000 Tier 1 daily active users. | Authentication and security emails |
| Clerk Hobby | 50,000 monthly retained users; custom email templates and removing branding from prebuilt UI require a paid plan. | Considered; Firebase selected for this custom UI |
| Resend Free | 3,000 transactional emails/month, at most 100/day; requires a verified sending domain | Optional app notifications, separate from Firebase security emails |

These authentication allowances cover an initial 100-user rollout, subject to provider abuse controls. They do not cover hosting, domain registration, Stripe fees, or Claude/Kiro subscriptions. Firebase's free **email-link sign-in** allowance is only five emails/day; this implementation uses email/password and Google instead. Firebase also limits account creation to 100/hour per client IP.

Checked 23 October 2026: [Firebase limits](https://firebase.google.com/docs/auth/limits), [Clerk pricing](https://clerk.com/pricing), [Resend quotas](https://resend.com/docs/knowledge-base/account-quotas-and-limits). Recheck before launch because providers can change limits.

## 1. Firebase project setup

1. Open the existing [Morrow project](https://console.firebase.google.com/project/morrow-91e98/overview). Analytics is optional; this app does not need Firestore, Firebase Hosting, or Cloud Functions.
2. The registered Web app's public `apiKey`, `authDomain`, `projectId`, and `appId` are already saved. Authentication does not require `storageBucket`, `messagingSenderId`, or `measurementId`, and the dashboard uses its installed Firebase SDK rather than an additional script tag.
3. [Authentication for morrow-91e98](https://console.firebase.google.com/project/morrow-91e98/authentication) is initialized, and **Email/Password** and **Google** are enabled. No Identity Platform upgrade or billing change has been performed.
4. `localhost` is already an authorized domain. Add the eventual website hostname before deployment. The main dashboard and its `PUBLIC_ORIGIN` use `http://localhost:8100`. Use this exact hostname for sign-in; `127.0.0.1` is not the configured browser origin.
5. Email-enumeration protection is enabled. Firebase now enforces the UI's 12–128 character password policy for new passwords, without forcing existing users to change their password on sign-in. Keep Firebase's default one-account-per-email behavior.

The project and public web configuration identify your Firebase app. They do not grant administrator access in Morrow. That access comes from your PostgreSQL user record.

References: [web setup](https://firebase.google.com/docs/auth/web/start), [Google sign-in](https://firebase.google.com/docs/auth/web/google-signin), [password policy](https://firebase.google.com/docs/auth/web/password-auth).

## 2. Configure email actions

In Authentication → Templates, set the app name and support details to Morrow. Firebase can send verification and reset emails itself; no Resend account or SMTP password is needed for these messages.

For email actions to use Morrow's matching screens, set the custom action URL to your dashboard root, such as `https://app.example.com/`. For local testing use `http://localhost:8100/`. Firebase adds `mode` and `oobCode` parameters. The dashboard maps these to its verification/reset screens, removes the parameters from the query string, and validates the one-time code through Firebase. It ignores arbitrary return URLs and API keys supplied in links.

The default Firebase-hosted action handler also works if you leave the template action URL unchanged; its Continue button returns to the dashboard. This first version supports verification and password reset, not a self-service email-address change flow.

References: [verification and reset emails](https://firebase.google.com/docs/auth/web/manage-users), [custom action handlers](https://firebase.google.com/docs/auth/custom-email-handler).

## 3. Configure the backend

Preserve the existing database passwords and session secret in the ignored `morrow-api/.env`. The main application is configured as follows:

```dotenv
AUTH_PROVIDER=firebase
FIREBASE_PROJECT_ID=morrow-91e98
FIREBASE_API_KEY=the-existing-public-web-api-key
FIREBASE_AUTH_DOMAIN=morrow-91e98.firebaseapp.com
FIREBASE_APP_ID=1:889389012510:web:d83a158b5ab708ae6f000f
PUBLIC_ORIGIN=http://localhost:8100
COOKIE_SECURE=false
FIREBASE_CREDENTIALS_FILE=./.runtime/firebase-admin.json
```

For a public deployment use your HTTPS origin, `COOKIE_SECURE=true`, and the real hostnames in `ALLOWED_HOSTS`. Never expose the API or PostgreSQL ports directly to the internet. The frontend's ignored `.env.local` has `MORROW_API_URL=http://127.0.0.1:8200` and `MORROW_WORKSPACE_OWNER_EMAIL=semrisoujanya2003@gmail.com`; it needs no private Firebase key. The backend loopback address is separate from the browser's configured `localhost` origin.

The current machine already has the private `.runtime/firebase-admin.json` credential for `morrow-auth-runtime@morrow-91e98.iam.gserviceaccount.com`; no new download is needed. The runtime verifies tokens and reads Firebase user records, so the read-only `roles/firebaseauth.viewer` role is sufficient. It does not need account-management or token-creation permissions. [Firebase Authentication IAM roles](https://docs.cloud.google.com/iam/docs/roles-permissions/firebaseauth)

For a replacement credential or another non-Google host, use a dedicated service account for this project with the same read-only role and save its credential privately. [Project settings → Service accounts](https://console.firebase.google.com/project/morrow-91e98/settings/serviceaccounts/adminsdk) provides the Firebase Admin setup instructions. Keep the file at permissions `0600`; do not commit it, paste it into chat, or place it in frontend code. On Google infrastructure, prefer an attached service identity with Application Default Credentials instead of a downloaded key.

`firebase login` authenticates the Firebase CLI; it does not supply credentials to the Python Admin SDK. Ordinary credentials from `gcloud auth application-default login` also need additional setup for Firebase Authentication: Firebase rejects the standard gcloud OAuth client ID. For local end-user credentials, Firebase documents using a custom **Desktop app** OAuth client with `--client-id-file`; the backend already supplies the project ID explicitly. A project service-account credential or an attached service identity avoids that local OAuth setup. [Admin SDK credential requirements](https://firebase.google.com/docs/admin/setup#test_with_gcloud_end_user_credentials), [Firebase CLI authentication](https://firebase.google.com/docs/cli#sign-in-test-cli)

The Docker credential-read check has passed for the current file. Repeat it after moving or replacing the credential. The Docker service runs as UID 1000. File-backed Compose secrets preserve the host file's ownership and permissions, so a `0600` file belonging to another host UID can be unreadable inside the container. Check read access without displaying the key:

```sh
docker compose -f compose.yaml -f compose.firebase.yaml run --rm --no-deps --entrypoint python api -c 'import os; f=open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"], "rb"); f.close(); print("Firebase credential is readable")'
```

If this reports a permission error, give **only this credential file** ownership compatible with the container service, then rerun the check:

```sh
sudo chown 1000:1000 .runtime/firebase-admin.json
sudo chmod 600 .runtime/firebase-admin.json
```

This assigns the file to the container's service UID; managing it from a different host account may subsequently require administrator rights. Keep it owned by your host account instead when running Python directly. Do not make the key world-readable or change ownership of the workspace. Compose secret `uid`/`mode` settings cannot fix this for file-backed secrets. [Docker secret behavior](https://docs.docker.com/reference/compose-file/services/#secrets)

Start or restart the configured Docker stack with its secret overlay:

```sh
docker compose -f compose.yaml -f compose.firebase.yaml up -d --build
```

The overlay mounts the credential read-only. Migrations run before the API starts. The Admin SDK has authenticated successfully and read the verified owner's identity. For direct Python execution, set `GOOGLE_APPLICATION_CREDENTIALS` to the private file's absolute path; the preview script does this automatically.

The browser keeps Firebase authentication state in memory, exchanges a verified ID token plus a CSRF challenge for an HttpOnly Morrow cookie, then signs out of the Firebase client. The backend checks token signatures, project identity, recent authentication, verified email, disabled users, and revocation. It continues checking revocation on authenticated requests. Firebase failures deny access rather than bypassing identity checks. Local password routes are disabled in Firebase mode.

`AUTH_IP_ATTEMPT_LIMIT` defaults to 1,000 attempts per 15 minutes. Requests through the dashboard share its gateway address, so this is an aggregate cap; the per-identifier cap stays at 10. The API does not trust arbitrary forwarded client-IP headers. These are abuse limits, not a measured concurrent-user capacity guarantee.

## 4. Administrator migration record and reference procedure

**This migration is complete. Do not rerun the link command for this workspace.** The imported administrator previously named `admin@example.com` now signs in with Google as **`semrisoujanya2003@gmail.com`** on **[port 8100](http://localhost:8100/#/login)**. The link preserved the same user and tenant, administrator role, all nine module permissions, 292 applications, 635 artifacts, one profile, and 2,754 records. The `admin.firebase.link` audit event records the change. The private `.runtime/bootstrap-admin.txt` now contains Google sign-in instructions instead of the obsolete local login.

The following procedure documents the staged migration for reference or a separate existing local workspace. Recording an intended email alone never grants access. Do not create a second administrator or copy a preview account over the imported tenant.

For a separate migration, an isolated preview can establish the verified Firebase identity before disabling the main workspace's local login. Start its API from `morrow-api`:

```sh
.venv/bin/python scripts/firebase_preview.py
```

Start the existing dashboard on the preview port from `froned-dashbord`:

```sh
MORROW_API_URL=http://127.0.0.1:8203 MORROW_WORKSPACE_OWNER_EMAIL='' npm start -- --port 8103
```

The script uses real Firebase, a fresh session secret, separate artifact/mail directories, and the marked database `morrow_firebase_preview`. It refuses an unrelated database with that name. It seeds no users, imports no existing data, and disables Stripe and external app-mail delivery. A signed-in preview user receives an ordinary Free workspace in that separate database. That preview account does not carry the main administrator's role or data.

For an existing workspace that has **not** been migrated, keep its main API in local mode while completing these checks:

1. Open the existing dashboard's Firebase preview, choose **Continue with Google**, and sign in with the real owner's Google account. Complete Google consent and verify that the application issues a session. Do not enable Firebase on the main API just to create this identity.
2. Confirm the email is verified and copy the Firebase UID. Creating a user in the Firebase console alone does not establish ownership of the inbox. Do not manually mark an unverified address as verified to bypass this step.
3. Confirm the configured server credential can read that user in `morrow-91e98`. Prepare the dashboard's local origin and restart configuration before changing the main account.

Once those checks succeed, perform the link and provider switch together during a brief maintenance window. The link command works while `AUTH_PROVIDER=local`, but **immediately removes the old local password and sessions**. The completed migration used this command shape; substitute the appropriate existing account and verified owner only for a different migration:

```sh
docker compose -f compose.yaml -f compose.firebase.yaml run --rm --build admin link-firebase-user --email EXISTING_LOCAL_EMAIL --firebase-uid VERIFIED_FIREBASE_UID --new-email VERIFIED_OWNER_EMAIL --reason "Move the imported administrator workspace to its verified Firebase owner"
```

After a successful link, finish the coordinated cutover. These configuration steps are already complete for the current workspace:

1. Set `AUTH_PROVIDER=firebase` in `morrow-api/.env` and use the configured dashboard origin, for example `PUBLIC_ORIGIN=http://localhost:8100`.
2. Set `MORROW_WORKSPACE_OWNER_EMAIL=semrisoujanya2003@gmail.com` in `froned-dashbord/.env.local`.
3. Restart the API with `docker compose -f compose.yaml -f compose.firebase.yaml up -d --build`, then restart the existing dashboard with `npm start`.
4. Sign in as the verified owner. Check the existing tenant, administrator role, plan, profile, 292 imported applications, and 635 artifacts, and confirm the `admin.firebase.link` audit record.

Automatic linking by email is unavailable. If the Firebase UID already owns another PostgreSQL account in the main database, the command rejects the conflict rather than merging two users' data. Switching only `AUTH_PROVIDER` back to `local` does not restore the removed password; avoid a premature cutover.

## 5. Optional emails through Resend

Firebase sends authentication emails independently. For other transactional messages, create a Resend account, verify a domain you own with the supplied DNS records, and configure:

```dotenv
MAIL_BACKEND=resend
MAIL_FROM=Morrow <hello@your-verified-domain.example>
RESEND_API_KEY=your-private-sending-api-key
```

Restart the API after changing these values. The backend's existing mail queue supports branded HTML plus plaintext, stable idempotency keys, retry backoff, and expired-link cleanup. The private key stays on the backend. The free daily cap is shared across recipients: sending 100 welcome emails leaves no further Resend allowance that day. Firebase verification/reset mail does not consume that Resend quota.

Schedule a one-minute job to retry queued mail using the same deployment settings:

```sh
docker compose -f compose.yaml -f compose.firebase.yaml run --rm admin send-mail
```

No bulk-mail or arbitrary-recipient endpoint has been exposed to users. App features enqueue their own intended notifications. Preview `.eml` files are not proof of inbox delivery.

## 6. Check before inviting the 100 users

- Sign up using an inbox you control; confirm delivery and that an unverified account cannot open a workspace.
- Verify the email and sign in; repeat with Google on desktop and mobile.
- Request a password reset; confirm the new password works and previous sessions lose access.
- Test an expired/reused action link, popup cancellation, and an unavailable network.
- Confirm a regular user cannot access the administrator area or another user's files.
- Confirm the migrated administrator retains the imported data and audited access.
- Confirm Stripe remains in test mode until paid workflows are ready. Authentication setup does not connect individual automation runtimes or a user's Claude/Kiro session.

Automated tests cover the Firebase exchange with a mocked provider, UI flows with synthetic accounts, client token handling, and mail delivery with mocked transports. Live Google consent and application session creation succeeded in the isolated setup workspace. Main-workspace checks passed for the administrator link, preserved data, all nine administrator module permissions, API health, runtime identity lookup, Google popup, and unauthenticated private-endpoint rejection. Completing a fresh main-dashboard Google sign-in requires the owner's browser session. Firebase email delivery, DNS verification, and mailbox placement are not yet verified.
