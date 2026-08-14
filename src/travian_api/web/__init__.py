"""Web UI package. Running it requires the ``web`` extra."""


def main() -> None:
    """Entry point for the ``travian-web`` console script.

    Lives here, outside the fastapi import chain, so a base install without the
    ``web`` extra fails with instructions instead of a bare ModuleNotFoundError.
    """
    try:
        from travian_api.web.app import main as run
    except ImportError as exc:
        raise SystemExit(
            "travian-web needs the web dependencies. "
            "Install them with: pip install 'travian-api[web]'"
        ) from exc
    run()
