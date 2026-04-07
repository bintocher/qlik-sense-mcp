# Quick Commands Reference

## Installation and Usage

```bash
# Install and run with uvx (recommended)
uvx qlik-sense-mcp-server

# Install from PyPI
pip install qlik-sense-mcp-server

# Run installed package
qlik-sense-mcp-server
```

## Development

```bash
# Setup development environment
make dev

# See all available commands
make help

# Build package
make build
```

## Version Management

```bash
# Bump version and create PR
make version-patch  # 1.0.0 -> 1.0.1
make version-minor  # 1.0.0 -> 1.1.0
make version-major  # 1.0.0 -> 2.0.0
```

## Publishing

```bash
# After merging PR with version bump
git tag v1.0.1
git push origin v1.0.1

# GitHub Actions will automatically publish to PyPI
```