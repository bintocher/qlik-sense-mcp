# Login/password (form) authentication for qlik-sense-mcp

This mode lets the MCP authenticate against a Qlik virtual proxy configured
with a **"Form based" authentication method** — most commonly the in-box
login page that validates Windows credentials — using a plain username and
password instead of a client certificate or a JWT.

Form mode is selected automatically when `QLIK_PASSWORD` is set in the
environment (unless `QLIK_JWT_TOKEN` is also set — JWT takes priority).
Certificate mode and JWT mode are both unchanged.

---

## How it works under the hood

Unlike JWT (see [AUTH_JWT.md](AUTH_JWT.md)), there is no single documented
wire format for Qlik's form-based login — the login page is ordinary HTML
with a `<form>`, rendered by whichever auth module the virtual proxy is
configured with. The MCP drives it the way a browser would:

1. **GET** the entry point — the virtual proxy root by default — and
   follow every redirect. This matters: the real login URL is not a fixed
   path. Verified against a live deployment, Qlik's login page lives at
   `internal_forms_authentication/?targetId=<guid>`, where `targetId` is
   generated fresh on every visit; POSTing to the page without it fails
   with `400 Virtual proxy not possible to determine, since neither
   TargetId nor Virtual Proxy were specified`. A browser never hits that
   URL directly — it starts at the hub or the proxy root and lands there
   through Qlik's own redirect chain — so the MCP reproduces that same
   starting point instead of guessing the login URL. Override
   `QLIK_FORM_LOGIN_PATH` only if your deployment's login flow starts
   somewhere other than the virtual proxy root.
2. **Parse** the page Qlik redirected to for the first `<form>` that
   contains an `<input type="password">` — that is the login form. Hidden
   fields (anti-forgery tokens, return URLs, `targetId` if it lives in a
   hidden field rather than the URL, ...) are captured and resent
   unchanged. The username field is detected as the first text/email input
   whose name suggests an identity field (`user`, `login`, `email`,
   `account`), falling back to the first text field found. Override with
   `QLIK_FORM_USERNAME_FIELD` / `QLIK_FORM_PASSWORD_FIELD` if detection
   picks the wrong field.
3. **POST** the credentials — hidden fields plus username and password — to
   the form's `action` URL, or the landed-on URL itself (`targetId`
   included) when the form has no `action`, following redirects. The
   username value is `QLIK_USER_DIRECTORY\QLIK_USER_ID` when a directory
   is set, otherwise just `QLIK_USER_ID` (the conventional Windows login
   format — verified as the exact placeholder text Qlik's own login page
   shows).
4. Whatever Qlik session cookie shows up afterwards (matched the same way
   JWT mode matches it — conventionally `X-Qlik-Session-<prefix>`, or
   plain `X-Qlik-Session` on the central proxy) is the bootstrapped
   session.
5. **GET** `{vp}/qps/csrftoken` — the same anti-CSWSH endpoint JWT mode's
   phase 1 uses — now authenticated by the cookie instead of a bearer
   token, to obtain `qlik-csrf-token`.

From that point on, a form session behaves exactly like a JWT session after
its own bootstrap: the session cookie and CSRF token authenticate every QRS
call and the Engine WebSocket upgrade, and no credentials are sent again
until the session expires (same ~25 minute TTL as JWT mode). Re-bootstrap
also triggers on QRS `401`, on `302` (a missing session cookie is answered
with a redirect back to the login page rather than a clean 401), and on
`500 Authentication error: Restart the browser.` (a present but
server-unrecognized cookie — verified after simulating a proxy failover);
the Engine WebSocket side retries the same way when the upgrade succeeds
but Engine closes the socket without a greeting.

Implementation: [`form_session.py`](../qlik_sense_mcp_server/form_session.py).

---

## Configuration

Minimal config:

```jsonc
{
  "mcpServers": {
    "qlik-sense": {
      "command": "uvx",
      "args": ["qlik-sense-mcp-server"],
      "env": {
        "QLIK_SERVER_URL": "https://qlik.company.com/forms",
        "QLIK_USER_DIRECTORY": "COMPANY",
        "QLIK_USER_ID": "your-username",
        "QLIK_PASSWORD": "your-password"
      }
    }
  }
}
```

- **`QLIK_SERVER_URL`** — the Qlik hostname, optionally with the virtual
  proxy prefix as URL path. Unlike JWT, form mode does **not** require a
  prefix — a form-based auth module can be attached to the central proxy.
- **`QLIK_USER_DIRECTORY`** — optional. The domain part of the login,
  joined with `QLIK_USER_ID` as `DOMAIN\username`. Leave unset if your
  module expects a bare username or a UPN (`user@domain.com`) — in that
  case put the full string in `QLIK_USER_ID` instead.
