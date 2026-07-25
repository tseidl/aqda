"""Entry point for running AQDA as a module or via the console script."""

import argparse
import threading
import webbrowser

import uvicorn

from aqda import __version__

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


# Build the argument parser for the `aqda` console script.
def build_parser():
    parser = argparse.ArgumentParser(
        prog="aqda",
        description="AQDA — Augmented Qualitative Data Analysis. "
        "Starts the local web app and opens it in your browser.",
    )
    parser.add_argument("--version", action="version", version=f"aqda {__version__}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"Interface to bind (default: {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser window on startup"
    )
    return parser


def main():
    args = build_parser().parse_args()

    # Open browser after a short delay to let the server start
    def open_browser():
        import time

        time.sleep(1.5)
        webbrowser.open(f"http://{args.host}:{args.port}")

    if not args.no_browser:
        threading.Thread(target=open_browser, daemon=True).start()

    print(f"\n  AQDA is running at http://{args.host}:{args.port}\n")
    from aqda.app import app

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    # uvicorn exits with status 3 itself if the port is already in use.
    server.run()


if __name__ == "__main__":
    main()
