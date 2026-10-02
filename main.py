"""
Legacy root entrypoint.

Preferred interfaces:
  - CLI: ``python -m cli.main`` or ``tradingagents ...``
  - Web UI: ``make ui``
"""

from cli.main import app


if __name__ == "__main__":
    app()
