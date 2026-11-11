"""Start the local app: http://127.0.0.1:8770 (only reachable from this computer)."""
import os
import sys
import threading
import traceback
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PORT", "8770"))


def _hold(msg: str) -> None:
    """Show a start-up problem and keep the window open so it can be read."""
    print("\n" + msg)
    log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "startup_error.log")
    try:
        with open(log, "w", encoding="utf-8") as f:
            f.write(msg + "\n\n" + traceback.format_exc())
        print(f"(Details saved to {log} — send this file if you need help.)")
    except OSError:
        pass
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        from crawler import paths
        from crawler.server import make_server
    except Exception as e:  # noqa: BLE001
        _hold(f"The app could not load: {e}\nRun the start file again; it reinstalls what is missing.")
        return
    try:
        paths.migrate()
    except Exception as e:  # noqa: BLE001  never let a copy problem stop the app
        print(f"Note: could not copy data from an older copy ({e}). Continuing.")
    try:
        httpd = make_server(PORT)          # bind first, so the browser never opens before the app is listening
    except OSError as e:
        _hold(f"Could not start on port {PORT}: {e}\n"
              f"The app is probably already running in another window — open {url} in your browser, "
              "or close the other window and start again.")
        return
    try:
        from crawler import schedule
        schedule.get().start()                # scheduled searches run while this window is open
        from crawler import telegram_bot
        telegram_bot.start()                  # TELEGRAM_BOT_TOKEN set: the team's chat can check and tap buttons
    except Exception as e:  # noqa: BLE001
        print(f"Note: schedules are not running ({e}).")
    print(f"Account lists and results: {os.path.dirname(paths.LISTS_DIR)}")
    print(f"App running at {url}  (keep this window open; close it to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        _hold(f"The app stopped because of an error: {e}")
