# Google Cloud setup for Fmaiily

Fmaiily uses the Google OAuth 2.0 authorization-code flow. It needs **one** thing from you: an
OAuth client ID and secret from a Cloud project you control. There is no managed service, no
billing, and no verification application.

Estimated time: 10 minutes.

---

## 1. Create a project

1. Open <https://console.cloud.google.com/projectcreate>.
2. Name it (for example `fmaiily`) and create it. Billing is not required.

## 2. Configure the OAuth consent screen

1. With your project selected, go to **APIs & Services → OAuth consent screen**.
2. Choose **External**. (Internal is only available to Workspace projects and is not needed.)
3. Fill in the required fields: app name, and a support email. Add your own address as a test
   user under **Test users** - while the app is in *Testing* status only listed users can consent.
4. Under **Data Access**, add the scope `.../auth/gmail.send`.
5. Save.

Fmaiily also requests `openid` and `email`. Those are non-sensitive OpenID Connect scopes and are
what let the gateway learn *which* address was connected, because `gmail.send` alone cannot read
the mailbox profile. Add all three to the consent screen:

| Scope                                        | Why                                                              |
|----------------------------------------------|------------------------------------------------------------------|
| `https://www.googleapis.com/auth/gmail.send`  | The only permission needed to send. Treat as the security boundary. |
| `openid`                                     | Standard OIDC scope; used to identify the connected principal.    |
| `email`                                      | Non-sensitive; returns the address the account was connected as. |

> **Why not fewer scopes?** Without `openid`/`email` there is no supported way to learn the
> address of the account that just consented, and `users.getProfile` is not covered by
> `gmail.send`. Adding `gmail.readonly` would work too, but it would grant mailbox read access to
> a service whose entire job is sending mail, so Fmaiily does not ask for it.

## 3. Create the OAuth client

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. **Application type: Web application**.
3. **Name**: `fmaiily` (or anything you will recognise).
4. **Authorized redirect URIs**: add

   ```
   http://localhost:8000/v1/oauth/google/callback
   ```

   The path must match `FMAIILY_OAUTH_REDIRECT_URI` exactly, including the scheme, host, port, and
   trailing path. Google compares this literally.

   - Running on your own machine: the value above is correct.
   - Running in Docker on the same machine: still correct, because the port is published on the
     host and the browser talks to the host, not the container.
   - Running on a server: use `https://your-host/v1/oauth/google/callback`.
5. Create. Copy the **Client ID** and **Client secret**.

## 4. Point Fmaiily at them

```bash
cp .env.example .env
$EDITOR .env
```

```dotenv
FMAIILY_TOKEN_ENCRYPTION_KEY=<output of: fmaiily gen-key>
FMAIILY_GOOGLE_OAUTH_CLIENT_ID=xxxxx.apps.googleusercontent.com
FMAIILY_GOOGLE_OAUTH_CLIENT_SECRET=xxxxx
FMAIILY_OAUTH_REDIRECT_URI=http://localhost:8000/v1/oauth/google/callback
```

The client secret is a credential, not a password: it identifies your application to Google, it
does not grant access on its own. Keep it in `.env` (git-ignored) or a secret manager, not in the
repository.

## 5. Connect an account

```bash
# print the consent URL
docker compose exec fmaiily fmaiily accounts connect
# or, running from a checkout:
uv run fmaiily accounts connect
```

Open the printed URL in a browser, pick the Gmail account, and approve. Google redirects to the
callback, which exchanges the code, identifies the address, encrypts the tokens, and stores the
account.

You can do the same over HTTP:

```bash
curl -s http://localhost:8000/v1/oauth/google/start -H "authorization: Bearer $FMAIILY_KEY"
# -> {"authorization_url": "https://accounts.google.com/...","state": "..."}

curl -s "http://localhost:8000/v1/oauth/google/callback?code=...&state=..." \
     -H "authorization: Bearer $FMAIILY_KEY"
# -> {"account": "you@gmail.com", "account_type": "personal", "scopes": [...]}
```

The `state` parameter is single-use and expires after
`FMAIILY_OAUTH_STATE_TTL_SECONDS` (default 600). Replaying it is rejected.

## 6. Verify

```bash
docker compose exec fmaiily fmaiily status
```

