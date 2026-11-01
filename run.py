"""Start the local app: http://127.0.0.1:8770 (only reachable from this computer)."""
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crawler.server import serve  # noqa: E402

PORT = int(os.environ.get("PORT", "8770"))

if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print(f"App running at {url}  (keep this window open; close it to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        serve(PORT)
    except OSError as e:
        print(f"\nCould not start on port {PORT}: {e}\n"
              "The app is probably already running — open the address above in your browser, "
              "or close the other window first.")
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to close...")
    except KeyboardInterrupt:
        pass
