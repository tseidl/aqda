"""Entry point for running AQDA as a module or via the console script."""

import signal
import sys
import webbrowser
import threading

import uvicorn


def _force_exit(signum, frame):
    """Force exit on second Ctrl+C."""
    print("\n  Shutting down...")
    sys.exit(0)


def main():
    host = "127.0.0.1"
    port = 8765

    # Allow Ctrl+C to always work (even during long async operations)
    signal.signal(signal.SIGINT, _force_exit)
    signal.signal(signal.SIGTERM, _force_exit)

    # Open browser after a short delay to let the server start
    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://{host}:{port}")

    threading.Thread(target=open_browser, daemon=True).start()

    print(f"\n  AQDA is running at http://{host}:{port}\n")
    uvicorn.run("aqda.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
