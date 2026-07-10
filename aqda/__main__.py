"""Entry point for running AQDA as a module or via the console script."""

import webbrowser
import threading

import uvicorn


def main():
    host = "127.0.0.1"
    port = 8765

    # Open browser after a short delay to let the server start
    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://{host}:{port}")

    threading.Thread(target=open_browser, daemon=True).start()

    print(f"\n  AQDA is running at http://{host}:{port}\n")
    from aqda.app import app

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()
