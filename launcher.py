"""
launcher.py - Pathogene Desktop Launcher

tkinter MUST run on the main thread.
We use a module-level queue in a shared bridge module so Flask worker
threads can request a dialog and block until the main thread runs it.
"""

import multiprocessing
import os
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_internal_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "_internal"
        if candidate.exists():
            return candidate
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


APP_DIR      = get_app_dir()
INTERNAL_DIR = get_internal_dir()
BACKEND_DIR  = INTERNAL_DIR / "backend"
FRONTEND_DIR = INTERNAL_DIR / "frontend"
DATA_DIR     = APP_DIR / "data"
PORT         = 8000
LOG_FILE     = APP_DIR / "pathogene.log"

# ---------------------------------------------------------------------------
# Dialog bridge — shared queue injected into sys.modules BEFORE app.py loads
# ---------------------------------------------------------------------------
# We create a tiny fake module object and put it in sys.modules["_dialog_bridge"]
# so that app.py can do:
#   import _dialog_bridge
#   path = _dialog_bridge.request_dialog("svs")

import types as _types

_bridge = _types.ModuleType("_dialog_bridge")
_bridge._request_queue = queue.Queue()


def _bridge_request_dialog(kind: str):
    """Called from Flask worker thread. Blocks until main thread returns result."""
    result_q = queue.Queue()
    _bridge._request_queue.put({"kind": kind, "result_queue": result_q})
    try:
        return result_q.get(timeout=120)
    except queue.Empty:
        return None


_bridge.request_dialog = _bridge_request_dialog
sys.modules["_dialog_bridge"] = _bridge


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# tkinter dialog — always runs on main thread
# ---------------------------------------------------------------------------

def _run_dialog_main_thread(kind: str):
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", True)
    root.update()

    if kind == "svs":
        path = filedialog.askopenfilename(
            parent=root,
            title="Select H&E slide",
            filetypes=[
                ("Whole Slide Images", "*.svs *.tif *.tiff *.ndpi *.scn"),
                ("All Files", "*.*"),
            ],
        )
    else:
        path = filedialog.askopenfilename(
            parent=root,
            title="Select CosMx image",
            filetypes=[
                ("PNG Images", "*.png"),
                ("Image Files", "*.png *.jpg *.jpeg *.tif *.tiff"),
                ("All Files", "*.*"),
            ],
        )

    root.destroy()
    return path or None


def _pump_dialog_queue():
    """Drain pending dialog requests. Called from main thread loop every 100ms."""
    while True:
        try:
            req = _bridge._request_queue.get_nowait()
        except queue.Empty:
            break
        try:
            result = _run_dialog_main_thread(req["kind"])
        except Exception as e:
            log(f"Dialog error: {e}")
            result = None
        req["result_queue"].put(result)


# ---------------------------------------------------------------------------
# OpenSlide DLL (Windows only)
# ---------------------------------------------------------------------------

def _add_openslide_dll():
    if sys.platform != "win32":
        return

    added = False

    # 1) Manually bundled openslide-win64/bin (legacy fallback)
    dll_dir = INTERNAL_DIR / "openslide-win64" / "bin"
    if dll_dir.exists():
        try:
            os.add_dll_directory(str(dll_dir))
            log(f"OpenSlide DLL path added (bundled): {dll_dir}")
            added = True
        except Exception as e:
            log(f"WARN: could not add bundled OpenSlide DLL path: {e}")

    # 2) openslide-bin package ships DLLs inside the package directory.
    #    PyInstaller copies them into _internal/, so search there too.
    for candidate in [
        INTERNAL_DIR,
        INTERNAL_DIR / "openslide",
        INTERNAL_DIR / "openslide_bin",
    ]:
        if candidate.exists():
            dlls = list(candidate.glob("libopenslide*.dll")) + list(candidate.glob("openslide*.dll"))
            if dlls:
                try:
                    os.add_dll_directory(str(candidate))
                    log(f"OpenSlide DLL path added (openslide-bin): {candidate}")
                    added = True
                    break
                except Exception as e:
                    log(f"WARN: could not add {candidate}: {e}")

    # 3) Let openslide-python find its own DLLs (works when openslide-bin is installed)
    if not added:
        try:
            import openslide
            log("OpenSlide loaded via openslide-python built-in loader.")
        except Exception as e:
            log(f"WARN: OpenSlide not available: {e}. SVS files may not open.")


# ---------------------------------------------------------------------------
# Flask server (runs in background thread)
# ---------------------------------------------------------------------------

