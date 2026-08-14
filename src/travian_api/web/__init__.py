"""Web UI package. Running it requires the ``web`` extra."""

# Top-level modules the ``web`` extra provides (see pyproject.toml). Only a
# failure to find one of THESE means the extra is missing; any other import
# error is a real bug and must surface as itself, not as install advice.
_WEB_EXTRA_MODULES = frozenset(
    {"fastapi", "uvicorn", "sqlalchemy", "aiosqlite", "bcrypt", "jwt", "cryptography", "multipart"}
)


def main() -> None:
    """Entry point for the ``travian-web`` console script.

    Lives here, outside the fastapi import chain, so a base install without the
    ``web`` extra fails with instructions instead of a bare ModuleNotFoundError.
    """
    try:
        from travian_api.web.app import main as run
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.partition(".")[0] in _WEB_EXTRA_MODULES:
            raise SystemExit(
                "travian-web needs the web dependencies. "
                "Install them with: pip install 'travian-api[web]'"
            ) from exc
        raise
    run()
