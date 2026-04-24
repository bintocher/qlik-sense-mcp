# JWT authentication for qlik-sense-mcp

This mode lets an analyst use `qlik-sense-mcp-server` from their own Cursor /
Claude Code instance with **nothing on disk but a bearer token string**. No
client certificates, no private keys, no service account credentials. All
secrets stay with the admin who runs `tools/qlik_jwt_admin.py`.

The admin signs long-lived JWTs with a private key that lives only on the
admin's machine. Each JWT encodes a single analyst's domain identity. The
analyst copies the token into two environment variables of their MCP config —
that is it. Qlik applies that analyst's own security rules, stream
membership, and Section Access automatically.

Certificate mode (legacy) still works unchanged for admins who need full QRS
access to the Qlik server. JWT mode is selected automatically when
`QLIK_JWT_TOKEN` is set in the environment.

---

## Part 1 — What the admin does once

### 1.1 Generate the signing keys

```bash
python tools/qlik_jwt_admin.py init-keys --out ./jwt_keys
```

This produces three files in `./jwt_keys/`:

| File | Purpose | Lives where |
|---|---|---|
| `jwt_private.pem` | Private RSA key, used to sign tokens | Admin machine only. Never copy to analysts. |
| `jwt_public.pem` | Public key (informational) | Anywhere — not sensitive. |
| `jwt_cert.pem` | Self-signed X.509 certificate with the public key | Pasted once into QMC. |

Keep `jwt_private.pem` safe. Anyone who obtains it can impersonate any user
in the configured virtual proxy until you rotate the key in QMC.

### 1.2 Create a JWT virtual proxy in QMC

Open `https://<your-qlik-host>/qmc` → **Configure system** → **Virtual
proxies** → **Create new** and fill in the following values. Every value
marked *default* matches what the MCP assumes out of the box — change it
only if you have a good reason.

| Field | Value | Notes |
|---|---|---|
| **Description** | `JWT for qlik-sense-mcp` | Free-form. |
| **Prefix** | `jwt` *(default)* | Lowercase. Must match the URL path you give analysts in `QLIK_SERVER_URL`. |
| **Session cookie header name** | `X-Qlik-Session-jwt` *(default)* | Must be unique across virtual proxies. The MCP auto-detects this from `Set-Cookie`, so the exact name is not critical — any `X-Qlik-Session*` works. |
| **Authentication method** | `JWT` | |
| **JWT certificate** | Paste the full contents of `./jwt_keys/jwt_cert.pem`, including the `-----BEGIN CERTIFICATE-----` lines | |
| **JWT attribute for user ID** | `userId` *(default)* | Must match `--user-id-claim` when issuing tokens. |
| **JWT attribute for user directory** | `userDirectory` *(default)* | Must match `--user-dir-claim`. |
| **Load balancing** → **Load balancing nodes** | Select your Engine node (usually Central) | |
| **Advanced** → **Host allow list** | Add the exact hostname your analysts will put into `QLIK_SERVER_URL`, e.g. `qlik.company.com` | **Hostname only**, no scheme, no port, no IP addresses. Origin validation is strict. |
| **Link** | Link the virtual proxy to **every** Proxy service whose hostname is in the Host allow list above — and **always also to the Central Proxy**, even when analysts will never hit the Central hostname. | In a multi-node cluster, any prefixed VP that is not linked to the Central Proxy is rejected by all proxy services with HTTP 400 ("The http request header is incorrect") regardless of whether the receiving node has its own link. Single-node deployments get this for free. |

Save. Wait 10–30 seconds for the proxy to restart.

### 1.3 Issue a token for an analyst

For each analyst you want to enable:

```bash
python tools/qlik_jwt_admin.py issue-token \
    --key ./jwt_keys/jwt_private.pem \
    --user-id ivanov \
    --user-directory COMPANY
```

`--user-id` and `--user-directory` must match the values Qlik uses for that
user (usually AD `sAMAccountName` and the domain short name). The defaults
`--days 365`, `--user-id-claim userId`, and `--user-dir-claim userDirectory`
match the QMC configuration above.