- **`QLIK_USER_ID`** — required. The username submitted to the login form.
- **`QLIK_PASSWORD`** — required. Treat it like any other password: it is
  sent once per session bootstrap (~every 25 minutes) and never logged.

### Optional overrides

You do not normally need these — they exist because the login page's exact
shape depends on the auth module your admin configured.

| Variable | Purpose | Default |
|---|---|---|
| `QLIK_FORM_LOGIN_PATH` | Path under the virtual proxy to start the login flow from, before any redirect | the virtual proxy root |
| `QLIK_FORM_USERNAME_FIELD` | Force the login form's username field name | auto-detected |
| `QLIK_FORM_PASSWORD_FIELD` | Force the login form's password field name | auto-detected |
| `QLIK_VERIFY_SSL` | `true` enables TLS verification | `false` |
| `QLIK_CA_CERT_PATH` | Path to a corporate CA bundle | unset |

---

## Reload-task administration (optional)

The 14 reload-task tools (`get_tasks`, `create_task`, `start_task`, ...)
are off by default in form mode, same as in JWT mode — they need QRS
repository-admin rights, which an ordinary analyst identity does not have.

That said, QRS checks the QMC role behind the authenticated session, not
how the session was authenticated — a form login that maps to a
`RootAdmin`/`ContentAdmin` identity (or an equivalent custom role) has
exactly the same QRS access a certificate-mode service account would.
Verified directly: `reloadtask/count` and a full task listing succeeded
against a form-mode session backed by such an identity.

If you know the identity behind your `QLIK_USER_ID` has these rights, set:

```
QLIK_TASK_TOOLS=true
```

to register the task tools in form mode too. If it does not, every task
call will fail with 403 — the server does not (and cannot) check this for
you ahead of time.

---

## Session limit — same rule as JWT mode

**Qlik allows at most 5 concurrent sessions per user identity**, and the
login POST creates one before any WebSocket exists — see
[AUTH_JWT.md § 2.4](AUTH_JWT.md#24-session-limit--run-one-session-per-token)
for the full explanation and practical rules. They apply identically here:
run one MCP server process per credential, keep it running, and don't
start the same config from several clients at once.

---

## Troubleshooting

### `could not find a login form (an <input type="password"> field) on ...`

The redirect chain from `QLIK_FORM_LOGIN_PATH` (the virtual proxy root by
default) did not land on a page with a login form — most likely because
the login flow on your deployment does not start there. Open the virtual
proxy's URL in a browser, log in manually, and check the network tab for
where the redirect chain actually starts; set `QLIK_FORM_LOGIN_PATH`
to that path if it differs from the proxy root.

### `could not detect the login form's username/password field names`

The login page has a password field but no text/email field the detector
recognises (or none at all — unlikely for a login form). Inspect the
page's HTML and set `QLIK_FORM_USERNAME_FIELD` / `QLIK_FORM_PASSWORD_FIELD`
explicitly to the exact `name="..."` attributes.

### `login did not produce a Qlik session cookie`

The POST went through but Qlik did not consider it a successful login.
Most common causes:

1. **Wrong credentials.** Verify by logging in manually in a browser with
   the same `QLIK_USER_DIRECTORY` / `QLIK_USER_ID` / `QLIK_PASSWORD`.
2. **Wrong field names guessed.** The POST silently reached the page but
   with fields the auth module ignores. Set `QLIK_FORM_USERNAME_FIELD` /
   `QLIK_FORM_PASSWORD_FIELD` explicitly.
3. **A hidden anti-forgery field was not carried through correctly** — some
   modules bind the token to the specific GET that served it, so retrying
   after a partial failure needs a fresh bootstrap (`invalidate()` handles
   this automatically on the next call).

### Everything looks right but requests still fail

Turn on debug logging:

```
LOG_LEVEL=DEBUG qlik-sense-mcp-server --stdio
```

The log shows the login URL, the field names it detected, the session
cookie name that was picked up, and whether `qlik-csrf-token` was received.

---

## Security notes

- `QLIK_PASSWORD` is a real password — treat your MCP config file the same
  way you would treat a config file containing any other secret.
- A stolen password lets the attacker log in as that user through the
  virtual proxy until the password is changed. It does not, unlike a
  compromised JWT signing key, expose other users.
- Prefer JWT mode ([AUTH_JWT.md](AUTH_JWT.md)) when your Qlik deployment
  supports it: a JWT can be scoped and rotated without changing a live
  password, and nothing as sensitive as a password sits in the config file.

---

## Related

- [`docs/AUTH_JWT.md`](AUTH_JWT.md) — the other per-analyst authentication
  mode, including the session-limit rules that apply here unchanged.
- [`docs/configuration.md`](configuration.md) — full environment variable
  index.
- [`docs/troubleshooting.md`](troubleshooting.md) — general troubleshooting.
- [`README.md`](../README.md) — project overview and quick start.
