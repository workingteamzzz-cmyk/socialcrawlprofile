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
    print(f"Aplikasi berjalan di {url}  (biarkan jendela ini terbuka; tutup untuk berhenti)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        serve(PORT)
    except KeyboardInterrupt:
        pass
