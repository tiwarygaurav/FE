"""Urja Meter API: a clean REST API over the Urja Meter Ops portal."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("urja-api")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"


def main() -> None:
    """Console entry point: `urja-api` (or `python -m urja_api`)."""
    import argparse
    import logging

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Urja Meter API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # httpcore's DEBUG output includes response headers, i.e. the portal's session cookie.
    logging.getLogger("httpcore").setLevel(max(logging.INFO, logging.getLogger().level))
    uvicorn.run("urja_api.api.app:create_app", factory=True, host=args.host, port=args.port, log_level=args.log_level)