You should see the account, its soft message/recipient limits, and remaining capacity. Then send
one real message:

```bash
docker compose exec fmaiily fmaiily send-test someone@example.com
```

---

## Limits, and what Fmaiily does about them

Gmail enforces these itself; Fmaiily's job is to stay well clear of them so an account is never
locked:

| Limit                                   | Value                                    | Source                                |
|-----------------------------------------|------------------------------------------|---------------------------------------|
| Messages per rolling 24 h, free Gmail    | ~500                                     | Workspace sending limits              |
| Messages per rolling 24 h, Workspace     | 2,000 (500 on trial)                     | Workspace sending limits              |
| Recipients per rolling 24 h, Workspace   | 10,000                                   | Workspace sending limits              |
| Recipients per message                   | 500                                      | Workspace sending limits              |
| `messages.send` API cost                 | 100 quota units                          | Gmail API usage limits                |
| Per-user rate                            | 250 units/user/second moving average     | Gmail API usage limits                |

Fmaiily enforces `limit x FMAIILY_SOFT_LIMIT_RATIO` (default 0.85) and **refuses before calling
Google**, so an agent gets `quota_exceeded` with the exact numbers instead of risking the account.
Pacing (`FMAIILY_DEFAULT_SEND_INTERVAL_SECONDS`, default 2 s) spreads sends per account and is
persisted, so it survives a restart.

A daily-quota rejection from Google is treated as terminal for that job rather than retried:
per Google's own documentation the limit can stay in force for hours, and retrying only spends
more of the remaining budget. The job ends as `daily_send_quota_exceeded` and the quota view shows
when capacity frees up.

---

## Troubleshooting

**`authorization_url` returns "OAuth is not configured"**
`FMAIILY_GOOGLE_OAUTH_CLIENT_ID` or `..._SECRET` is missing. Confirm with
`docker compose exec fmaiily env | grep FMAIILY_GOOGLE`.

**Google says "Error 400: redirect_uri_mismatch"**
The URI in the Cloud console does not match `FMAIILY_OAUTH_REDIRECT_URI` character for character.
Note the scheme (`http` vs `https`), the port, and any trailing slash.

**Google says "Access blocked" or the consent screen is not shown**
The app is in *Testing* and the account is not listed under **Test users**, or the consent screen
is missing the `gmail.send` scope. Both are fixable in the console without going through Google's
verification process, because the app stays in external/testing mode.

**`403 insufficientPermissions` on send**
The connected account no longer holds `gmail.send`. Reconnect it, and check the account's scopes
with `fmaiily accounts list`.

**`token refresh failed` / `account_not_found`**
`FMAIILY_TOKEN_ENCRYPTION_KEY` changed after the account was connected, so the stored token cannot
be decrypted. Reconnect the account. This key must be backed up like a database password.

**Drafts are unavailable**
Draft mode is a later phase and needs the `gmail.compose` scope. An account connected with only
`gmail.send` cannot create drafts; Fmaiily reports `scope_missing` rather than failing obscurely.

---

## Keeping the connection alive

Google access tokens live about an hour. Fmaiily refreshes them automatically, ahead of expiry, and
retries once on a `401`. A successful refresh also persists a rotated refresh token, so a long-lived
deployment does not need periodic reconnection.

If a refresh fails, the previous token is kept and the account is marked `error` so a transient
Google outage does not destroy a working session. `fmaiily status` shows which account is affected.

---

## Your responsibilities

Fmaiily makes sending convenient; it does not make it lawful. You remain responsible for:

- complying with the Gmail Terms of Service and Google Workspace acceptable-use policies;
- only sending to recipients who have consented or otherwise expect the message;
- not using the gateway for bulk or unsolicited mail.

Fmaiily will not hide Gmail's limits from you, and it will not bypass them. It deliberately paces
and refuses rather than pushing an account to the edge of what Google tolerates.

## References

- Gmail API usage limits: <https://developers.google.com/workspace/gmail/api/reference/quota>
- Gmail sending limits: <https://knowledge.workspace.google.com/admin/gmail/gmail-sending-limits-in-google-workspace>
- OAuth scopes: <https://developers.google.com/workspace/gmail/api/auth/scopes>
