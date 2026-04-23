"""Entry point for running the Travian API CLI as a module.

Usage: python -m travian_api [command]
"""

from .cli import app

if __name__ == "__main__":
    app()
