# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

## [Unreleased]

## [2.3.1] - 2026-09-25

### Fixed

- JWT mode: an expired, not-yet-valid or malformed `QLIK_JWT_TOKEN` is now
  reported as exactly that, with the expiry date and the remedy (issue a new
  token), before any request reaches Qlik. The virtual proxy answers every
  unusable token with the same bare HTTP 400, so the error used to read
  "csrftoken returned HTTP 400: <html page>" and sent the model looking for a
  changed proxy, rotated keys or a deleted app.
- JWT mode: HTTP 400 for a token that is well-formed and not expired now
  says the proxy refused the token instead of quoting the HTML error page.

## [2.3.0] - 2026-08-30

### Changed

- Dependency floors moved up to the lines this release is developed and
  tested against: `httpx>=0.28`, `pydantic>=2.13`, `python-dotenv>=1.2`,
  `websocket-client>=1.9`, `PyJWT>=2.13`, `cryptography>=50.0`, and for
  development `build>=1.6`, `twine>=7.0`, `pytest>=9.0`,
  `pytest-asyncio>=1.4`. They are minor-level, so a newer patch is always
  welcome. The MCP SDK range is unchanged at `>=1.8.0,<3.0.0` - both SDK
  lines stay supported.
- Version bumping moved from bump2version, which has had no release since
  2020, to [bump-my-version](https://pypi.org/project/bump-my-version/).
  Its configuration lives in `[tool.bumpversion]` in `pyproject.toml`
  beside the version it bumps, and `.bumpversion.cfg` is gone. That file
  had also gone stale: it still said `current_version = 2.0.2`, so the
  next `make version-patch` would have produced 2.0.3 rather than 2.2.1.

### Removed

- Configuration that reached nothing: `QLIK_PROXY_PORT` and
  `QLIK_HTTP_PORT` were read into the config object, and no code path
  built a request from either - ticket authentication and the metadata
  endpoint they were meant for do not exist in this server. With them go
  the `proxy_port` / `http_port` fields and the `DEFAULT_PROXY_PORT`,
  `DEFAULT_TICKET_TIMEOUT`, `DEFAULT_FIELD_FETCH_SIZE`,
  `MAX_FIELD_FETCH_SIZE`, `MAX_TABLES` and `MAX_TABLES_AND_KEYS_DIM`
  constants, none of which had a reader.
- Seventeen unused helpers in `utils.py` (`format_bytes`,
  `format_number`, `format_duration_ms`, `truncate_text`, `safe_divide`,
  `validate_app_id` and the rest of that layer) and four exception classes
  nothing raised (`QlikAuthError`, `QlikRepositoryError`,
  `QlikAppNotFoundError`, `QlikConfigError`). What remains in `utils.py` is
  what the server calls: field-name writing, the XSRF key, and picking
  Qlik's session cookie out of a jar. Their tests were rewritten to cover
  those instead of the deleted formatters.
- The `qlik_sense_mcp_server/engine_api.py` back-compat shim. The client
  has lived in `engine/` since 2.0.0; the twenty places still importing
  through the old path now import `qlik_sense_mcp_server.engine`.
- The `git-clean` make target, which deleted `.git` and re-initialised the
  repository.

### Fixed

- Documentation consistency pass across the whole doc set. The tool count
  is 28 everywhere (`README.md` said 27 in one table, `usage.md` said 24);
  the authentication section of `usage.md` lists all three modes instead
  of the two it knew before form login was added; `configuration.md` no
  longer carries two separate "Logging" sections with different contents;
  `installation.md` states the real MCP SDK floor (`mcp>=1.8.0,<3.0.0`,
  matching `pyproject.toml`) and pins the current release in its `uvx`
  example; and the project layout in `architecture.md` lists the modules
  that were added since it was written (`tools/schema.py`,
  `engine/queries.py`, `engine/expressions.py`, `engine/filters.py`,
  `exceptions.py`).
- Every MCP client example that spawns the server now passes `--stdio`.
  Without it the process starts in Streamable HTTP mode and a client that
  spawned it over stdio never gets a reply, so the examples in
  `configuration.md`, `AUTH_JWT.md`, `AUTH_FORM.md` and `mcp.json.example`
  could not work as printed. `configuration.md` also shows the HTTP form
  of the same registration, for the long-lived server it describes.
- `QLIK_LOG_REPLIES` and `MCP_PORT` are documented; they existed in the
  code with no mention anywhere. `.env.example` no longer describes an
  HTTP timeout and a retry count that are not settings, and now covers
  JWT mode, form-mode overrides and `QLIK_TASK_TOOLS`.
- Three statements that would have misconfigured a working deployment.
  The virtual proxy prefix in `QLIK_SERVER_URL` was described as required
  in form mode as well as JWT; it is required only in JWT mode, and a
  form-based auth module can sit on the central proxy. A missing
  `QLIK_CA_CERT_PATH` was said to disable TLS verification; verification
  is governed by `QLIK_VERIFY_SSL` alone, and the CA path only adds a
  private CA to the trust store. Certificate paths, user directory and
  user id were listed as requirements for everyone; each is now tied to
  the mode that needs it.
- The `--help` tool list was missing `engine_query`, the main analysis
  tool, while the count printed beside it came from the live registry and
  said 28. The list names all eleven Engine tools now. `mcp.json.example`
  likewise pre-approves `engine_query`, `engine_get_field_range`,
  `search_app` and `get_about`, which it had left out.
- CI never ran on a pull request into `dev`: `test.yml` triggered on
  `main` only, so a branch merged the way this project merges was tested
  after the fact rather than before. Both `main` and `dev` trigger it now.
- The 1.x leg of the SDK matrix installed `mcp>=1.1.0,<2.0.0`, below the
  floor the package itself declares; it pins `>=1.8.0,<2.0.0` now.

## [2.2.0] - 2026-08-29

The first build carrying both lines of work: everything accumulated in
2.0.2 (released to PyPI but never merged into the main branch) and the
login/password authentication mode.

## [2.1.0] - 2026-08-29

### Added

- Login/password (form) authentication mode, selected automatically when
  `QLIK_PASSWORD` is set. Drives a virtual proxy's "Form based" login page
  (most commonly the in-box Windows credentials page) the way a browser
  would: GET the entry point and follow Qlik's own redirect chain to the
  login page — which carries a per-visit `targetId` that cannot be
  hardcoded or guessed — parse out the form, POST the credentials, and
  reuse the resulting session cookie exactly like JWT mode does after its
  own bootstrap. No client certificates and no JWT signing key needed; the
  trade-off is a plain password living in the MCP config instead. See
  `docs/AUTH_FORM.md`.
- `QLIK_TASK_TOOLS=true` now works outside certificate mode too. The
  fourteen reload-task tools need a QRS repository-admin role, which is a
  QMC property of the authenticated identity, not of the authentication
  method — so a JWT or form login that maps to a sufficiently privileged
  identity has exactly the same QRS access a certificate-mode service
  account would (verified live: a full reload-task listing against a
  form-mode session). The default is unchanged — on in certificate mode,
  off in JWT/form mode — this only adds the opt-in for an operator who has
  confirmed the identity behind their JWT/password does hold those rights.

### Fixed

- QRS did not always signal an expired or unrecognized session with a
  clean 401. Verified against a live form-mode deployment: a request with
  no session cookie at all gets a 302 redirect to the virtual proxy's
  login page, and one with a present-but-unrecognized cookie (plausible
  after a proxy node failover, not just outright deletion) gets a 500
  whose body is Qlik's own "Authentication error: Restart the browser."
  page. Both now trigger the same automatic re-login-and-retry as a 401
  instead of surfacing as an opaque `HTTP 302` / `HTTP 500` error.
- The Engine WebSocket upgrade has the same gap with no HTTP status to
  catch it by: a session whose local TTL says "fresh" but that Qlik no
  longer recognizes server-side lets the upgrade itself succeed, and
  Engine then closes the socket without ever sending a greeting. Now
  retried once with a refreshed session, symmetric to the existing 401/403
  handling on the upgrade response.
- Form login no longer fails on the redirect chain to the login page. Qlik's
  proxy closes the connection right after the 302, so following the redirect
  on the pooled keep-alive connection wrote into a dead socket and raised
  `RemoteProtocolError` before the login page was ever fetched; both hops of
  the credential exchange now ask for a fresh connection. Verified against
  Qlik Sense May 2026 Patch 2, where the login failed every time without it.
- A live form session is now reachable as `server.form_session`, like its JWT
  twin, and the test fixture hands it back when a run ends. Without that,
  consecutive login/password runs piled up Qlik sessions until the per-user
  limit was exhausted.
- Re-login after the session TTL expired failed on any deployment that kept the
  old session alive: Qlik serves the login form only to a client it does not
  recognise, so the refresh landed on the hub and reported "could not find a
  login form", while the request that triggered it went out on a session Qlik
  no longer honoured. Verified live: an app listing that returns 22 apps
  returned an error instead. The previous session cookie is now dropped before
  logging in again (a load balancer's own cookie is left alone).
- A session bootstrapped by the Engine path now reaches the Repository client
  too. Previously a run that touched Engine first left the Repository client
  with an empty jar, got a redirect on its first call and logged in a second
  time, spending another of the five sessions Qlik allows per user. Same fix in
  JWT mode.
- A session is considered fresh by its cookie and age, not by the CSRF token.
  Qlik releases before November 2024 never send one, so such a deployment never
  had a fresh session and logged in again on every single request.
- Wrong credentials behind a load balancer now say so. The "a single cookie can
  only be the session" rule was being applied to the whole client jar, where the
  balancer's own cookie can be the only one present, so a failed login looked
  successful and surfaced as an unrelated error on the next request.
- The login POST is retried once on a dropped connection, like the entry-point
  GET. Both hops run through the same redirect chain and are exposed to the same
  transport failures; the retry now lives in one place for both.
- The first QRS call after a bootstrap survives a dropped connection: Qlik
  closes the connection at the end of the login chain, and a read is repeated
  once on a fresh one. Writes still report the failure, since a write that died
  while answering may already have happened.
- The script log and tempContent downloads go through the authorized request
  path. They called the client directly with `follow_redirects=True`, so a
  lapsed session turned the login page into a 200 and the HTML was stored as the
  script log - reachable now that task tools can be enabled outside certificate
  mode.
- Engine tells a missing license apart from a stale session. `OnLicenseAccessDenied`
  was treated as an expired cookie, so every such connect spent a full re-login
  on a certain failure and brought the per-user session limit closer.
## [2.0.2] - 2026-08-14

### Added

- The app's own library of measures and dimensions comes back with
  `get_app_details` under `library`, and a query can name a measure from
  it instead of assembling the aggregation itself:
  `{"measures": [{"master": "Sales MUSD"}]}`. This is the vocabulary the
  author of the app wrote down - what revenue means here, and how it is
  computed. Assembled by hand, the same question can pick a neighbouring
  field or a different aggregation and answer with a number nobody on
  that dashboard would recognise.
- The bookmarks and alternate states an app holds come back under
  `named_sets`, so a caller asking for one by name has somewhere to read
  the right spelling.
- The shape of a typed query is declared in the tool schema: which keys a
  filter, a metric, a measure, a scope and a query have, which are
  required, and what values they take. A key that does not exist is
  refused before Qlik is asked.
- Data of one object comes back in pages (`limit`, `offset`), while its
  definition and expressions come whole. Measured on a live table: the
  reply used to run to 111 thousand characters, nearly all of them rows.

### Changed

- A refusal states what is wrong and stops there. It no longer advises
  what to do next, and no longer lists what would have been valid:
  choosing the next step belongs to the caller, and the lists it may need
  it can ask for with a call of its own.
- The reply of a query carries its warnings first, before the numbers,
  together with a plain flag when a number cannot be taken as verified.
  A batch where every query failed answers as a failure rather than as a
  list with failures inside it.
- A period that could not be checked is said out loud instead of being
  passed over in silence beside a number.

### Fixed

- `get_app_field` no longer claims to return the most frequent values
  first: the order is the field's own, ascending, and the first ten
  values of a large field are the first ten alphabetically.
- An app whose name holds an apostrophe is found by name again. Measured:
  the repository refuses a quote inside a filter in every form, so the
  quote no longer goes into the filter at all.
- A grouping written as an expression stays an expression instead of
  being read as a field name, and a grouping can carry a label to be
  sorted by.
- The page of an object is read from Engine rather than cut out of the
  first one it happened to send.
- The default state `$` is accepted; it exists in every app and is never
  listed among the named ones.

## [2.0.2-dev] - 2026-08-13

### Added

- A metric can aggregate over groups rather than over rows: `per` names
  what each inner value is computed for, `inner_agg` how it is computed.
  `{"field": "tis_days", "inner_agg": "sum", "per": "IssueId",
  "agg": "fractile", "p": 0.85}` becomes
  `Fractile(Aggr(Sum([tis_days]), [IssueId]), 0.85)` — days summed per
  issue, then the 85th percentile across issues. This is a different
  question from the same aggregation over rows, and the answers differ:
  measured on four rows, the median over rows was 6 and the median over
  issues 10. Without this the second question could only be asked by
  writing the expression by hand, and asking the first instead returned a
  number that looked entirely reasonable.
- `fractile` as an ordinary aggregation, with `p` for the fraction. A
  fraction outside 0..1 is refused: measured, Qlik answers such a call
  with a dash rather than an error, and a dash reads as a value.
- A metric or a hand-written measure can carry its own `filters`,
  overriding the query's, so a KPI holds its numerator and denominator in
  one answer. No key means the query's filters; `[]` means none at all.
  Identical filter sets are built once, not once per measure, and the
  reply says in `measure_filters` which measure used which slice.
- `engine_query` accepts `measures` in a single query, not only inside
  `queries`.
- A filter says which of the four things it does with the values it
  names: keep them (`values`), drop them (`exclude`), add them to what is
  selected (`add`) or keep what is in both (`intersect`). Only one per
  filter — several conditions on one field are several filters, and
  stating two at once is refused rather than answered by the first.
- A filter can name values of its field by a condition on another field:
  `matching` for the clients who bought in 2023, `not_matching` for those
  who did not, both together for "bought then and not since". `of_field`
  reads the values from a different field than the one being narrowed.
  Measured on a model of a hundred: 60 bought in 2023, 40 did not, 30
  bought in 2023 and not in 2024.
- A filter can search text (`contains`, `starts_with`, `ends_with`,
  case-insensitive) or keep the values an expression holds for
  (`match_expression`).
- `scope` says what a query counts over before any filter narrows it: the
  whole model whatever is selected, a bookmark, an alternate state, or
  the selections as they were a few steps back (`selection_back`) or
  forward again (`selection_forward`). Stated on the query it
  reaches every measure; stated on one metric, or on one part of an
  arithmetic metric, only that one.
- `total` and `total_except` count across the grouping instead of within
  it, so a share of the whole is one metric beside the value it is a
  share of. Measured on 40 and 60 against a total of 100: the shares came
  back 0.4 and 0.6, and with `total_except` by region each client's row
  carried its own region's total.
- Sets combine with each other, not only with values of a field:
  `{"combine": "union", "of": [{...}, {...}]}` answers "bought in 2023 or
  lives in the South", and `intersect`, `exclude` and
  `symmetric_difference` answer the other three questions of the same
  shape. Each set carries filters of its own. Measured on two sets holding
  40 and 60: 100, 0, 40 and 100 respectively. A filter written outside the
  combination is refused, because Qlik reads no modifier around one.
- `op` and `of` state arithmetic over aggregations — a ratio, a
  difference, a product — with each part free to carry its own filters
  and its own scope. Division answers with no value rather than an error
  when the denominator is zero. The reply names the slice each part used.

### Fixed

- A field name with a space made the check refuse a query Qlik runs
  happily: `CheckExpression` reads a bare `Тип ставки` as two tokens and
  answers "Garbage after expression: 'ставки'". Every model with
  non-English field names was locked out of `engine_query`. Names are now
  written in brackets — once, where the query is planned, because the
  text of an expression is the key by which Engine's verdict finds its
  way back to the query. A name that arrives already bracketed is left as
  it is.
- A filter on a field the app does not have blamed the bounds: a missing
  field has no tags, so it read as "not a date" and perfectly good dates
  went into a numeric parse that complained about them. The field is
  checked before its bounds are read. A failed question is told apart
  from a missing field, so a dropped frame is not reported as "no such
  field".
- The warning about set analysis fired on almost every measure. It was
  built on `GetFieldsFromExpression` returning the fields a modifier
  filters on; measured, it returns every field of the expression, so
  `Sum([Amount])` came back as "set analysis filters on 'Amount'". The
  warning and the call behind it are gone.
- A limit above the ceiling, and a cube too wide for one page, are
  refused with the value that fits instead of being quietly reduced. An
  offset that is not a row number is refused rather than clamped. Bounds
  given in the wrong order are refused instead of silently swapped.
  Stating both an inclusive and an exclusive bound for the same end is
  refused instead of one being ignored. An empty value inside a filter
  list is named by its position rather than dropped.
- A page of field values that filled up looked exactly like a field that
  ran out. The reply now says whether there is more.
- An unrecognised `published` value answered like the correct one:
  anything unknown meant "no filter", which is what `"both"` means. It is
  refused with the values it takes.

### Changed

- The group with no dimension value is kept by default in both tools -
  the hypercube used to drop it unasked, which contradicted the same
  sentence written for the typed query. Dropping it is a
  statement about the data — facts with no value for the grouping field
  are still facts — so it is the caller's to make: `exclude_null_dimensions`
  is available in `engine_query` as well, and defaults to keeping the
  group in both tools. A ranking may now start with Qlik's `"-"` row
  where it did not before.
- Suggestions of near-miss field names and values are gone. The model
  reads the field list and the values itself; a shortlist picked by
  string similarity is a guess about spelling, not a fact about the app.
  The value search behind the old suggestion also cost about 2.5 seconds
  per refusal.
- Messages name a field in brackets — `[Дата ставки]` — so the boundary
  between the name and the sentence around it is visible.

## [2.0.1] - 2026-08-13


### Fixed

- A field name with a space made the check refuse a query Qlik runs
  happily. `CheckExpression` reads a bare `Тип ставки` as two tokens and
  answers "Garbage after expression: 'ставки'", so every model with
  non-English field names was locked out of `engine_query` entirely.
  Names are now written in brackets — once, where the query is planned,
  because the text of an expression is the key by which Engine's verdict
  finds its way back to the query. A name that arrives already bracketed
  is left as it is.
- A filter on a field the app does not have blamed the bounds: a missing
  field has no tags, so it read as "not a date" and perfectly good dates
  went into a numeric parse that complained about them. The field is now
  checked before its bounds are read, and the refusal names the field.
- The warning about set analysis fired on almost every measure. It was
  built on `GetFieldsFromExpression` returning the fields a modifier
  filters on; measured, it returns every field of the expression, so
  `Sum([Amount])` came back as "set analysis filters on 'Amount'". The
  warning and the call behind it are gone — nothing distinguishes a
  filter field from an aggregated one, and a claim that cannot be checked
  is worse than none.
- A limit above the ceiling, and a cube too wide for one page, are
  refused with the value that fits instead of being quietly reduced. A
  reduced limit returns fewer rows than were asked for, and nothing in
  the reply told the two apart. The hand-written path already refused;
  the typed one now matches it.
- An offset that is not a row number is refused rather than clamped to
  zero.
- Bounds given in the wrong order are refused instead of silently
  swapped, and stating both an inclusive and an exclusive bound for the
  same end is refused instead of one of them being ignored.
- An empty value inside a filter list is named by its position rather
  than dropped.

### Changed

- The group with no dimension value is kept by default. Dropping it is a
  statement about the data — facts with no value for the grouping field
  are still facts — so it is now the caller's to make:
  `exclude_null_dimensions` is available in `engine_query` as well, and
  defaults to keeping the group in both tools. A ranking may now start
  with Qlik's `"-"` row where it did not before.
- Suggestions of near-miss field names and values are gone. The model
  reads the field list and the values itself; a shortlist picked by
  string similarity is a guess about spelling, not a fact about the app.
  The value search behind the old suggestion also cost about 2.5 seconds
  per refusal.
- Messages name a field in brackets — `[Дата ставки]` — so the boundary
  between the name and the sentence around it is visible.

## [2.0.0] - 2026-08-12

### Added

- `engine_query`: a query stated rather than written. `group_by` names the
  grouping fields, `metrics` the aggregations
  (`{"field": "Amount", "agg": "sum"}`), `filters` the period or the
  values to narrow to; the server writes the Qlik expressions. `queries`
  takes a list of independent questions — different groupings, different
  measures, different filters — and runs the whole list over three
  round-trips rather than three per question, however many there are. A
  question that fails takes only itself down.
- A period filter reports what it selected. Every query filtered on a date
  answers with `period_check`: the earliest and latest value of that field
  inside the result, and whether the filter applied. Qlik reports neither —
  it drops a condition it cannot honour and answers with the unfiltered
  total, a number larger than the truth with nothing to mark it as wrong.
- Filters described rather than written work in `engine_create_hypercube`
  too, applied wherever a measure carries the `{filter}` marker.
- `QLIK_TASK_TOOLS=false` leaves the 14 reload-task tools out of
  certificate mode, for an identity that only reads data.

- Filter bounds follow the field. On a date field `from` and `to` are days;
  on any other field they are the values themselves, so
  `{"field": "Discount", "from": 400}` is a discount of 400 or more.
  Measured on a live app: read as a date, 400 became a day in 1901 and the
  answer came back 18,774 where the truth was 1,898,591 — a plausible
  number, and wrong.
- `greater_than` and `less_than` exclude the bound they name. "More than
  400" and "from 400" are different questions, and on a 10M-row app the
  difference is the 184 orders discounted by exactly 400.
- A numeric range reports its control values the way a period does: the
  smallest and largest value inside the result, and whether they fall
  within the bounds asked for.

### Changed

- Every check on a query is now made by Engine, in one batch that costs
  about 4ms against 75ms for the smallest hypercube: `ExpandExpression`
  resolves `$(...)` so the checks see what will run, `CheckExpression`
  reports syntax and names the data model does not have,
  `GetFieldsFromExpression` reports the fields a set modifier actually
  filters on. The lexical scan that guessed at all three — set-modifier
  field names, SQL keywords, quoted comparisons, variable expansion — is
  removed. An unknown name in a measure is now refused rather than
  warned about: it is Engine's verdict, and Qlik scores such a name as 0,
  so the measure would come back as a column of zeros that reads as an
  answer.
- The form of a date filter is measured rather than assumed. Comparison
  inside a set modifier runs against the text Qlik displays for a value,
  so a serial-number range returns 0 on a field displayed as `01.01.2024`
  and works on one displayed as `45292` — with no error either way.
  Measured against a field carrying a time of day, the numeric form
  selected nothing and the expression form was correct; on a field of bare
  numbers both were correct and the numeric one sixty times cheaper. The
  server now checks the cheap form against a reference count and uses it
  only where it agrees, remembering the answer per field.
- A date in a query result reads as the text Qlik displays for it, the
  same writing as the sample values in `get_app_details`, the values from
  `get_app_field` and the bounds from `engine_get_field_range`. It used to
  come back as the serial number, so one value had two writings and a
  filter written from the wrong one selected nothing.
- Tool descriptions rewritten to one shape: what the tool does, when to
  use it, when not to, what it returns and what it does not. The block
  about Qlik's session limit repeated in eight of them is gone — it
  described something the caller does not control.
- `get_app_details` switches to a `columns` + `rows` table once a model has
  more than 60 fields, and skips reading sample values there. Narrow
  models — the normal case — keep the readable per-field form. Measured
  at 61 fields: 6.3k characters as objects against 3.8k as a table, and
  16k instead of ~32k at 300 fields.
- `get_app_details` now says in its own description that `get_about` is
  never a prerequisite and `get_apps` is only needed without a name.
- Field metadata is tighter without losing anything a caller needs. Tags
  come back as one string without Qlik's `$` (`"numeric integer"` for
  `["$numeric", "$integer"]`); `rows` is no longer repeated on every field
  when it is the table's row count, reported with the table; `is_key` and
  `tags` appear only when they say something. The load-script comment is
  returned as before — it is the only human description a column has.
  Measured: 7.6k characters to 6.5k on a 33-field model.

### Fixed

- A batch of queries no longer fails as a whole. Engine is asked about
  every expression in the batch once, and each query is then judged on its
  own: two mistakes of different kinds both land, and a query naming
  `NopeAmount` is not refused because another named `Nope`.
- Two filters that each select something but select nothing together took
  the whole call down. Engine answers Min/Max over an empty set with a
  "NaN" sentinel, which reached a numeric comparison as text; it is now
  read as "no value" everywhere.
- A field name written into an expression stays one name. `Amount]) +
  Sum([Amount` turned one aggregation into two, the second without the
  filter, while the reply reported the period as applied.
- One call is bounded: 25 queries, and 200 grouping fields, measures and
  filter values between them. Each of those costs an Engine call before
  the batch runs.
- Session objects are released on every path out, including a transport
  failure part way through a batch.
- A repository request is retried after a timeout only when it reads.
  Retrying a POST could create a second task.
- The first call after a quiet period no longer fails. A Qlik that has
  been idle answers slowly — measured through the virtual proxy, 15 to 21
  seconds to bootstrap a session and 3 to 15 for the first repository
  call, against hundredths of a second once warm — and both ran under a
  ten-second deadline. The bootstrap now has its own minute, and a
  timed-out repository call is retried once with room to breathe.
- The schema cache never noticed a reload: both callers passed `None`
  where the app's reload timestamp belonged, so the invalidation the
  cache was built around had never once fired and only the ten-minute
  expiry limited it. It now receives the real timestamp.

- Engine sessions are released about five seconds after the socket
  closes instead of lingering for the proxy's inactivity timeout. The
  WebSocket URL now carries a `ttl` segment, so restarting the server
  no longer walks into Qlik's limit of five sessions per user.
- Ten environment variables removed, leaving 15 `QLIK_*` settings:
  `QLIK_HTTP_TIMEOUT`, `QLIK_WS_RETRIES`, `QLIK_WS_IDLE_PROBE_AFTER`,
  `QLIK_WS_PROBE_TIMEOUT`, `QLIK_WS_GREETING_TIMEOUT`,
  `QLIK_LOG_REPLY_CHARS`, `QLIK_JWT_SESSION_TTL`,
  `QLIK_JWT_SESSION_COOKIE`, `QLIK_JWT_USER_ID_CLAIM`,
  `QLIK_JWT_USER_DIR_CLAIM`. The first six are now fixed values; the last
  two were never read by the server.
- The virtual proxy session cookie is recognised even when QMC renames it:
  the conventional `X-Qlik-Session*` first, then any name containing
  "qlik", then a lone cookie. Previously a renamed cookie arriving
  alongside a load-balancer cookie left no way to connect, since the
  override that covered it was removed. The error now lists the cookies
  that did arrive.

## [1.9.0] - 2026-08-11

### Added

- A hypercube dimension naming a field the model does not have is now
  refused with `error_category: field_not_found` and `did_you_mean`
  suggestions. Qlik scores an unknown name as 0, so the query used to
  return one row holding the grand total.
- Every hypercube reply carries `warnings`: an all-zero or all-`'-'`
  measure, SQL written into a measure (`AS`, `SELECT`, `GROUP BY`,
  `FROM`, `WHERE`), names a measure mentions that the model does not
  have, and an empty result.
- `get_app_sheet_objects` and `get_app_object` return `fields_used`,
  `measures` and `dimensions`, with master items resolved to their
  library definitions.

### Fixed

- Measure expressions were read from the object layout, where Engine
  does not put them; they come from the properties.
- Filter panes reported no fields — the fields are in their listbox
  children.
- Field names outside `A-Za-z` were dropped, `1e3` was reported as a
  field, and a double-quoted set-analysis search returned its words as
  field names.
- `=Year(no_such_field)` skipped the unknown-field check.
- `suppress_zero=True` hid the all-zero-measure warning.
- The SQL check fired on a field legitimately called
  `[Cost as planned]`.
- Certificate mode ignored the URL scheme and could not reach `ws://`
  with the default retry budget.

## [1.8.1] - 2026-08-11

### Changed

- The scheme in `QLIK_SERVER_URL` now decides the transport for the
  Engine WebSocket too: `http://host/jwt` connects with `ws://`,
  `https://` with `wss://`. An `http://` URL is logged as a warning —
  the JWT and the session cookie then travel in clear text.
- `QLIK_VERIFY_SSL` defaults to `false`. Qlik Sense Enterprise serves a
  self-signed certificate, so verification failed on a correct
  installation. Set it to `true` to turn verification on.

### Fixed

- Filter panes reported no fields: a list object carries one
  `qDimensionInfo`, not a list, and iterating it walked the dict's keys.
- Field extraction matched only bracketed names, so `Sum(Sales)`
  reported nothing.

## [1.8.0] - 2026-08-11

### Added

- `update_task_schedule` and `delete_task_schedule` — a task can have
  several triggers, and stopping one used to mean disabling the task.
- `QLIK_WS_IDLE_PROBE_AFTER` (30s), `QLIK_WS_PROBE_TIMEOUT` (15s) and
  `QLIK_WS_GREETING_TIMEOUT` (15s), separate from `QLIK_WS_TIMEOUT`.
- End-to-end test suite (`tests/test_e2e_*.py`, marker `e2e`) that runs
  the tools against a real Qlik. Skipped unless `QLIK_E2E_*` is set.
- `QlikEngineAPI.send_requests_pipelined()`; sheet objects now cost 2
  round-trips instead of 2 per object. (from PR #29)

### Changed

- `get_app_variables` always returns objects, and both sources by
  default — script variables used to be dropped unless asked for.
- `qSuppressMissing` is never set: it drops the NULL-dimension row,
  which `qNullSuppression` already does under the caller's control.
- `engine_api.py` and `server.py` are split into `engine/` and `tools/`
  packages. The old import paths keep working.

### Removed

- 55 methods nothing called (~1180 lines), including
  `get_pivot_table_data`, which raised `NameError` on every call.

### Fixed

- QRS paging happens on the server: `app/full` ignores skip/take and is
  capped at MaxRecordLimit, so apps past the cap were invisible and
  `total_found` reported the cap as the total. Same for reload tasks and
  execution history.
- `published="both"` was unreachable and answered with published apps
  only.
- `create_task_schedule` built schedules QRS rejected outright: an empty
  `operational` section, a wrong `incrementOption` numbering, the
  interval in the wrong position, and an enum where a window string
  belongs.
- Failed-task filters missed Aborted (6) and Error (11); `get_tasks`
  gained the documented `"running"` filter and refuses an unknown one.
- Hypercube pages are read to the requested height instead of returning
  fewer rows silently.
- Engine calls are serialised: overlapping calls on the shared socket
  discarded each other's replies and could switch documents mid-call.
- Session objects are destroyed on every path, including exceptions.
- Field search and paging run in Engine — on a 200k-value field, a local
  scan could not see a match at all.
- `get_app_field_statistics` uses `NullCount()`: `null_percentage` was
  near zero however much was missing.
- A Repository failure returns `error_category: repository_error`
  instead of an empty list, and a missing field is reported instead of
  being invented as `0`.
- The cached WebSocket is no longer wedged by its own health check —
  Qlik's proxy does not relay ping/pong to Engine, so through a virtual
  proxy the next request hung for the whole timeout (63s to 0.005s).
- A refused Engine session (`OnMaxParallelSessionsExceeded`) raises
  `QlikSessionLimitError` instead of surfacing as a parse error later.
- `exclude_null_dimensions=false` keeps the NULL group again.
- Session-object ids are unique per call, so a stale cached calculation
  cannot come back. (from PR #27)
- Script-log retrieval, `grand_total` nulls, truncation reporting,
  case-sensitive wildcard search, `start_date` defaulting to the past,
  and per-client transaction locks.

## [1.7.2] - 2026-07-31

### Added
- **Field and table comments are now returned.** Qlik keeps the business
  description of a column in `qComment`, set by `COMMENT FIELD x WITH
  '...'` / `COMMENT TABLE t WITH '...'` in the load script, and hands it
  out in `GetTablesAndKeys` for every field and table. The server used to
  drop it — `get_app_details` reported a hard-coded empty comment — so an
  LLM had to infer a column's meaning from its name alone. Now
  `get_app_details` puts a `comment` key on every table and field that
  carries one, and `get_app_field` returns `field_comment` for the field
  it lists. The key is omitted when the script sets no comment, so apps
  without comments pay nothing in context size.
- `QlikEngineAPI.get_field_description()` — thin wrapper over the Engine
  `GetFieldDescription` method: name, comment, source tables, cardinality,
  byte size for a single field, with no data page and no hypercube.
  Returns `{}` for a field the model does not know.
- `tests/test_field_comments.py` — covers comment propagation through
  `get_fields`, `get_field_description` and the `get_app_details` payload,
  including the "no comment set" case that must not emit the key.

## [1.7.1] - 2026-07-29

### Fixed
- **`ensure_app()` could cache a handle as "has data" when Engine actually
  opened it without data.** The cached-connection reuse logic trusted the
  requested `no_data` flag rather than checking what Engine actually did.
  If a WebSocket session got shared/attached to an existing no-data
  session for the same user+app, `GetAppLayout`/`GetAppProperties` still
  succeeded, but `GetTablesAndKeys` silently returned `qtr: []` — an app
  looked fully readable while its data model came back empty, with no
  error surfaced. `ensure_app()` now reads Engine's own
  `qIsOpenedWithoutData` from `GetAppLayout` right after `OpenDoc`,
  retries once on a fresh connection if data was requested but not
  granted, and raises `QlikEngineError` instead of returning a handle
  that would later produce a silently empty data model.
- Corrected misleading inline comments on `GetTablesAndKeys` positional
  arguments (`qCellHeight` / `qSyntheticMode` / `qIncludeSysVars`) in
  three call sites — the values were already correct, only the comments
  describing them were wrong.

## [1.7.0] - 2026-07-29

### Added
- **Support for MCP SDK 2.x.** SDK 2.0 removed `mcp.server.fastmcp` and
  replaced the FastMCP host with `mcp.server.mcpserver.MCPServer`.
  `server.py` now selects the host class at import time and exposes the
  result as `MCP_SDK_MAJOR`, so the same code runs on both SDK lines.
  The only behavioural difference between them is that 2.x takes the
  bind address in `run_streamable_http_async()` rather than in the
  constructor; everything else — the `@tool()` decorator, `stdio`, the
  tool registry used by `--help` — is identical.
- `tests/test_sdk_compat.py` — asserts that the selected host matches the
  installed SDK, that every API the server calls exists on it, and that
  the published `engine_create_hypercube` schema still carries the
  ranking parameters and its docstring. A future SDK change now fails
  here instead of in a user's terminal.

### Changed
- **Dependency is now `mcp>=1.8.0,<3.0.0`.** The `<2.0.0` pin from 1.6.1
  is no longer needed. The floor was raised from the long-standing (and
  incorrect) `1.1.0`: `FastMCP.run_streamable_http_async()` — the default
  transport — first appears in 1.8.0, so 1.2–1.7 would install happily
  and then die with `AttributeError` on startup, and 1.1 has no
  `FastMCP` at all. The upper bound keeps the next breaking SDK major
  from breaking installs again.

### Fixed
*(found by an automated review of this release)*
- **Hypercube session objects leaked on every failure after creation.**
  `DestroySessionObject` ran only on the success path, so a malformed
  layout or an Engine error left the result set pinned in Engine memory
  for the rest of the (deliberately long-lived) session. Cleanup moved
  into a `finally`; it is skipped only when the socket has already been
  force-closed, where there is nothing left to talk to.
- **`limit=0` or a negative limit silently returned one row.** The page
  height was clamped with `max(1, ...)`, so a nonsensical limit produced
  data instead of an error. Non-positive and non-integer limits now
  return a structured `invalid_limit` error before any Engine call.
- **A dimension sort expression given in Qlik's native `{"qv": "..."}`
  form was double-wrapped** into `{"qv": {"qv": "..."}}` and silently
  ignored by the Engine. Both that form and a plain string are now
  accepted.
- Corrected the comment and docs around the automatic `qSuppressMissing`
  applied when ranking by a measure: it is a cube-wide flag that drops
  rows where *any* measure is missing, not only the ranked one. The
  Engine offers no per-measure equivalent.

### Verified
- Both SDK lines were exercised end to end: full test suite on mcp
  1.29.0 and on mcp 2.0.0 (158 tests each), the streamable-HTTP
  transport answering a real `initialize` + `tools/list` handshake on
  both (24 tools published with identical schemas), and a live
  `tools/call` against a 91M-row Qlik app on 2.0.0 returning a correct
  top-5 ranking.

## [1.6.1] - 2026-07-28

### Fixed
- **Pinned `mcp<2.0.0` — the server would not start otherwise.** MCP SDK
  2.0.0 was released on 2026-07-28 and removes `mcp.server.fastmcp`, the
  FastMCP host this server is built on, in favour of a new
  `mcp.server.mcpserver` API. Because the dependency was declared as
  `mcp>=1.1.0`, every fresh install — including 1.6.0 and every earlier
  release — resolved to 2.0.0 and died at import with
  `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. The pin
  restores installability; porting to the 2.x API is a separate piece of
  work.
- **The NULL dimension row no longer hijacks a ranking.** Facts that
  carry no value for the grouping field all collapse into a single row
  that Qlik renders as `"-"`, so that row often holds a very large total
  and takes first place in a top-N, pushing out every real value.
  `engine_create_hypercube` now sets `qNullSuppression` on every
  dimension. Observed on a live app: the `"-"` row held the entire
  measure total and occupied rank 1 of the result.

### Added
- **`exclude_null_dimensions`** on `engine_create_hypercube`, default
  `True`. Pass `False` to keep the `"-"` row — useful precisely when you
  want to measure how much data is unattributed, since a large `"-"`
  total means the grouping field is not linked to those facts in the
  data model.

## [1.6.0] - 2026-07-28

### Added
- **Real top-N support in `engine_create_hypercube`.** New `sort_by` and
  `sort_order` parameters order the result by ANY column — a measure
  label, a measure expression, or a dimension field name (matching
  ignores case and square brackets). `sort_by` is translated into
  `qInterColumnSortOrder` with that column first, plus the matching
  `qSortBy` / `qSortCriterias` direction. Combined with `limit`, this
  finally answers "the 10 clients with the highest GGR" in one call.
- **`limit` parameter**, a clearer name for `max_rows`. `max_rows` still
  works as an alias, so existing callers and saved prompts are
  unaffected.
- **`suppress_zero`** to drop rows whose measure is 0 — mainly useful
  with `sort_order="asc"`, where zero-valued groups would otherwise fill
  the whole result.
- **Request echo on every failure.** Any error reply — raised exception
  or an `{"error": ...}` payload, including timeouts — now carries
  `tool` and `request` with the exact arguments that produced it. A
  timeout finally says WHICH query timed out instead of just "timed out
  after 180s". Implemented once in the `_timed` decorator, so it applies
  to all tools.
- **`timings` in the hypercube response**, split into
  `open_app_seconds` (loading the app into Engine memory, paid only by
  the first call against an app) and `get_layout_seconds` (the actual
  computation) — so a slow call can be attributed instead of guessed at.
- **Usage examples in every tool docstring**, with realistic arguments
  and a shortened but structurally correct response.
- **Qlik session-limit warning in the Engine tool docstrings and in
  `docs/AUTH_JWT.md`**: Qlik permits at most 5 concurrent sessions per
  user identity and can lock the account beyond that, so tool calls must
  never be fanned out in parallel. This server deliberately funnels
  everything through one cached Engine session.
- `tests/test_hypercube.py` and `tests/test_tool_registration.py` — 52
  new tests covering sort resolution, the generated `qHyperCubeDef`,
  guard rails, the request echo and per-mode tool visibility.

### Changed
- **Reload-task tools are registered in certificate mode only.** They
  call QRS endpoints that require repository-admin rights, which a JWT
  analyst does not have, so JWT mode now advertises 12 tools instead of
  24 rather than offering 12 that can only return 403.
- **The hypercube response is compact by default.** It now returns
  `columns` plus `rows` (plain values — numbers stay numbers) together
  with `grand_total`, instead of the raw `qHyperCube` with its per-cell
  `qElemNumber`/`qState` metadata. Pass `include_raw_layout=true` to get
  the full Qlik layout back.
- **`engine_create_hypercube`'s docstring was rewritten** around a
  SQL analogy and three worked examples, and no longer recommends
  `qSortByExpression` for ranking. Measured on a 91M-row table: sorting
  by the measure column runs in 0.2s where `qSortByExpression` took
  286s, because the latter makes the Engine compute the same aggregate a
  second time purely to order rows.
- **The `socket_timeout` hint now lists fixes in order of impact** and
  names the correct environment variable (`QLIK_WS_TIMEOUT`; it
  previously pointed at `QLIK_WS_OPERATION_TIMEOUT`, which does not
  exist).
- Truncation warnings distinguish a ranked query (expected: "showing the
  10 highest rows of 1217") from an unsorted one (a real problem: the
  returned rows are arbitrary).

### Fixed
- **Sorting by a measure never worked.** `qInterColumnSortOrder` was
  hard-coded to `list(range(n_cols))`, so the first dimension always won
  and the per-measure `qSortBy` was dead configuration. Any "top 10 by
  revenue" request silently returned 10 alphabetically-first rows —
  plausible-looking data in the wrong order. Verified against a live
  91M-row app: the new ordering matches a reference sort computed
  independently over all 1217 groups.
- **`create_hypercube` mutated its caller's dictionaries**, injecting
  default `sort_by` keys into the passed-in `dimensions` / `measures`
  and then returning them as the "echoed input".
- **Hypercube session objects were never released.** Each call left its
  session object alive, pinning the result set in Engine memory for the
  rest of the session; they are now destroyed after the data is read.
- `DEFAULT_HYPERCUBE_MAX_ROWS` was imported in `engine_api.py` but
  unused, while the default was hard-coded to 1000 in the signature.
- Docstring corrections: `get_apps` documented QRS field names
  (`modifiedDate`, `lastReloadTime`, `published`, `fileSize`) that the
  tool does not return — it returns `modified_dttm` / `reload_dttm`;
  `get_task_executions` documented snake_case keys where QRS returns
  camelCase with `duration` in milliseconds.

## [1.5.1] - 2026-04-27

### Changed
- **Documentation overhaul.** All `.md` files reviewed for accuracy
  against the v1.5.0 source: README highlights renamed to v1.5.0
  with a JWT auth bullet, JWT row added to the documentation index,
  `docs/configuration.md` gained a complete JWT environment-variable
  reference (`QLIK_JWT_TOKEN`, `QLIK_JWT_USER_ID_CLAIM`,
  `QLIK_JWT_USER_DIR_CLAIM`, `QLIK_JWT_SESSION_COOKIE`,
  `QLIK_JWT_SESSION_TTL`), `docs/installation.md` gained a cert/JWT
  branching note, `docs/architecture.md` documents the new
  `JwtSession` component and `tools/qlik_jwt_admin.py` admin CLI,
  `docs/troubleshooting.md` gained a JWT-authentication problems
  section that cross-links into `docs/AUTH_JWT.md`, and
  `docs/AUTH_JWT.md` gained a `Related` index. `COMMANDS.md` was
  reduced to a true one-page cheatsheet pointing at the deeper
  docs in `docs/`.

### Fixed
- **Release hygiene.** `qlik_sense_mcp_server/__init__.py` and
  `.bumpversion.cfg` were not bumped during the 1.5.0 release and
  still reported `1.4.1`. Both are brought back in sync with
  `pyproject.toml` as part of this release.

## [1.5.0] - 2026-04-24

### Added
- **JWT authentication mode** via a Qlik Sense JWT virtual proxy. The
  admin signs a long-lived token per analyst on a private machine; the
  analyst puts `QLIK_SERVER_URL` and `QLIK_JWT_TOKEN` into their
  `mcp.json` and nothing else. No client certificates, no private keys,
  no service account on the analyst side — identity travels in the JWT
  payload and Qlik applies that user's normal security rules, stream
  membership and Section Access. Mode switches automatically when
  `QLIK_JWT_TOKEN` is set in the environment.
- **`qlik_sense_mcp_server/jwt_session.py`** — lazy, thread-safe holder
  of the bootstrapped Qlik session material (session cookie plus
  `qlik-csrf-token`), with a conservative 25-minute TTL (override via
  `QLIK_JWT_SESSION_TTL`) and transparent re-fetching on 401/403.
- **`tools/qlik_jwt_admin.py`** — admin CLI with two commands:
  `init-keys` generates an RSA 2048 keypair plus self-signed X.509
  certificate for pasting into the QMC JWT virtual proxy;
  `issue-token` signs an RS256 JWT for a single analyst. Token
  lifetime defaults to 90 days; bearer JWTs have no individual
  revocation path, so the default deliberately prefers rotation
  discipline over long-lived convenience.
- **`docs/AUTH_JWT.md`** — complete admin + analyst guide covering key
  generation, QMC virtual proxy configuration (with a multi-node
  warning about linking to the Central Proxy), token issuance,
  revocation strategy, operational troubleshooting and the exact
  two-phase bootstrap the MCP performs under the hood.

### Fixed
- **Engine WebSocket works on Qlik November 2024+.** Under CSWSH
  protection the anti-CSRF token must be present as a URL query
  parameter (`?qlik-csrf-token=<value>`) on the WS upgrade, not just
  as an HTTP header. Without this the upgrade is rejected with 403.
  The Engine client now appends the CSRF token to the URL after the
  JWT session bootstrap and additionally sends it as a header for
  forward/backward compatibility.
- **Engine WebSocket self-heals on stale JWT session.** A 401/403 on
  the WS handshake triggers one re-bootstrap of the JwtSession and a
  retry of the same endpoint, symmetric to the existing QRS 401
  retry path.
- **URL parsing preserves non-standard ports.** `engine_api.connect()`
  now builds WSS URLs and the `Origin` header from the full `netloc`
  of `QLIK_SERVER_URL` instead of the bare hostname, so deployments on
  ports like 8443 work without regression. The `Origin` scheme is also
  derived from the configured URL rather than hardcoded to `https`.

### Changed
- **`QlikSenseConfig.validate_runtime()`** is now the single entry
  point for runtime validation. It rejects `QLIK_SERVER_URL` without a
  scheme, rejects schemes other than `http`/`https`, and warns on
  multi-segment virtual proxy prefixes (Qlik VPs are single-segment).
- **`QlikRepositoryAPI.__init__`** raises `QlikConnectionError` up
  front when `auth_mode == jwt` but no `JwtSession` was passed,
  instead of failing with an obscure 401 on the first request.
- **`qlik_jwt_admin.py issue-token`** warns on stderr when invoked on
  an interactive TTY — the token ends up in shell scrollback and
  must be treated as a password. It also warns when
  `--user-id-claim` or `--user-dir-claim` deviate from the
  documented defaults, since silent claim-name mismatches with the
  QMC VP configuration are the number-one cause of rejected tokens.

## [1.4.1] - 2026-04-07

### Added
- **`engine_get_field_range` MCP tool** — lightning-fast bounds query for
  a single field (`Count(DISTINCT)` + `Min` + `Max`) via a measures-only
  hypercube. Runs in seconds on any table size, regardless of row count.
  Prefer this over `get_app_field_statistics` for "what's the loaded
  period" / "what's the cardinality" questions.
- **`light` parameter on `get_app_field_statistics`** (default `True`).
  Light mode skips `Sum`/`Avg`/`Median`/`Mode`/`Stdev` — these are
  meaningless on date/text fields and extremely slow on big fact tables.
  Pass `full=true` only on small numeric fields.
- **`get_app_details.warnings` array** that flags huge fact tables
  (>500M rows / >100M rows), high-cardinality fields (>1M distinct
  values) and date-typed fields, each with concrete instructions about
  the right tool and pattern to use.
- **Hypercube query estimator hints**: rejection responses for
  `engine_create_hypercube` now carry `error_category` (`limit_exceeded`,
  `cell_cap_exceeded`, `socket_timeout`, `engine_api_error`,
  `connection_error`), `failed_step`, `failed_stage`, `elapsed_seconds`
  and a `hint` pointing at set-analysis / top-N / slice-by-category
  patterns.
- **`tool_call_seconds`** is injected as the first key of every MCP tool
  response (millisecond precision wall-clock time of the call). On
  exception the same envelope carries `error_type` and `tool` so the
  caller can attribute failures.
- **`docs/` folder** with seven topical pages: installation,
  configuration, usage, tools, architecture, development,
  troubleshooting. README is now a short landing page that links into
  `docs/`.
- **Disclaimer** in `README.md` and `LICENSE`: this project is an
  independent community integration, not affiliated with Qlik. All
  protocol information used was obtained from publicly available
  sources (help.qlik.com, qlik.dev, Qlik Community).

### Changed
- **Hard hypercube limits enforced server-side, before any RPC**:
  `engine_create_hypercube` now rejects requests with `max_rows > 5000`
  or `columns * max_rows > 9900` (Qlik Engine itself caps a single
  `NxPage` at 10000 cells with error `7009 calc-pages-too-large`). The
  rejection happens in milliseconds with a structured error and a hint
  — there is no auto-pagination. The LLM must design narrower queries
  via set analysis, top-N or slice-by-category.
- **`QLIK_WS_TIMEOUT` default raised from `8.0` s to `180.0` s**, now
  uniformly applied to BOTH the WebSocket handshake AND every Engine
  API call (`OpenDoc`, hypercube creation, `GetLayout`, field
  statistics).
- **Per-app WebSocket endpoint** is tried first when an `app_id` is
  known. `connect(app_id=...)` builds
  `wss://<host>:<engine_port>/app/<url-encoded-app-id>` as the
  preferred connection URL, falling back to the global
  `/app/engineData` endpoint.
- **All MCP tool docstrings rewritten** in English with generic
  placeholders (`<DimA>`, `<MetricX>`, `<val>` etc.). The
  `engine_create_hypercube` docstring documents the two hard rules
  explicitly: ALWAYS use set analysis (never `If()` inside an
  aggregate), and NEVER put expressions in `qFieldDefs` (per-row
  evaluation, not cached, no symbol-table use).
- **`get_app_field` falls back to a one-dimension hypercube** when the
  underlying `ListObject` returns an empty result. The response then
  includes `fallback_used: "hypercube"` and, on total failure, a
  `warning` field describing the next step.

### Fixed
- **Strict id-matching in `send_request`**. Every received WebSocket
  frame is parsed and only the frame whose `id` matches our `req_id` is
  treated as the answer. Notifications (`OnConnected`, `OnAuthenticated`,
  `OnSessionTimedOut`) are skipped at DEBUG. Late replies from a
  previously timed-out request are skipped at WARNING. Without this
  fix, a single timed-out hypercube call would leave stale data in the
  recv buffer that the next call consumed as its own response,
  cascading failures for the rest of the session.
- **`_kill_socket()` on any failure path**. Timeouts, parse errors and
  unexpected exceptions all force-close the WebSocket and invalidate
  the cached app handle. The next call opens a fresh connection
  instead of reusing a zombie socket.
- **`tests/test_server.py`** rewritten for the FastMCP architecture
  (the old `QlikSenseMCPServer` class no longer exists). Covers
  `_err`/`_ok`, version pin, 24-tool registration, core tool presence,
  and `_timed` decorator behaviour including exception handling. The
  full suite now passes again (97 tests).
- **`tests/test_config.py`** updated for the new `DEFAULT_WS_TIMEOUT`
  default value.

### Documentation
- README cut from ~800 to ~100 lines. The full content lives in `docs/`
  with one topic per file.
- All facts re-verified against current upstream sources: MCP spec
  2025-03-26 (Streamable HTTP transport), qlik.dev, help.qlik.com
  November 2025 (Engine error 7009, hypercube cell cap, standard QSE
  ports).
- All approximate numbers (`~`, `+`, "around", "about") removed from
  user-facing text.
- Copyright years updated to `2025-2026`.

## [1.4.0] - 2026-04-06

### Added
- **HTTP streaming transport**: server now runs with `streamable-http` MCP
  transport by default on `http://127.0.0.1:8000/mcp`. Legacy `stdio`
  transport remains available via the `--stdio` flag.
- **Cached Engine API connections**: `QlikEngineAPI` now keeps a single
  long-lived WebSocket and reuses the opened app handle across tool calls
  via the new `ensure_app(app_id)` entry point. Switching to another
  `app_id` closes the old app and opens the new one; dropped connections
  are transparently re-established (ping-based liveness check). This
  dramatically reduces load on the Qlik engine — no more
  connect/open/close on every single tool call.
- **`QLIK_WS_TIMEOUT` default raised to `180.0s`** and now uniformly
  applied to BOTH the WebSocket handshake AND every Engine API call
  (`OpenDoc`, hypercube creation, `GetLayout`, field statistics). A
  single knob is enough for the vast majority of setups; increase it
  further for very heavy hypercubes on large apps.

### Changed
- Major refactor of `server.py`: all Engine-based tools now use
  `engine_api.ensure_app(...)` instead of the previous
  `connect()` / `open_doc()` / ... / `disconnect()` boilerplate. Each tool
  is now a single-entry call that benefits from connection caching.
- `QlikEngineAPI.send_request()` accepts an optional per-request
  `timeout` argument and restores the previous socket timeout in a
  `finally` block.
- `open_doc` / `open_doc_safe` / `create_hypercube` /
  `get_field_statistics` now use `ws_operation_timeout` for their
  underlying `recv()` calls.

### Fixed
- `Connection timed out` errors on hypercube creation for large apps:
  the hypercube timeout was previously bound to the short
  `QLIK_WS_TIMEOUT` connection timeout. It is now controlled
  independently via `QLIK_WS_OPERATION_TIMEOUT`.

### Documentation
- Updated `README.md`: added "HTTP streaming mode" note, described
  connection caching and the two-timeouts model in the Architecture
  section, documented `QLIK_WS_OPERATION_TIMEOUT` in the environment
  variables reference.
- Updated `.env.example` and MCP configuration snippet with
  `QLIK_WS_OPERATION_TIMEOUT`.

## [1.3.4] - 2025-10-10

### Added
- Enhanced hypercube creation with explicit sorting options for dimensions and measures
- Support for custom sorting expressions in dimensions
- Option to create hypercubes without dimensions (measures-only)
- Improved sorting defaults: dimensions sort by ASCII ascending, measures sort by numeric descending

### Changed
- New configuration parameter `QLIK_HTTP_PORT` for metadata requests to `/api/v1/apps/{id}/data/metadata` endpoint
- Dynamic X-Qlik-Xrfkey generation for enhanced security (16 random alphanumeric characters)
- Utility function `generate_xrfkey()` for secure key generation

### Changed
- Replaced all static "0123456789abcdef" XSRF keys with dynamic generation
- Updated help output to use stderr instead of print to maintain MCP protocol compatibility
- Enhanced logging system throughout the codebase - replaced print statements with proper logging

### Removed
- Removed `size_bytes` parameter from `get_app_details` tool output (non-functional parameter)
- Eliminated all print() statements in favor of logging for MCP server compliance

### Documentation
- Updated README.md with new QLIK_HTTP_PORT configuration parameter
- Updated .env.example and mcp.json.example with QLIK_HTTP_PORT settings
- Enhanced configuration documentation with detailed parameter descriptions

## [1.3.2] - 2025-10-06

### Fixed
- Fixed published filter in get_apps function to properly handle filtering logic
- Removed numeric_value field from user variables and switched to text_value for more accurate data representation

### Changed
- Improved code readability by removing verbose output of user variable lists
- Enhanced user variable handling with better filtering for script-created variables
- Optimized variable data processing for improved performance and accuracy

## [1.3.1] - 2025-09-08

### Fixed
- Proxy API metadata request now respects `verify_ssl` configuration. Replaced conditional CA path logic with `self.config.verify_ssl` in `server.py` to ensure proper TLS verification behavior.

## [1.3.0] - 2025-09-08

### Added
- get_app_sheets: list sheets with titles and descriptions (Engine API)
- get_app_sheet_objects: list objects on a specific sheet with id, type, description (Engine API)
- get_app_object: retrieve specific object layout via GetObject + GetLayout (Engine API)

### Changed
- Upgraded MCP dependency to `mcp>=1.1.0`
- Improved logging configuration with LOG_LEVEL and structured stderr output
- Tunable Engine WebSocket behavior via environment variables: `QLIK_WS_TIMEOUT`, `QLIK_WS_RETRIES`
- Enhanced field statistics calculation and debug information in server responses
- README updated to include new tools and examples; MCP configuration extended

### Fixed
- More robust app open logic (`open_doc_safe`) and better error messages for Engine operations
- Safer cleanup for temporary session objects during Engine operations

### Documentation
- Updated `README.md` with API Reference for new tools and optional environment variables
- Updated `mcp.json.example` autoApprove list to include new tools

[2.1.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v2.0.1...v2.1.0
[2.2.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v2.1.0...v2.2.0
[2.0.2]: https://github.com/bintocher/qlik-sense-mcp/compare/v2.0.1...v2.0.2
[1.4.1]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.4...v1.4.0
[1.3.4]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.3...v1.3.4
[1.3.2]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.2.0...v1.3.0
