# Development

## Environment

Use [uv](https://docs.astral.sh/uv/) for dependency management. The
`Makefile` wraps the common workflow:

```bash
# Create a venv and install the package + dev extras in editable mode
make dev

# List all targets
make help

# Build the wheel and sdist into ./dist
make build
```

The `dev` extras (defined in [`pyproject.toml`](../pyproject.toml))
include `build`, `twine`, `bump2version`, `pytest` and
`pytest-asyncio`.

## Tests

Pytest discovers everything under [`tests/`](../tests/):

```bash
pytest
```

The suite is offline — no Qlik server is required. Engine behaviour is
covered by driving `create_hypercube` against a fake `send_request` and
asserting on the generated `qHyperCubeDef`
([`tests/test_hypercube.py`](../tests/test_hypercube.py)), which is how
the sorting contract (`qInterColumnSortOrder`, per-column direction) is
pinned down. Tool visibility per authentication mode is covered by
re-importing `server.py` under different environments
([`tests/test_tool_registration.py`](../tests/test_tool_registration.py)).

Changes to Engine query building should still be smoke-tested against a
real app before release — the offline tests verify the request we send,
not what Qlik does with it.

## Versioning

The project uses [bump2version](https://pypi.org/project/bump2version/)
through `make` targets. Each target bumps the version, commits the
change and opens a pull request:

```bash
make version-patch    # 1.5.0 -> 1.5.1
make version-minor    # 1.5.0 -> 1.6.0
make version-major    # 1.5.0 -> 2.0.0
```

The PyPI package version is read from `pyproject.toml`.

## Adding a new tool

1. Implement the underlying method on `QlikRepositoryAPI` or
   `QlikEngineAPI`. Add a clear docstring and return a plain `dict`.
2. Register a new function in
   [`server.py`](../qlik_sense_mcp_server/server.py):
   ```python
   @mcp.tool()
   @_timed
   def my_new_tool(app_id: str, foo: int = 10) -> str:
       """
       One-paragraph summary of what the tool does and when to call it.

       Args:
           app_id: ...
           foo: ...

       Returns:
           ...
       """
       e = _check()
       if e:
           return e
       try:
           app_handle = engine_api.ensure_app(app_id, no_data=False)
           result = engine_api.my_method(app_handle, foo)
           return _ok(result)
       except Exception as ex:
           return _err(str(ex))
   ```
3. Both decorators are required:
   - `@mcp.tool()` registers the function with FastMCP. Use
     `@_cert_only_tool()` instead if the tool needs QRS admin rights
     (reload-task administration) — it registers the tool in
     certificate mode only, so JWT analysts are not offered calls that
     can only return 403.
   - `@_timed` wraps the response with `tool_call_seconds` and a
     structured error envelope, and echoes the failing `request` back
     to the caller. You get both for free; do not hand-roll them.
4. Write the docstring for an LLM, not for a human reader. Every tool
   ends with an `Example:` block showing a realistic `Call:` and a
   shortened but structurally correct `Returns:`. Document the actual
   response keys — a docstring that promises keys the tool does not
   return is worse than no docstring, because the model will build its
   next call around them.
5. Update [`docs/tools.md`](tools.md).
6. Update [`CHANGELOG.md`](../CHANGELOG.md).

New tools normally do not need any JWT-aware code: cert vs JWT auth is
abstracted inside `QlikRepositoryAPI` / `QlikEngineAPI`, and the
session bootstrap + cache lives in
[`jwt_session.py`](../qlik_sense_mcp_server/jwt_session.py). Touch
those modules only when you change the auth protocol itself.

## Admin tooling

[`tools/qlik_jwt_admin.py`](../tools/qlik_jwt_admin.py) is a standalone
admin CLI for JWT mode. It generates the RSA keypair + self-signed
X.509 certificate the Qlik QMC virtual proxy expects (`init-keys`) and
issues per-analyst JWTs signed with that key (`issue-token`). It does
not depend on the running MCP server. See
[`docs/AUTH_JWT.md`](AUTH_JWT.md) for the full setup walkthrough,
including the QMC fields and the security model.

## Release checklist

1. Bump the version with `make version-<level>`.
2. Update `CHANGELOG.md` with the changes.
3. Merge the PR.
4. CI publishes to PyPI on tag.