def _find_venv_python() -> str:
    """
    Find the venv python.exe that was used to build this bundle.
    It lives next to the exe in a .venv-build folder, or the user
    can override via PATHOGENE_PYTHON env var before launching.
    """
    # Already set externally (e.g. by build.bat during testing)
    override = os.environ.get("PATHOGENE_PYTHON", "").strip()
    if override and Path(override).exists():
        return override

    # Standard location: <app_dir>/../.venv-build/Scripts/python.exe
    # This works when dist/Pathogene/ is inside the project folder.
    candidates = [
        APP_DIR.parent / ".venv-build" / "Scripts" / "python.exe",   # dist/Pathogene -> dist
        APP_DIR.parent.parent / ".venv-build" / "Scripts" / "python.exe",  # dist/Pathogene -> project root
        APP_DIR.parent / ".venv-build" / "bin" / "python",
        APP_DIR.parent.parent / ".venv-build" / "bin" / "python",
        APP_DIR / ".venv-build" / "Scripts" / "python.exe",
        APP_DIR / ".venv-build" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            return str(c)

    # Fallback: write a small helper that locates itself
    return ""


def start_flask():
    try:
        os.environ["PATHOGENE_BACKEND_DIR"]  = str(BACKEND_DIR)
        os.environ["PATHOGENE_FRONTEND_DIR"] = str(FRONTEND_DIR)
        os.environ["PATHOGENE_DATA_DIR"]     = str(DATA_DIR)

        # Set PATHOGENE_PYTHON so app.py subprocess calls use a real interpreter
        py = _find_venv_python()
        if py:
            os.environ["PATHOGENE_PYTHON"] = py
            log(f"PATHOGENE_PYTHON: {py}")
        else:
            log("WARN: Could not find venv python.exe. Script execution may fail.")

        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))

        import importlib.util
        app_py = BACKEND_DIR / "app.py"
        if not app_py.exists():
            log(f"ERROR: app.py not found at {app_py}")
            return

        spec    = importlib.util.spec_from_file_location("app_pathogene", str(app_py))
        app_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app_mod)

        flask_app = app_mod.app
        log(f"Flask starting on http://127.0.0.1:{PORT}/")
        flask_app.run(
            host="127.0.0.1",
            port=PORT,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
    except Exception as e:
        import traceback
        log(f"Flask startup failed: {e}")
        log(traceback.format_exc())


def wait_for_server(timeout: int = 30) -> bool:
    import urllib.request
    url      = f"http://127.0.0.1:{PORT}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    multiprocessing.freeze_support()

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass

    log("=" * 60)
    log("Pathogene starting")
    log("=" * 60)
    log(f"APP_DIR      : {APP_DIR}")
    log(f"INTERNAL_DIR : {INTERNAL_DIR}")
    log(f"BACKEND_DIR  : {BACKEND_DIR}")
    log(f"FRONTEND_DIR : {FRONTEND_DIR}")
    log(f"DATA_DIR     : {DATA_DIR}")
    log(f"frozen       : {getattr(sys, 'frozen', False)}")
    log("=" * 60)

    if not BACKEND_DIR.exists():
        log(f"ERROR: backend/ not found: {BACKEND_DIR}")
        input("Press Enter to exit...")
        sys.exit(1)

    if not FRONTEND_DIR.exists():
        log(f"ERROR: frontend/ not found: {FRONTEND_DIR}")
        input("Press Enter to exit...")
        sys.exit(1)

    for sub in ["slides", "tiles", "cosmx", "cosmx_tiles",
                "annotations", "qc_results", "uploads"]:
        try:
            (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log(f"WARN: could not create data/{sub}: {e}")

    _add_openslide_dll()

    server_thread = threading.Thread(target=start_flask, daemon=True)
    server_thread.start()

    log(f"Waiting for Flask server on port {PORT}...")
    if not wait_for_server(timeout=30):
        log("ERROR: server did not respond within 30 seconds.")
        input("Press Enter to exit...")
        sys.exit(1)

    url = f"http://127.0.0.1:{PORT}/"
    log(f"Opening browser: {url}")
    webbrowser.open(url)

    log("Running. Close this window to stop the app.")
    log(f"Log file: {LOG_FILE}")

    # Main loop: pump dialog queue every 100ms
    try:
        while True:
            _pump_dialog_queue()
            time.sleep(0.1)
    except KeyboardInterrupt:
        log("Shutting down.")


if __name__ == "__main__":
    main()