The CLI prints a ready-to-paste token plus the `env` block for the analyst's
MCP config.

For long-lived service tokens, pass a larger value:

```bash
python tools/qlik_jwt_admin.py issue-token \
    --key ./jwt_keys/jwt_private.pem \
    --user-id svc_ai \
    --user-directory COMPANY \
    --days 3650
```

For scripting:

```bash
TOKEN=$(python tools/qlik_jwt_admin.py issue-token --key ... --user-id ivanov --user-directory COMPANY --quiet)
```

### 1.4 Revocation

Qlik does not provide per-token revocation. Your options are:

- **Standard case — the analyst leaves or changes role.** Disable or remove
  the domain user in Qlik (via QMC or AD sync). A valid JWT for a disabled
  user is rejected by the Qlik proxy — no MCP change required.

- **Emergency — the signing key is compromised.** Generate a new keypair
  (`init-keys --out ./jwt_keys_new`), paste the new `jwt_cert.pem` into the
  virtual proxy, reissue tokens for everyone. Replacing the certificate in
  QMC invalidates every token that was signed with the old key at once.

---

## Part 2 — What the analyst does

### 2.1 Install

Same as the regular install path. See `docs/installation.md`.

### 2.2 Configure Cursor / Claude Code

Add the following to the MCP config (e.g. `.cursor/mcp.json` or the
equivalent for your client):

```json
{
  "mcpServers": {
    "qlik": {
      "command": "qlik-sense-mcp-server",
      "args": ["--stdio"],
      "env": {
        "QLIK_SERVER_URL": "https://qlik.company.com/jwt",
        "QLIK_JWT_TOKEN": "eyJhbGciOiJSUzI1NiJ9...."
      }
    }
  }
}
```

Two variables — that is the whole configuration.

- **`QLIK_SERVER_URL`** — the Qlik hostname with the virtual proxy prefix as
  URL path. If the admin used the default prefix `jwt`, the path is `/jwt`.
- **`QLIK_JWT_TOKEN`** — the token string the admin gave you. Treat it like
  a password.

Restart Cursor. The MCP server connects to Qlik on first tool call, all
operations run under the analyst's own Qlik identity, and Qlik security
rules apply normally.

### 2.3 Optional overrides

You do not normally need these. Use them only if your admin configured
something non-standard.

| Variable | Purpose | Default |
|---|---|---|
| `QLIK_JWT_USER_ID_CLAIM` | Name of the payload claim holding the user id | `userId` |
| `QLIK_JWT_USER_DIR_CLAIM` | Name of the payload claim holding the user directory | `userDirectory` |
| `QLIK_JWT_SESSION_COOKIE` | Exact name of the VP session cookie | auto-detected from bootstrap response |
| `QLIK_VERIFY_SSL` | `false` disables TLS verification | `true` |
| `QLIK_CA_CERT_PATH` | Path to a corporate CA bundle | unset |

---

## How it works under the hood

The MCP performs a two-phase handshake the first time a tool needs Qlik.

**Phase 1** — the MCP calls `GET https://<host>/<vp_prefix>/qps/csrftoken`
with `Authorization: Bearer <jwt>`. The Qlik virtual proxy validates the JWT
against the certificate configured in QMC, creates a session, and returns:

- `Set-Cookie: X-Qlik-Session-<prefix>=<value>`
- HTTP header `qlik-csrf-token: <value>`

**Phase 2** — every subsequent request carries the session cookie. HTTP QRS
calls also include `qlik-csrf-token`. The Engine WebSocket upgrade sends
the cookie, the CSRF token **both as a header and appended to the URL as
`?qlik-csrf-token=<value>` query parameter**, and an `Origin` header
pointing at the Qlik host — but **not** `Authorization: Bearer`, because
Qlik November 2024+ rejects that on WebSocket upgrades under its CSWSH
protection. Empirically, the query-parameter form of the CSRF token is what
Qlik actually validates on the WS upgrade; sending it only as a header
still returns 403. This two-phase flow is Qlik's official path and is safe
on older versions too (the extra header/query is simply ignored
pre-November 2024).

