.PHONY: help install dev clean build version-patch version-minor version-major publish create-pr git-clean

# Default target
help:
	@echo "Available commands:"
	@echo "  install        - Install package in development mode"
	@echo "  dev            - Setup development environment"
	@echo "  clean          - Clean build artifacts"
	@echo "  build          - Build package for distribution"
	@echo "  version-patch  - Bump patch version and create PR"
	@echo "  version-minor  - Bump minor version and create PR"
	@echo "  version-major  - Bump major version and create PR"
	@echo "  publish        - Publish to PyPI (automated via GitHub Actions)"
	@echo "  create-pr      - Create pull request for current changes"
	@echo "  git-clean      - Clean git history (DESTRUCTIVE)"

# Development setup
install:
	pip install -e .

dev:
	pip install -e ".[dev]"
	pip install build twine bump2version

# Clean build artifacts
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Build package
build: clean
	python -m build

# Version bumping with PR creation
version-patch:
	@echo "Bumping patch version..."
	bump2version patch
	$(MAKE) create-pr

version-minor:
	@echo "Bumping minor version..."
	bump2version minor
	$(MAKE) create-pr

version-major:
	@echo "Bumping major version..."
	bump2version major
	$(MAKE) create-pr

# Create pull request
create-pr:
	@VERSION=$$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/'); \
	BRANCH="release/v$$VERSION"; \
	echo "Creating PR for version $$VERSION on branch $$BRANCH"; \
	git checkout -b "$$BRANCH"; \
	git add .; \
	git commit -m "chore: bump version to $$VERSION"; \
	git push origin "$$BRANCH"; \
	gh pr create --title "Release v$$VERSION" --body "Automated version bump to $$VERSION" --base main --head "$$BRANCH"

# Publish (triggered by GitHub Actions)
publish: build
	@echo "Publishing via GitHub Actions - create and push a version tag"
	@echo "Example: git tag v1.0.0 && git push origin v1.0.0"

# Clean git history (DESTRUCTIVE)
git-clean:
	@echo "WARNING: This will completely reset git history!"
	@read -p "Are you sure? (y/N): " confirm && [ "$$confirm" = "y" ] || exit 1
	rm -rf .git
	git init
	git add .
	git commit -m "chore: initial commit"
	@echo "Git history cleaned. Set remote with: git remote add origin <url>"
