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
   - `@mcp.tool()` registers the function with FastMCP.
   - `@_timed` wraps the response with `tool_call_seconds` and a
     structured error envelope.
4. Update [`docs/tools.md`](tools.md).
5. Update [`CHANGELOG.md`](../CHANGELOG.md).

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