The MCP re-bootstraps automatically on session expiry (every ~25 minutes or
on a 401 response).

---

## Troubleshooting

### `csrftoken returned 401 — JWT rejected by the virtual proxy`

Most common causes:

1. **The token is expired.** Check the `exp` claim. Reissue with a larger
   `--days`.
2. **Clock skew.** The `iat` claim is backdated 30 s by `issue-token` to
   mitigate small skew. If the Qlik server is more than that ahead of the
   admin's machine, sync NTP.
3. **Claim name mismatch.** The QMC "JWT attribute for user ID / user
   directory" fields must exactly match `--user-id-claim` /
   `--user-dir-claim` (case-sensitive). Defaults are `userId` /
   `userDirectory`.
4. **Wrong certificate in QMC.** If you regenerated keys but forgot to paste
   the new `jwt_cert.pem`, the VP still trusts the old key and rejects the
   newly-signed tokens.
5. **User unknown to Qlik.** The payload `userId` / `userDirectory` must
   identify a user that Qlik recognizes. Disabled / nonexistent users are
   rejected.

### `csrftoken returned 400 — VP is not linked to Central Proxy`

In a multi-node Qlik cluster, any request to a prefixed virtual proxy
whose VP is not linked to the Central Proxy is answered with HTTP 400
"The http request header is incorrect" — a ~88 KB styled Qlik error page,
identical across all prefixes and even for prefixes that do not exist.
The fix is to link the JWT VP to the Central Proxy in QMC (Proxies →
Central → Associated items → Virtual proxies → Link), in addition to any
node-specific proxy that hosts the public hostname. See section 1.2 above.

### `csrftoken returned 403 — VP refused the request`

Host allow list mismatch. Add the exact hostname from `QLIK_SERVER_URL` to
the virtual proxy's **Advanced → Host allow list** in QMC. Hostname only, no
scheme, no port, no IP.

### WebSocket fails with `Handshake status 403 Forbidden`

On Qlik November 2024+ the WebSocket upgrade is rejected under CSWSH
protection when the `qlik-csrf-token` is only sent as an HTTP header.
The MCP appends it as a `?qlik-csrf-token=<value>` query parameter to the
WS URL (that is what Qlik actually validates) and also sends it as a
header for backward compatibility. If you are running the MCP built-in
then this is already correct — a 403 here means either the VP is not
linked to the Central Proxy (see 400 entry above, which sometimes
surfaces as a 403 on WS), or the hostname in `QLIK_SERVER_URL` does not
match an entry in the VP Host allow list.

### `JWT mode requires a virtual proxy prefix in QLIK_SERVER_URL`

You set `QLIK_JWT_TOKEN` but `QLIK_SERVER_URL` has no path. Add the prefix:

```
QLIK_SERVER_URL=https://qlik.company.com/jwt
```

### Everything looks right but requests still fail

Turn on debug logging in the MCP:

```
LOG_LEVEL=DEBUG qlik-sense-mcp-server --stdio
```

The log shows the exact bootstrap URL, the session cookie name that was
picked up, whether `qlik-csrf-token` was received, and the HTTP status of
each subsequent call.

---

## Security notes

- `jwt_private.pem` is the only secret that grants admin-level impersonation
  power. Keep it on one machine, back it up offline, and rotate it if you
  suspect any compromise.
- Analysts only ever hold a signed JWT. A stolen JWT lets the attacker act
  as *that one analyst* until the token expires (default 365 days) or the
  user account is disabled in Qlik. It does not let them forge tokens for
  other users — they do not have the signing key.
- Use separate virtual proxies for separate trust domains (e.g. dev vs
  prod). Each VP has its own certificate, so compromising one does not
  affect the other.
- `--days 3650` exists for convenience but tilts the tradeoff toward
  availability, not rotation discipline. The conservative default is 365.
  Pick what matches your policy.
