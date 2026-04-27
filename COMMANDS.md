# Quick Commands Reference

Quick reference. For full documentation see `docs/`:
`installation.md` (install + certs) · `configuration.md` (env vars) · `usage.md` (MCP client setup) · `tools.md` (tool catalog) · `development.md` (dev + tests) · `architecture.md` · `troubleshooting.md` · `AUTH_JWT.md` (JWT mode).

## Run

```bash
# One-shot via uvx (no global install)
uvx qlik-sense-mcp-server

# Run installed package
qlik-sense-mcp-server
```

## Install

```bash
# From PyPI
pip install qlik-sense-mcp-server

# Or use uv
uv tool install qlik-sense-mcp-server
```

See `docs/installation.md` for client-certificate export and `docs/configuration.md` for env vars.

## Develop

```bash
# Bootstrap venv + dev deps
make dev

# All available targets
make help

# Build sdist + wheel
make build
```

See `docs/development.md` for tests, lint, and contribution flow.

## Release

```bash
# Bump version, open PR (current line: 1.5.0)
make version-patch  # 1.5.0 -> 1.5.1
make version-minor  # 1.5.0 -> 1.6.0
make version-major  # 1.5.0 -> 2.0.0

# After PR merge: tag and push — GitHub Actions publishes to PyPI
git tag v1.5.1
git push origin v1.5.1
```

## JWT (admin)

```bash
# 1. Generate signing key + self-signed cert (run once, admin machine only)
python tools/qlik_jwt_admin.py init-keys --out ./jwt_keys

# 2. Issue a per-analyst token (defaults: --days 90, RS256, claims userId/userDirectory)
python tools/qlik_jwt_admin.py issue-token \
    --key ./jwt_keys/jwt_private.pem \
    --user-id ivanov \
    --user-directory COMPANY \
    --days 90

# Long-lived service token
python tools/qlik_jwt_admin.py issue-token \
    --key ./jwt_keys/jwt_private.pem \
    --user-id svc_ai --user-directory COMPANY --days 365

# Scripting (token-only on stdout)
python tools/qlik_jwt_admin.py issue-token --key ./jwt_keys/jwt_private.pem \
    --user-id ivanov --user-directory COMPANY --quiet
```

Paste `./jwt_keys/jwt_cert.pem` into the QMC JWT virtual proxy. Full setup, QMC fields, and troubleshooting: see `docs/AUTH_JWT.md`.
