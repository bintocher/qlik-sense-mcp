# Troubleshooting

## Connection problems

### `SSL: CERTIFICATE_VERIFY_FAILED`

The CA certificate path is wrong, the certificate is expired, or the
hostname does not match. Steps:

1. Check that `QLIK_CA_CERT_PATH` points at the actual CA file used to
   sign the Qlik Sense node certificate.
2. Confirm the cert hasn't expired:
   `openssl x509 -in /path/to/root.pem -noout -dates`.
3. As a temporary workaround for self-signed dev clusters, set
   `QLIK_VERIFY_SSL=false`. Do not use this in production.

### `ConnectionError: Failed to connect to Engine API`

The Engine WebSocket port (default `4747`) is unreachable, or the
client certificate was rejected. Steps:

1. Confirm the port is open from the host running the MCP server:
   `openssl s_client -connect qlik.company.com:4747 -cert client.pem -key client_key.pem -CAfile root.pem`.
2. Check `QLIK_ENGINE_PORT` if your cluster uses a non-standard port.
3. Verify the user listed in `QLIK_USER_DIRECTORY` / `QLIK_USER_ID`
   exists in QMC and has access to the Engine.

### `401 Unauthorized` from the Repository API

1. Confirm `QLIK_USER_DIRECTORY` and `QLIK_USER_ID` are correct and
   exist in QMC.
2. Confirm the client certificate maps to that user.
3. Confirm the user has at least the `RootAdmin` or `ContentAdmin`
   role, or the equivalent custom role with read access to apps and
   tasks.

## Hypercube errors

### `WebSocket recv() timed out after 180.0s waiting for response to Engine method 'GetLayout'`

Engine is genuinely computing something slow. Order of fixes:

1. **Add a set-analysis filter** inside every measure. Narrowing the
   period or category by even one value usually cuts the cost by
   orders of magnitude on a wide fact table.
2. **Reduce `max_rows`.** The hard server cap is 5000, but for top-N
   queries you almost always want 15-50.
3. **Drop a high-cardinality dimension** if you have one. Sorting a
   dimension with one million distinct values by a measure expression
   forces a full materialization. Use the SLICE-BY-CATEGORY pattern
   from the `engine_create_hypercube` docstring instead.
4. As a last resort, raise `QLIK_WS_TIMEOUT` (e.g. to `300.0`) to
   give the Engine more time. Do not raise it for the entire MCP
   session — most of the time the right answer is to redesign the
   query.

### `error_category: limit_exceeded` / `cell_cap_exceeded`

You asked for too many rows. The error response contains a `hint`
with the exact alternative values. See the `engine_create_hypercube`
docstring for the four standard fixes (set-analysis filter, top-N,
slice-by-category, drop a dimension).

### `error_category: engine_api_error`

Qlik rejected the expression. The full Engine error is in the `error`
field. Common causes:

- Field name is misspelled or in the wrong case. Always copy field
  names from `get_app_details` (`fields[*].name`) — they are
  case-sensitive.
- Set-analysis values use the wrong quoting. Numbers go without
  quotes, text in single quotes, wildcards in double quotes. See the
  set-analysis quick reference in the `engine_create_hypercube`
  docstring.
- A measure references a field that lives in a disconnected island
  of the data model.

## Configuration self-test

```bash
python -c "
from qlik_sense_mcp_server.config import QlikSenseConfig
cfg = QlikSenseConfig.from_env()
print('server_url        =', cfg.server_url)
print('user_directory    =', cfg.user_directory)
print('user_id           =', cfg.user_id)
print('client_cert_path  =', cfg.client_cert_path)
print('verify_ssl        =', cfg.verify_ssl)
print('repository_port   =', cfg.repository_port)
print('engine_port       =', cfg.engine_port)
"
```

## Verbose logging

Set `LOG_LEVEL=DEBUG` in the env block. Each hypercube call then logs
`CreateSessionObject`, `GetLayout` and any follow-up Engine method with
their durations:

```
create_hypercube: planning qInitialDataFetch height=1000 (max_rows=1000, columns=4, cells=4000/9900)
create_hypercube: CreateSessionObject (dims=1, measures=3, max_rows=1000, op_timeout=180.0s)
create_hypercube: CreateSessionObject done in 0.45s
create_hypercube: GetLayout (cube_handle=2)
create_hypercube: GetLayout done in 12.30s
```

This is the fastest way to figure out which Engine call is the
bottleneck.

## Measuring tool performance

Every tool response begins with `tool_call_seconds` — wall-clock time
of the call rounded to milliseconds. Use this to identify slow calls in
your own session, against your own data — there are no hard-coded
"typical" numbers, because every Qlik app has different sizes and
selections.

If a single hypercube call takes more than a few seconds against a
small app, the issue is almost always one of the four covered above:
missing set-analysis filter, dimension expression in `qFieldDefs`,
high-cardinality dimension sorted by an expression, or `Aggr()` over a
high-cardinality dimension.
