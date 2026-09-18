# app_web.py
# -*- coding: utf-8 -*-
"""
Pathogene Web Backend

Flask web version of the pywebview Pathogene app.

Expected structure:
E:/병리/
├─ backend/
│  ├─ app_web.py
│  ├─ make_dzi.py
│  ├─ make_cosmx_dzi.py
│  ├─ register_anchors.py
│  └─ openslide-win64/          optional
├─ frontend/
│  ├─ index.html
│  ├─ viewer.js
│  └─ logo.png
└─ data/
   ├─ slides/
   ├─ tiles/
   ├─ cosmx/
   ├─ cosmx_tiles/
   ├─ annotations/
   └─ qc_results/

Run:
    cd /d E:\병리\backend
    python app_web.py

Open:
    http://localhost:8000/
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
import importlib
import importlib.util
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request, send_from_directory, Response
from werkzeug.utils import secure_filename
from flask_cors import CORS

# =============================================================================
# PATH CONFIG
# =============================================================================

def _resolve_dir(env_key: str, fallback: Path) -> Path:
    """Resolve a runtime directory.

    The packaged app sets PATHOGENE_BACKEND_DIR, PATHOGENE_FRONTEND_DIR, and
    PATHOGENE_DATA_DIR from the launcher. In development, these variables are
    usually absent, so we fall back to paths relative to this file.
    """
    value = os.environ.get(env_key, "").strip()
    return Path(value).resolve() if value else fallback.resolve()


_FILE_BACKEND_DIR = Path(__file__).resolve().parent
_FILE_PROJECT_DIR = _FILE_BACKEND_DIR.parent

BACKEND_DIR = _resolve_dir("PATHOGENE_BACKEND_DIR", _FILE_BACKEND_DIR)
FRONTEND_DIR = _resolve_dir("PATHOGENE_FRONTEND_DIR", _FILE_PROJECT_DIR / "frontend")
DATA_DIR = _resolve_dir("PATHOGENE_DATA_DIR", _FILE_PROJECT_DIR / "data")

# Keep PROJECT_DIR for compatibility with old endpoints.
PROJECT_DIR = DATA_DIR.parent

SLIDES_DIR = DATA_DIR / "slides"
TILES_DIR = DATA_DIR / "tiles"
COSMX_DIR = DATA_DIR / "cosmx"
COSMX_TILES_DIR = DATA_DIR / "cosmx_tiles"
ANNOTATIONS_DIR = DATA_DIR / "annotations"
QC_DIR = DATA_DIR / "qc_results"
UPLOADS_DIR = DATA_DIR / "uploads"

for d in [DATA_DIR, SLIDES_DIR, TILES_DIR, COSMX_DIR, COSMX_TILES_DIR, ANNOTATIONS_DIR, QC_DIR, UPLOADS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Optional OpenSlide DLL path for Windows development and packaged app.
_openslide_candidates = [
    BACKEND_DIR / "openslide-win64" / "bin",
    BACKEND_DIR.parent / "openslide_bin",
]
if getattr(sys, "frozen", False):
    try:
        _openslide_candidates.append(Path(sys.executable).resolve().parent / "_internal" / "openslide_bin")
    except Exception:
        pass

for dev_dll in _openslide_candidates:
    if dev_dll.exists():
        try:
            os.add_dll_directory(str(dev_dll))
            print(f"[INIT] OpenSlide DLL path added: {dev_dll}")
            break
        except Exception as e:
            print(f"[WARN] Could not add OpenSlide DLL path: {e}")

_backend_str = str(BACKEND_DIR)
sys.path = [_backend_str] + [p for p in sys.path if p != _backend_str]
print(f"[INIT] sys.path[0] = {sys.path[0]}")
print(f"[INIT] BACKEND_DIR  = {BACKEND_DIR}")
print(f"[INIT] FRONTEND_DIR = {FRONTEND_DIR}")
print(f"[INIT] DATA_DIR     = {DATA_DIR}")

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
CORS(app)

_pipeline_lock = threading.Lock()
_pipeline_running = False
_pipeline_status: Dict[str, Any] = {
    "running": False,
    "slide_id": None,
    "step": None,
    "total": 0,
    "current": 0,
    "logs": [],
    "error": None,
    "done": False,
    "need_anchors": False,
    "anchor_payload": None,
    "need_preview": False,
    "preview_payload": None,
}

_anchor_event = threading.Event()
_pending_anchors: Optional[Dict[str, Any]] = None

# Preview-first pipeline context. This stores the latest slide prepared by
# /api/pipeline/run so /api/pipeline/confirm can start the heavy tiling step.
_preview_context: Dict[str, Any] = {}

# =============================================================================
# UTILS
# =============================================================================

def _reset_status(slide_id: str, steps: list[str]) -> None:
    global _pipeline_status
    _pipeline_status = {
        "running": True,
        "slide_id": slide_id,
        "step": None,
        "total": len(steps),
        "current": 0,
        "steps": steps,
        "logs": [],
        "error": None,
        "done": False,
        "need_anchors": False,
        "anchor_payload": None,
        "need_preview": False,
        "preview_payload": None,
    }


def _log(line: str) -> None:
    line = str(line).rstrip()
    if not line:
        return
    print(line)
    _pipeline_status.setdefault("logs", []).append(line)


def _set_step(idx: int, name: str) -> None:
    _pipeline_status["current"] = idx
    _pipeline_status["step"] = name
    _log(f"[{idx}/{_pipeline_status.get('total', '?')}] {name}")


def _fail(step: str, exc: Exception) -> None:
    global _pipeline_running
    msg = f"{step} failed: {exc}"
    _pipeline_status["error"] = msg
    _pipeline_status["running"] = False
    _pipeline_status["done"] = False
    _pipeline_status["need_anchors"] = False
    _log("[ERROR] " + msg)
    _log(traceback.format_exc())
    _pipeline_running = False


def _finish(slide_id: str) -> None:
    global _pipeline_running
    _pipeline_status["running"] = False
    _pipeline_status["done"] = True
    _pipeline_status["need_anchors"] = False
    _pipeline_status["slide_id"] = slide_id
    _log(f"[DONE] {slide_id}")
    _pipeline_running = False


def _json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _safe_copy(src: str | Path, dst: str | Path) -> None:
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() or src.stat().st_size != dst.stat().st_size:
        shutil.copy2(src, dst)


def _import_local_module(module_name: str, alt_filename: str | None = None):
    """Import a backend helper module from BACKEND_DIR and reload it every time.

    During development we often replace auto_orientation.py/register_fine.py while
    the server is being restarted repeatedly. Loading from the exact file path
    avoids accidentally using a stale module already present in sys.modules.
    """
    filename = f"{module_name}.py"
    mod_path = BACKEND_DIR / filename
    if not mod_path.exists() and alt_filename:
        mod_path = BACKEND_DIR / alt_filename
    if not mod_path.exists():
        # Last fallback: normal import. This is mainly for installed modules.
        mod = importlib.import_module(module_name)
        try:
            return importlib.reload(mod)
        except Exception:
            return mod

    unique_name = f"_pathogene_{module_name}_{int(mod_path.stat().st_mtime_ns)}"
    spec = importlib.util.spec_from_file_location(unique_name, str(mod_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod




def _find_python() -> str:
    """Find a real Python interpreter for helper scripts.

    In a PyInstaller build, sys.executable is Pathogene.exe, not python.exe.
    Using sys.executable would recursively launch the app and open another
    browser window instead of running auto_orientation.py/register_fine.py.
    """
    override = os.environ.get("PATHOGENE_PYTHON", "").strip()
    if override:
        p = Path(override)
        if p.exists():
            return str(p)
        _log(f"[WARN] PATHOGENE_PYTHON is set but does not exist: {override}")

    # Development mode: normal Python execution.
    if not getattr(sys, "frozen", False):
        return sys.executable

    candidates: list[Path] = []

    # Packaged exe example:
    #   E:\Pathogene\dist\Pathogene\Pathogene.exe
    # Common real Python location created by build.bat:
    #   E:\Pathogene\.venv-build\Scripts\python.exe
    try:
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            exe_dir.parent.parent / ".venv-build" / "Scripts" / "python.exe",
            exe_dir.parent / ".venv-build" / "Scripts" / "python.exe",
            exe_dir / ".venv-build" / "Scripts" / "python.exe",
            exe_dir / "python.exe",
            exe_dir / "_internal" / "python.exe",
        ])
    except Exception:
        pass

    for c in candidates:
        try:
            if c.exists():
                return str(c)
        except Exception:
            pass

    for name in ("python.exe", "python", "py.exe", "py"):
        found = shutil.which(name)
        if found:
            return found

    raise RuntimeError(
        "Could not find a real Python interpreter for helper scripts. "
        "Set PATHOGENE_PYTHON to a real python.exe, for example "
        r"E:\Pathogene\.venv-build\Scripts\python.exe"
    )


def _run_python_script(script_name: str, args: list[str]) -> None:
    """Run a backend helper script as a blocking subprocess and stream logs."""
    script_path = BACKEND_DIR / script_name
    if not script_path.exists():
        raise RuntimeError(f"Required script not found: {script_path}")

    python_exe = _find_python()

    # Safety guard: in frozen mode, never use Pathogene.exe as the helper runner.
    if getattr(sys, "frozen", False):
        try:
            if Path(python_exe).resolve() == Path(sys.executable).resolve():
                raise RuntimeError(
                    f"Refusing to run helper script with packaged exe: {python_exe}. "
                    "This would recursively launch Pathogene. Check PATHOGENE_PYTHON."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

    cmd = [python_exe, str(script_path)] + [str(a) for a in args]
    _log("[PYTHON] " + python_exe)
    _log("[CMD] " + " ".join(cmd))

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(BACKEND_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    proc = subprocess.Popen(
        cmd,
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    assert proc.stdout is not None
    for line in proc.stdout:
        _log(line.rstrip())

    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"{script_name} failed with exit code {code}")

def _read_transform_payload(slide_id: str) -> Dict[str, Any]:
    """Read registration transform for preview.

    Priority is intentionally strict:
      1) transform_registered.json  = register_fine.py final result
      2) transform.json             = auto_orientation.py fallback only

    The preview should represent the final fine-registration result whenever it
    exists. Exact warp_matrix values are not used for preview because
    register_fine.py refines rotation/flip/scale/dx/dy and may inherit stale
    warp fields from earlier anchor experiments.
    """
    for fname in ["transform_registered.json", "transform.json"]:
        tf_file = COSMX_TILES_DIR / slide_id / fname
        if tf_file.exists():
            try:
                data = json.loads(tf_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data["_transform_source_file"] = fname
                    data["_is_fine_registered"] = (fname == "transform_registered.json")
                return data
            except Exception as e:
                return {"transform_error": str(e), "_transform_source_file": fname}
    return {}



def _normalize_preview_transform(tf_payload: Dict[str, Any], he_thumb_w: int = 0, he_thumb_h: int = 0) -> Dict[str, Any]:
    """Normalize transform fields so the frontend preview can render reliably."""
    raw_tf = tf_payload.get("transform", {}) if isinstance(tf_payload, dict) else {}
    if not isinstance(raw_tf, dict):
        raw_tf = {}

    def num(*keys, default=0.0):
        for k in keys:
            v = raw_tf.get(k, None)
            if v is None and isinstance(tf_payload, dict):
                v = tf_payload.get(k, None)
            if v is None or v == "":
                continue
            try:
                return float(v)
            except Exception:
                continue
        return float(default)

    tx = raw_tf.get("translateX_pixels", None)
    ty = raw_tf.get("translateY_pixels", None)

    try:
        tx = float(tx)
    except Exception:
        # Older transform JSON may store normalized translateX.
        try:
            tx = float(raw_tf.get("translateX", 0.0)) * float(he_thumb_w or 1024)
        except Exception:
            tx = num("dx", "offsetX", default=0.0)

    try:
        ty = float(ty)
    except Exception:
        # Older transform JSON may store normalized translateY.
        try:
            ty = float(raw_tf.get("translateY", 0.0)) * float(he_thumb_h or 1024)
        except Exception:
            ty = num("dy", "offsetY", default=0.0)

    tf = {
        "rotation": int(round(num("rotation", default=0.0))) % 360,
        "flipX": bool(raw_tf.get("flipX", False)),
        "flipY": bool(raw_tf.get("flipY", False)),
        "scale": num("scale", default=1.0),
        "translateX_pixels": round(tx, 4),
        "translateY_pixels": round(ty, 4),
    }

    # IMPORTANT: Do not pass warp_matrix to the preview.
    # Preview must show register_fine.py's final rotation/flip/scale/dx/dy.
    # Old anchor/SIFT warp matrices can override the fine result and make the
    # green CosMx overlay disappear or appear in the wrong place.

    if isinstance(tf_payload, dict) and tf_payload.get("rotation_exact") is not None:
        try:
            tf["rotation_exact"] = float(tf_payload.get("rotation_exact"))
        except Exception:
            pass

    return tf

def _find_slide_file(slide_id: str) -> Optional[Path]:
    for ext in (".svs", ".tif", ".tiff", ".ndpi", ".scn"):
        p = SLIDES_DIR / f"{slide_id}{ext}"
        if p.exists():
            return p
    return None



COSMX_EXTENSIONS = (".ome.tif", ".ome.tiff", ".tif", ".tiff", ".png", ".jpg", ".jpeg")

def _cosmx_extension(path: str | Path) -> str:
    """Return a supported CosMx extension while preserving compound OME-TIFF suffixes."""
    name = Path(path).name.lower()
    for ext in COSMX_EXTENSIONS:
        if name.endswith(ext):
            return ext
    return Path(path).suffix.lower()

def _find_cosmx_file(slide_id: str) -> Optional[Path]:
    """Find the copied CosMx image for slide_id regardless of supported image format."""
    sid = slide_id.lower()
    for ext in COSMX_EXTENSIONS:
        p = COSMX_DIR / f"{slide_id}{ext}"
        if p.exists():
            return p
    for p in COSMX_DIR.iterdir() if COSMX_DIR.exists() else []:
        if not p.is_file():
            continue
        lower = p.name.lower()
        for ext in COSMX_EXTENSIONS:
            if lower.endswith(ext) and lower[:-len(ext)] == sid:
                return p
    return None

def _copy_cosmx_for_slide(src: str | Path, slide_id: str) -> Path:
    """Copy CosMx input without disguising TIFF/OME-TIFF data as PNG."""
    src = Path(src)
    ext = _cosmx_extension(src)
    if ext not in COSMX_EXTENSIONS:
        raise ValueError(f"Unsupported CosMx image format: {src.name}")
    # Remove stale copies for this slide so downstream discovery is deterministic.
    for old_ext in COSMX_EXTENSIONS:
        old = COSMX_DIR / f"{slide_id}{old_ext}"
        if old.exists():
            try:
                old.unlink()
            except Exception:
                pass
    dst = COSMX_DIR / f"{slide_id}{ext}"
    _safe_copy(src, dst)
    _log(f"[Input] CosMx preserved as {dst.name}")
    return dst

def _get_image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(str(path)) as img:
            return img.size
    except Exception:
        try:
            import cv2
            img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if img is not None:
                return img.shape[1], img.shape[0]
        except Exception:
            pass
    return 0, 0


def _make_he_thumbnail(slide_id: str, svs_path: str | Path | None = None, max_size: int = 1024) -> bool:
    """Create H&E thumbnail.jpg and thumbnail_scale.json.

    Important: SVS files are NOT copied into data/slides.
    The selected local SVS path is used directly so large slide files do not get
    duplicated under E:\\병리\\data\\slides.
    """
    svs = Path(svs_path) if svs_path else _find_slide_file(slide_id)
    if svs is None or not svs.exists():
        _log(f"[ERROR] H&E file not found for {slide_id}: {svs_path or ''}")
        return False

    out_dir = TILES_DIR / slide_id
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from openslide import OpenSlide
        slide = OpenSlide(str(svs))
        slide_w, slide_h = slide.dimensions
        thumb = slide.get_thumbnail((max_size, max_size))
        thumb_w, thumb_h = thumb.size
        thumb.save(str(out_dir / "thumbnail.jpg"), "JPEG", quality=85)
        slide.close()
        (out_dir / "thumbnail_scale.json").write_text(
            json.dumps({
                "thumb_w": thumb_w,
                "thumb_h": thumb_h,
                "slide_w": slide_w,
                "slide_h": slide_h,
                "source_svs_path": str(svs),
            }, indent=2),
            encoding="utf-8",
        )
        _log(f"[Thumb] H&E thumbnail ready: {thumb_w}x{thumb_h}")
        _log(f"[Thumb] H&E source path: {svs}")
        return True
    except Exception as e:
        _log(f"[ERROR] H&E thumbnail failed: {e}")
        return False


def _make_cosmx_thumbnail(cosmx_path: Path, cosmx_thumb: Path, max_size: int = 1024, force: bool = False) -> tuple[int, int]:
    """Create a small CosMx preview without requiring the source to be PNG.

    For large TIFF/OME-TIFF files, try OpenSlide/pyvips first so an existing
    pyramid/overview can be used instead of decoding the full level-0 image.
    """
    if force and cosmx_thumb.exists():
        try:
            cosmx_thumb.unlink()
        except Exception:
            pass

    if cosmx_thumb.exists():
        return _get_image_size(cosmx_path)

    # 1) OpenSlide is efficient for pyramidal whole-slide TIFFs when supported.
    try:
        from openslide import OpenSlide
        slide = OpenSlide(str(cosmx_path))
        orig_w, orig_h = slide.dimensions
        thumb = slide.get_thumbnail((max_size, max_size)).convert("RGB")
        thumb.save(str(cosmx_thumb), "JPEG", quality=85)
        slide.close()
        _log(f"[Thumb] CosMx thumbnail ready by OpenSlide: {orig_w}x{orig_h}")
        return orig_w, orig_h
    except Exception as os_err:
        _log(f"[INFO] OpenSlide CosMx thumbnail unavailable: {os_err}")

    # 2) libvips can efficiently shrink tiled TIFF/OME-TIFF inputs on load.
    try:
        import pyvips
        src = pyvips.Image.new_from_file(str(cosmx_path), access="sequential")
        orig_w, orig_h = int(src.width), int(src.height)
        thumb = pyvips.Image.thumbnail(str(cosmx_path), max_size, height=max_size, size="down")
        try:
            if thumb.hasalpha():
                thumb = thumb.flatten(background=[255, 255, 255])
        except Exception:
            pass
        if thumb.bands == 1:
            thumb = thumb.colourspace("srgb")
        if thumb.bands > 3:
            thumb = thumb.extract_band(0, n=3)
        thumb.jpegsave(str(cosmx_thumb), Q=85)
        _log(f"[Thumb] CosMx thumbnail ready by pyvips: {orig_w}x{orig_h}")
        return orig_w, orig_h
    except Exception as vips_err:
        _log(f"[INFO] pyvips CosMx thumbnail unavailable: {vips_err}")

    # 3) Generic fallback for ordinary PNG/JPEG/TIFF inputs.
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        img = Image.open(str(cosmx_path))
        orig_w, orig_h = img.size
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        img.save(str(cosmx_thumb), "JPEG", quality=85)
        img.close()
        _log(f"[Thumb] CosMx thumbnail ready by PIL: {orig_w}x{orig_h}")
        return orig_w, orig_h
    except Exception as pil_err:
        _log(f"[WARN] PIL CosMx thumbnail failed: {pil_err}")

    return _get_image_size(cosmx_path)



def _make_original_cosmx_dzi(slide_id: str) -> bool:
    """Create original CosMx DZI at cosmx_tiles/<slide_id>/<slide_id>.dzi.

    The viewer uses this original DZI and applies transform_registered.json in
    OpenSeadragon. This avoids sparse registered DZI tile gaps and 404 errors.
    """
    cosmx_png = _find_cosmx_file(slide_id)
    if cosmx_png is None:
        _log(f"[CosMx Original DZI] CosMx image not found for: {slide_id}")
        return False

    out_dir = COSMX_TILES_DIR / slide_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dzi_path = out_dir / f"{slide_id}.dzi"
    files_dir = out_dir / f"{slide_id}_files"

    if dzi_path.exists() and files_dir.exists():
        _log(f"[CosMx Original DZI] Reusing existing: {dzi_path}")
        return True

    try:
        import pyvips
    except Exception as e:
        raise RuntimeError(
            "pyvips is required to create the original CosMx DZI. "
            "Install pyvips/libvips or pre-generate the DZI."
        ) from e

    _log(f"[CosMx Original DZI] Creating original CosMx DZI from: {cosmx_png}")
    img = pyvips.Image.new_from_file(str(cosmx_png), access="sequential")

    # Normalize to RGB/white background so OpenSeadragon gets normal JPEG tiles.
    try:
        if img.hasalpha():
            img = img.flatten(background=[255, 255, 255])
    except Exception:
        pass
    if img.bands == 1:
        img = img.colourspace("srgb")
    if img.bands > 3:
        img = img.extract_band(0, n=3)

    base = out_dir / slide_id
    img.dzsave(
        str(base),
        tile_size=254,
        overlap=1,
        suffix=".jpeg[Q=85]",
    )

    if not dzi_path.exists():
        raise RuntimeError(f"CosMx original DZI generation finished but DZI is missing: {dzi_path}")

    _log(f"[CosMx Original DZI] Done: {dzi_path}")
    return True

def _get_registration_preview(slide_id: str) -> Dict[str, Any]:
    """
    Return everything the frontend needs for the fast registration preview.
    This uses only thumbnails and the transform JSON; it does NOT require DZI tiles.

    Preview convention:
      - H&E thumbnail is drawn in its own thumbnail pixel space.
      - CosMx thumbnail is transformed into that same H&E thumbnail space.
      - Preview uses register_fine.py final rotation/flip/scale/translate fields.
      - warp_matrix is intentionally ignored in preview to avoid overriding fine registration.
    """
    he_thumb = TILES_DIR / slide_id / "thumbnail.jpg"
    if not he_thumb.exists():
        if not _make_he_thumbnail(slide_id):
            return {"error": f"H&E thumbnail failed for {slide_id}"}

    he_thumb_w, he_thumb_h = _get_image_size(he_thumb)
    he_w = he_h = 0
    scale_json = TILES_DIR / slide_id / "thumbnail_scale.json"
    if scale_json.exists():
        try:
            sc = json.loads(scale_json.read_text(encoding="utf-8"))
            he_w = int(sc.get("slide_w", 0) or 0)
            he_h = int(sc.get("slide_h", 0) or 0)
            he_thumb_w = int(sc.get("thumb_w", he_thumb_w) or he_thumb_w)
            he_thumb_h = int(sc.get("thumb_h", he_thumb_h) or he_thumb_h)
        except Exception:
            pass
    if not he_w or not he_h:
        he_w, he_h = he_thumb_w, he_thumb_h

    payload: Dict[str, Any] = {
        "slide_id": slide_id,
        "he_url": f"/tiles/{slide_id}/thumbnail.jpg",
        "he_preview": f"/tiles/{slide_id}/thumbnail.jpg",
        "he_w": he_w,
        "he_h": he_h,
        "he_thumb_w": he_thumb_w,
        "he_thumb_h": he_thumb_h,
        "he_preview_w": he_thumb_w,
        "he_preview_h": he_thumb_h,
        "preview_mode": "qc_falsecolor",
    }

    cosmx_png = _find_cosmx_file(slide_id)
    if cosmx_png is None:
        payload["has_cosmx"] = False
        payload["transform"] = _normalize_preview_transform({}, he_thumb_w, he_thumb_h)
        return payload

    cosmx_thumb = COSMX_DIR / f"{slide_id}_thumb.jpg"
    orig_w, orig_h = _make_cosmx_thumbnail(cosmx_png, cosmx_thumb, force=False)
    cosmx_thumb_w, cosmx_thumb_h = _get_image_size(cosmx_thumb if cosmx_thumb.exists() else cosmx_png)
    cosmx_url = f"/cosmx/{slide_id}_thumb.jpg" if cosmx_thumb.exists() else f"/cosmx/{cosmx_png.name}"

    payload.update({
        "has_cosmx": True,
        "cosmx_url": cosmx_url,
        "cosmx_preview": cosmx_url,
        "cosmx_orig_w": orig_w,
        "cosmx_orig_h": orig_h,
        "cosmx_w": orig_w,
        "cosmx_h": orig_h,
        "cosmx_thumb_w": cosmx_thumb_w,
        "cosmx_thumb_h": cosmx_thumb_h,
        "cosmx_preview_w": cosmx_thumb_w,
        "cosmx_preview_h": cosmx_thumb_h,
    })

    tf_payload = _read_transform_payload(slide_id)
    if tf_payload:
        # Keep non-transform diagnostic fields, but never expose warp_matrix to
        # the preview. The preview must reflect register_fine.py's final
        # rotation/flip/scale/dx/dy.
        payload.update(tf_payload)
        for stale_warp_key in ["warp_matrix", "affine_matrix", "matrix"]:
            payload.pop(stale_warp_key, None)

        if "original_sizes" in tf_payload:
            try:
                payload["he_w"], payload["he_h"] = tf_payload["original_sizes"].get("he", [he_w, he_h])
                payload["cosmx_orig_w"], payload["cosmx_orig_h"] = tf_payload["original_sizes"].get("cosmx", [orig_w, orig_h])
            except Exception:
                pass

        payload["transform"] = _normalize_preview_transform(tf_payload, he_thumb_w, he_thumb_h)
        payload["transform_source"] = tf_payload.get("_transform_source_file", "unknown")
        payload["is_fine_registered_preview"] = bool(tf_payload.get("_is_fine_registered", False))
        if not payload["is_fine_registered_preview"]:
            payload["registration_warning"] = (payload.get("registration_warning") or "Fine registration result was not found; preview is using auto_orientation fallback transform.json.")
    else:
        payload["transform"] = _normalize_preview_transform({}, he_thumb_w, he_thumb_h)
        payload["transform_source"] = "identity fallback"

    # If make_cosmx_dzi/register_fine produced a diagnostic overlay image, expose it.
    overlay_candidates = [
        COSMX_TILES_DIR / slide_id / f"{slide_id}_registered_overlay.png",
        COSMX_TILES_DIR / slide_id / "registered_overlay.png",
    ]
    for ov in overlay_candidates:
        if ov.exists():
            payload["diagnostic_overlay_url"] = f"/cosmx_tiles/{slide_id}/{ov.name}"
            break

    return payload

# =============================================================================
# FRONTEND ROUTES
# =============================================================================

@app.route("/")
def index():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return f"index.html not found: {index_file}<br>Expected frontend folder: {FRONTEND_DIR}", 404
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def frontend_file(filename):
    target = FRONTEND_DIR / filename
    if target.exists() and target.is_file():
        return send_from_directory(FRONTEND_DIR, filename)
    return _json_error(f"Not found: {filename}", 404)


@app.after_request
def after_request(resp: Response):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp

# =============================================================================
# TILE SERVING ENDPOINTS
# =============================================================================

@app.route("/tiles/<path:filepath>")
def serve_tiles(filepath):
    return send_from_directory(TILES_DIR, filepath)


@app.route("/cosmx_tiles/<path:filepath>")
def serve_cosmx_tiles(filepath):
    return send_from_directory(COSMX_TILES_DIR, filepath)


@app.route("/cosmx/<path:filepath>")
def serve_cosmx(filepath):
    return send_from_directory(COSMX_DIR, filepath)

# =============================================================================
# SLIDE / COSMX INFO ENDPOINTS
# =============================================================================

@app.route("/api/slides", methods=["GET"])
def list_slides():
    slides = []
    for slide_dir in sorted(TILES_DIR.iterdir()):
        if slide_dir.is_dir():
            dzi_file = slide_dir / f"{slide_dir.name}.dzi"
            if dzi_file.exists():
                slides.append({"id": slide_dir.name, "name": slide_dir.name, "dzi_url": f"/tiles/{slide_dir.name}/{slide_dir.name}.dzi"})
    return jsonify(slides)



@app.route("/api/cosmx/<slide_id>/dzi", methods=["GET"])
def get_cosmx_dzi(slide_id):
    """Return the CosMx DZI used by the viewer.

    Important:
    The viewer should use the original CosMx DZI and apply
    transform_registered.json at display time. The generated registered DZI can
    be sparse/incomplete when top levels or empty tiles are skipped, which causes
    OpenSeadragon 404 tile errors. Therefore original DZI is preferred here.
    """
    original_dzi   = COSMX_TILES_DIR / slide_id / f"{slide_id}.dzi"
    registered_dzi = COSMX_TILES_DIR / slide_id / f"{slide_id}_registered.dzi"

    # If this slide was produced by an older run that only generated
    # <slide_id>_registered.dzi, create the original CosMx DZI on demand.
    # This may take a little time on first refresh, but prevents missing sparse
    # registered tiles from breaking the viewer.
    if not original_dzi.exists() and _find_cosmx_file(slide_id) is not None:
        try:
            _make_original_cosmx_dzi(slide_id)
        except Exception as e:
            _log(f"[WARN] Could not create original CosMx DZI on demand: {e}")

    if original_dzi.exists():
        return jsonify({
            "has_cosmx": True,
            "dzi_url": f"/cosmx_tiles/{slide_id}/{slide_id}.dzi",
            "slide_id": slide_id,
            "registered": False,
            "registered_dzi_exists": registered_dzi.exists(),
            "mode": "original_dzi_plus_transform",
        })

    # Fallback only. This keeps older outputs viewable if the original CosMx DZI
    # does not exist, but it is no longer the preferred route.
    if registered_dzi.exists():
        return jsonify({
            "has_cosmx": True,
            "dzi_url": f"/cosmx_tiles/{slide_id}/{slide_id}_registered.dzi",
            "slide_id": slide_id,
            "registered": True,
            "mode": "registered_dzi_fallback",
        })

    return jsonify({"has_cosmx": False, "slide_id": slide_id})

@app.route("/api/cosmx/<slide_id>/transform", methods=["GET"])
def get_cosmx_transform(slide_id):
    """Return the registration transform used by the viewer.

    Priority:
      1. transform_registered.json from register_fine.py
      2. transform.json from auto_orientation.py

    Do not return identity just because a registered DZI exists. In the current
    viewer path, the original CosMx DZI is shown and this transform is applied
    in OpenSeadragon.
    """
    for fname in ["transform_registered.json", "transform.json"]:
        tf_file = COSMX_TILES_DIR / slide_id / fname
        if tf_file.exists():
            data = json.loads(tf_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["_transform_source_file"] = fname
                data["_is_fine_registered"] = (fname == "transform_registered.json")
            return jsonify(data)

    return jsonify({
        "version": "1.0",
        "slide_id": slide_id,
        "transform": "identity",
        "notes": "No transform file found",
    })


# =============================================================================
# LOCAL FILE DIALOG ENDPOINTS
# =============================================================================

@app.route("/api/dialog/svs", methods=["GET"])
def open_svs_dialog():
    """
    Open a native Windows file dialog on the server PC and return the selected
    H&E slide path.

    This endpoint is intended for localhost/demo usage where the Flask server
    and browser run on the same Windows machine.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        path = filedialog.askopenfilename(
            title="Select H&E slide",
            filetypes=[
                ("Whole Slide Images", "*.svs *.tif *.tiff *.ndpi *.scn"),
                ("All Files", "*.*"),
            ],
        )

        root.destroy()
        return jsonify({"path": path or None})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dialog/cosmx", methods=["GET"])
def open_cosmx_dialog():
    """
    Open a native Windows file dialog on the server PC and return the selected
    CosMx image path.

    This endpoint is intended for localhost/demo usage where the Flask server
    and browser run on the same Windows machine.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        path = filedialog.askopenfilename(
            title="Select CosMx image",
            filetypes=[
                ("CosMx Images", "*.ome.tif *.ome.tiff *.tif *.tiff *.png *.jpg *.jpeg"),
                ("OME-TIFF", "*.ome.tif *.ome.tiff"),
                ("Image Files", "*.png *.jpg *.jpeg *.tif *.tiff"),
                ("All Files", "*.*"),
            ],
        )

        root.destroy()
        return jsonify({"path": path or None})

    except Exception as e:
        return jsonify({"error": str(e)}), 500



# =============================================================================
# LOCAL BROWSER FILE UPLOAD ENDPOINTS
# =============================================================================

def _save_uploaded_file(field_name: str, allowed_exts: set[str], subdir: str):
    """
    Save a browser-selected file to data/uploads/<subdir>/ and return its server path.
    This avoids tkinter browser/backend dialog issues and feels like a normal web app.
    """
    if field_name not in request.files:
        return None, ("No file field named '%s'." % field_name, 400)

    f = request.files[field_name]
    if not f or not f.filename:
        return None, ("No file selected.", 400)

    original_name = Path(f.filename).name
    ext = Path(original_name).suffix.lower()

    if ext not in allowed_exts:
        return None, (f"Unsupported file extension: {ext}", 400)

    safe_name = secure_filename(original_name)
    if not safe_name:
        safe_name = "uploaded" + ext

    out_dir = UPLOADS_DIR / subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    dest = out_dir / safe_name
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        i = 1
        while True:
            candidate = out_dir / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1

    f.save(str(dest))

    return {
        "path": str(dest),
        "filename": original_name,
        "saved_name": dest.name,
        "stem": dest.stem,
        "size": dest.stat().st_size,
    }, None


@app.route("/api/upload/svs", methods=["POST"])
def upload_svs():
    data, err = _save_uploaded_file(
        "file",
        {".svs", ".tif", ".tiff", ".ndpi", ".scn"},
        "slides",
    )
    if err:
        msg, code = err
        return jsonify({"error": msg}), code
    return jsonify(data)


@app.route("/api/upload/cosmx", methods=["POST"])
def upload_cosmx():
    data, err = _save_uploaded_file(
        "file",
        {".png", ".jpg", ".jpeg", ".tif", ".tiff"},
        "cosmx",
    )
    if err:
        msg, code = err
        return jsonify({"error": msg}), code
    return jsonify(data)



def _run_fine_registration(slide_id: str, svs_path: str | Path, transform_file: str = "transform.json") -> None:
    """Run register_fine.py as a blocking subprocess.

    Do not import register_fine.py directly here. The original scripts rewrap
    stdout on Windows, and importing them into Flask can make fetch connections
    unstable. Subprocess execution preserves the old command-line behavior.
    """
    if not svs_path or not Path(svs_path).exists():
        raise RuntimeError(f"Fine registration needs a valid SVS path: {svs_path}")

    _log(f"[Fine] Starting fine registration from {transform_file}")
    _run_python_script("register_fine.py", [
        "--slide-id", slide_id,
        "--data-dir", str(DATA_DIR),
        "--size", "1024",
        "--svs-path", str(svs_path),
        "--transform-file", transform_file,
    ])
    _log("[Fine] Fine registration complete")


def _write_native_coordinate_transform(slide_id: str, he_w: int, he_h: int,
                                       cosmx_w: int, cosmx_h: int,
                                       processing_size: int = 1024) -> Path:
    """Write a no-registration transform for already-registered image pairs.

    The viewer's transform convention is defined in independently resized
    registration-thumbnail coordinates.  A native-coordinate identity therefore
    needs a scale correction when the H&E and CosMx canvases have different
    aspect ratios/sizes.  This correction makes the final full-resolution scale
    exactly 1.0 with zero translation/rotation, so CosMx pixel (x, y) is placed
    at H&E pixel (x, y) without running registration.
    """
    he_w = max(1, int(he_w))
    he_h = max(1, int(he_h))
    cosmx_w = max(1, int(cosmx_w))
    cosmx_h = max(1, int(cosmx_h))
    proc = max(1, int(processing_size))

    he_thumb_scale = min(proc / he_w, proc / he_h)
    cosmx_thumb_scale = min(proc / cosmx_w, proc / cosmx_h)
    preview_scale = he_thumb_scale / cosmx_thumb_scale if cosmx_thumb_scale else 1.0

    tf_dir = COSMX_TILES_DIR / slide_id
    tf_dir.mkdir(parents=True, exist_ok=True)
    out = tf_dir / "transform_registered.json"
    payload = {
        "version": "1.0_native_coordinates",
        "slide_id": slide_id,
        "method": "already_registered_tiling_only",
        "original_sizes": {
            "he": [he_w, he_h],
            "cosmx": [cosmx_w, cosmx_h],
            "size_ratio": (cosmx_w / he_w + cosmx_h / he_h) / 2.0,
        },
        "transform": {
            "rotation": 0,
            "flipX": False,
            "flipY": False,
            "translateX": 0.0,
            "translateY": 0.0,
            "translateX_pixels": 0.0,
            "translateY_pixels": 0.0,
            "scale": preview_scale,
        },
        "detection": {
            "processing_size": proc,
        },
        "registration": {
            "skipped": True,
            "reason": "Input pair marked as already registered; preserving native pixel coordinates.",
        },
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _log(
        f"[Tiling Only] Native-coordinate transform saved: scale={preview_scale:.6f}, "
        f"H&E={he_w}x{he_h}, CosMx={cosmx_w}x{cosmx_h}"
    )
    return out

# =============================================================================
# PREVIEW-FIRST PIPELINE ENDPOINTS
# =============================================================================

@app.route("/api/pipeline/run", methods=["POST"])
def pipeline_run_preview():
    """
    Phase 1 only:
    - copy inputs into data/
    - create H&E and CosMx thumbnails
    - run auto_orientation + register_fine when CosMx exists and mode='auto'
    - for mode='tiling-only', preserve native coordinates and skip registration
    - return preview payload for the canvas overlay screen
    """
    global _preview_context

    data = request.get_json(force=True, silent=True) or {}
    svs_path = data.get("svs_path", "")
    cosmx_path = data.get("cosmx_path", "")
    slide_id = data.get("slide_id") or (Path(svs_path).stem if svs_path else "")
    transform_type = data.get("transform_type", "affine")
    mode = data.get("mode", "auto")
    if mode == "semi-auto":
        mode = "manual"

    if not svs_path or not Path(svs_path).exists():
        return _json_error(f"SVS file not found: {svs_path}", 400)
    if cosmx_path and not Path(cosmx_path).exists():
        return _json_error(f"CosMx file not found: {cosmx_path}", 400)

    has_cosmx = bool(cosmx_path and Path(cosmx_path).exists())

    try:
        # Do NOT copy the SVS into data/slides. SVS files can be several GB,
        # so we keep only the selected local path and pass it directly to
        # OpenSlide / make_dzi later.
        svs_src = Path(svs_path)

        if has_cosmx:
            # The downstream code expects data/cosmx/<slide_id>.png.
            # CosMx images are usually much smaller than SVS, so copying/normalizing
            # only this file keeps make_cosmx_dzi.py and register_anchors.py compatible.
            _copy_cosmx_for_slide(cosmx_path, slide_id)

            # Avoid stale previews from an earlier failed/experimental run.
            # The current run must create transform.json via auto_orientation.py
            # and transform_registered.json via register_fine.py again.
            tf_dir = COSMX_TILES_DIR / slide_id
            for stale in [tf_dir / "transform_registered.json", tf_dir / "transform.json"]:
                try:
                    if stale.exists():
                        stale.unlink()
                        _log(f"[Clean] Removed stale {stale.name}")
                except Exception as e:
                    _log(f"[WARN] Could not remove stale transform {stale}: {e}")

        if has_cosmx and mode == "auto":
            phase1_steps = ["Preview thumbnails", "Auto orientation", "Fine registration"]
        elif has_cosmx and mode == "tiling-only":
            phase1_steps = ["Preview thumbnails", "Preserve native coordinates"]
        else:
            phase1_steps = ["Preview thumbnails"]
        _reset_status(slide_id, phase1_steps)
        _set_step(1, "Preview thumbnails")

        if not _make_he_thumbnail(slide_id, svs_path=svs_src):
            raise RuntimeError("H&E thumbnail generation failed")
        cosmx_orig_w = cosmx_orig_h = 0
        if has_cosmx:
            cosmx_orig_w, cosmx_orig_h = _make_cosmx_thumbnail(
                _find_cosmx_file(slide_id), COSMX_DIR / f"{slide_id}_thumb.jpg", force=True
            )

        # Already-registered / tiling-only path. Do not estimate a new transform.
        # Instead, preserve native full-resolution pixel coordinates so the pair
        # is tiled and displayed exactly as supplied.
        if has_cosmx and mode == "tiling-only":
            _set_step(2, "Preserve native coordinates")
            scale_json = TILES_DIR / slide_id / "thumbnail_scale.json"
            if not scale_json.exists():
                raise RuntimeError(f"H&E thumbnail scale metadata not found: {scale_json}")
            sc = json.loads(scale_json.read_text(encoding="utf-8"))
            he_orig_w = int(sc.get("slide_w", 0) or 0)
            he_orig_h = int(sc.get("slide_h", 0) or 0)
            if not he_orig_w or not he_orig_h or not cosmx_orig_w or not cosmx_orig_h:
                raise RuntimeError(
                    "Could not determine full-resolution H&E/CosMx dimensions for tiling-only mode"
                )
            _write_native_coordinate_transform(
                slide_id, he_orig_w, he_orig_h, cosmx_orig_w, cosmx_orig_h, processing_size=1024
            )

        # Automatic registration is the primary path. Do NOT return preview until
        # auto_orientation.py and register_fine.py have both completed and the
        # final transform_registered.json exists. Otherwise the frontend will
        # render an identity fallback and make the alignment look broken.
        if has_cosmx and mode == "auto":
            # 1) auto_orientation.py -> transform.json
            _set_step(2, "Auto orientation")
            _run_python_script("auto_orientation.py", [
                "--slide-id", slide_id,
                "--data-dir", str(DATA_DIR),
                "--mode", "auto",
                "--refine",
                "--size", "1024",
                "--svs-path", str(svs_src),
            ])

            transform_json = COSMX_TILES_DIR / slide_id / "transform.json"
            if not transform_json.exists():
                raise RuntimeError(f"auto_orientation finished but transform.json was not created: {transform_json}")
            _log(f"[OK] Auto orientation output found: {transform_json}")

            # 2) register_fine.py -> transform_registered.json
            _set_step(3, "Fine registration")
            _run_fine_registration(slide_id, svs_src, transform_file="transform.json")

            final_json = COSMX_TILES_DIR / slide_id / "transform_registered.json"
            if not final_json.exists():
                raise RuntimeError(f"register_fine finished but transform_registered.json was not created: {final_json}")
            _log(f"[OK] Fine registration output found: {final_json}")

        preview = _get_registration_preview(slide_id)
        if has_cosmx and mode == "tiling-only":
            preview["registration_skipped"] = True
            preview["registration_mode"] = "tiling-only"
            preview["registration_warning"] = "Registration skipped. Preview preserves the input files' native pixel coordinates."
        if has_cosmx and mode == "auto" and not preview.get("is_fine_registered_preview"):
            raise RuntimeError(
                "Preview refused to use fallback transform. "
                "Expected transform_registered.json from register_fine.py, "
                f"but preview source was: {preview.get('transform_source')}"
            )

        _preview_context = {
            "slide_id": slide_id,
            "svs_path": str(svs_src),
            "has_cosmx": has_cosmx,
            "transform_type": transform_type,
            "mode": mode,
        }

        _pipeline_status["running"] = False
        _pipeline_status["done"] = False
        _pipeline_status["need_preview"] = False
        _pipeline_status["preview_payload"] = preview

        return jsonify({
            "status": "preview_ready",
            "slide_id": slide_id,
            "preview_payload": preview,
            "preview": preview,
        })

    except Exception as e:
        _pipeline_status["running"] = False
        _pipeline_status["error"] = str(e)
        return _json_error(f"Preview failed: {e}", 500)


@app.route("/api/preview", methods=["POST"])
def preview_alias_for_old_frontend():
    """Backward-compatible alias so old POST /api/preview calls do not produce 405."""
    return pipeline_run_preview()


@app.route("/api/pipeline/confirm", methods=["POST"])
def pipeline_confirm():
    """
    Decision after preview.
    action='approve' -> start Phase 2 full DZI tiling.
    action='anchors' -> open anchor screen first, then return to preview after manual registration.
    """
    global _pipeline_running

    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action", "approve")
    if not _preview_context.get("slide_id"):
        return _json_error("No preview context found. Run /api/pipeline/run first.", 400)

    slide_id = _preview_context["slide_id"]
    has_cosmx = bool(_preview_context.get("has_cosmx"))
    transform_type = _preview_context.get("transform_type", "affine")

    if action == "anchors":
        preview = _get_registration_preview(slide_id)
        preview.update({
            "slide_id": slide_id,
            "transform_type": transform_type,
            "step": 1,
            "total": 1,
            "anchor_coordinate_space": "thumbnail",
        })
        _reset_status(slide_id, ["Anchor Placement"])
        _pipeline_status["running"] = True
        _pipeline_status["need_anchors"] = True
        _pipeline_status["anchor_payload"] = preview
        _pipeline_status["need_preview"] = False
        _pipeline_status["preview_payload"] = None
        _log("[WAIT] Waiting for manual anchors from frontend...")
        return jsonify({"status": "need_anchors", "slide_id": slide_id})

    with _pipeline_lock:
        if _pipeline_running:
            return _json_error("Pipeline already running", 409)
        _pipeline_running = True

    steps = ["H&E Tiling"] + (["CosMx Tiling"] if has_cosmx else [])
    _reset_status(slide_id, steps)
    threading.Thread(target=_run_phase2_tiling_thread, args=(slide_id,), daemon=True).start()
    return jsonify({"status": "started", "slide_id": slide_id, "steps": steps})


def _run_phase2_tiling_thread(slide_id: str):
    try:
        svs_path = _preview_context.get("svs_path") or str(_find_slide_file(slide_id) or "")
        if not svs_path or not Path(svs_path).exists():
            raise RuntimeError(f"H&E file not found for tiling: {svs_path}")

        _set_step(1, "H&E Tiling")
        make_dzi = _import_local_module("make_dzi")
        make_dzi.run(svs_path=svs_path, out_dir=str(TILES_DIR), log_cb=_log)

        if _preview_context.get("has_cosmx"):
            _set_step(2, "CosMx Original Tiling")
            _make_original_cosmx_dzi(slide_id)

            # Registered sparse DZI generation is intentionally not required for
            # the viewer anymore. The viewer uses original DZI +
            # transform_registered.json. This avoids skipped high-resolution
            # levels / sparse-tile 404s and is faster to debug.

    except Exception as e:
        _fail("Phase 2 tiling", e)
        return

    _finish(slide_id)


@app.route("/api/pipeline/anchors", methods=["POST"])
def pipeline_submit_anchors():
    """Manual anchor registration endpoint used by the current index.html."""
    global _pipeline_running

    data = request.get_json(force=True, silent=True) or {}
    slide_id = data.get("slide_id") or _preview_context.get("slide_id")
    transform_type = data.get("transform_type") or _preview_context.get("transform_type", "affine")
    anchors = data.get("anchors", {})

    if not slide_id:
        return _json_error("slide_id is required", 400)
    src = anchors.get("src", [])
    dst = anchors.get("dst", [])
    if len(src) != len(dst):
        return _json_error("src and dst anchor counts do not match", 400)
    orientation = anchors.get("orientation") or {}
    oriented_anchor_mode = orientation.get("coord_space") == "oriented_cosmx_thumbnail"
    # When the user manually rotates/flips CosMx before placing anchors, the
    # backend solves the remaining scale+translation. Two pairs are enough, but
    # the UI still encourages four or more for stability.
    min_needed = 2 if oriented_anchor_mode else (4 if transform_type == "affine" else 2)
    if len(src) < min_needed:
        return _json_error(f"Not enough anchors for {transform_type}. Need at least {min_needed}.", 400)
    if oriented_anchor_mode:
        _log(f"[Anchor] Orientation from UI: R={orientation.get('rotation', 0)} FX={orientation.get('flipX', False)} FY={orientation.get('flipY', False)}")

    with _pipeline_lock:
        if _pipeline_running:
            return _json_error("Pipeline already running", 409)
        _pipeline_running = True

    _reset_status(slide_id, ["Manual Registration", "Fine Registration"])
    _pipeline_status["need_anchors"] = False
    _pipeline_status["anchor_payload"] = None
    threading.Thread(
        target=_run_manual_anchor_registration_thread,
        args=(slide_id, transform_type, anchors),
        daemon=True,
    ).start()
    return jsonify({"status": "started", "slide_id": slide_id, "pairs": len(src)})


def _run_manual_anchor_registration_thread(slide_id: str, transform_type: str, anchors: Dict[str, Any]):
    global _pipeline_running
    try:
        _set_step(1, f"Manual Registration ({transform_type}, {len(anchors.get('src', []))} pairs)")
        register_anchors = _import_local_module("register_anchors")
        register_anchors.run(
            slide_id=slide_id,
            data_dir=str(DATA_DIR),
            transform_type=transform_type,
            mode="manual",
            anchors=anchors,
            log_cb=_log,
        )
        _set_step(2, "Fine Registration")
        svs_path = _preview_context.get("svs_path") or str(_find_slide_file(slide_id) or "")
        _run_fine_registration(slide_id, svs_path, transform_file="latest")
        preview = _get_registration_preview(slide_id)
        preview.update({"slide_id": slide_id, "transform_type": transform_type})
        _pipeline_status["running"] = False
        _pipeline_status["done"] = False
        _pipeline_status["need_preview"] = True
        _pipeline_status["preview_payload"] = preview
        _pipeline_status["need_anchors"] = False
        _pipeline_status["anchor_payload"] = None
        _log("[PREVIEW] Manual registration complete. Returning to preview.")
    except Exception as e:
        _fail("Manual Registration", e)
        return
    finally:
        _pipeline_running = False


# =============================================================================
# PIPELINE ENDPOINTS
# =============================================================================

@app.route("/api/pipeline/start", methods=["POST"])
def start_pipeline():
    global _pipeline_running
    data = request.get_json(force=True, silent=True) or {}
    svs_path = data.get("svs_path", "")
    cosmx_path = data.get("cosmx_path", "")
    slide_id = data.get("slide_id") or (Path(svs_path).stem if svs_path else "")
    transform_type = data.get("transform_type", "affine")
    mode = data.get("mode", "auto")

    if not svs_path or not Path(svs_path).exists():
        return _json_error(f"SVS file not found: {svs_path}", 400)
    if cosmx_path and not Path(cosmx_path).exists():
        return _json_error(f"CosMx file not found: {cosmx_path}", 400)

    with _pipeline_lock:
        if _pipeline_running:
            return _json_error("Pipeline already running", 409)
        _pipeline_running = True

    has_cosmx = bool(cosmx_path and Path(cosmx_path).exists())
    if has_cosmx:
        steps = ["H&E Tiling", "Auto Registration", "CosMx Tiling"] if mode == "auto" else ["H&E Tiling", "Anchor Placement", "Registration", "CosMx Tiling"]
    else:
        steps = ["H&E Tiling"]

    _reset_status(slide_id, steps)
    _anchor_event.clear()

    try:
        # Legacy endpoint kept for compatibility. Do NOT copy SVS into data/slides;
        # use the selected local source path directly.
        svs_src = Path(svs_path)
        if has_cosmx:
            _copy_cosmx_for_slide(cosmx_path, slide_id)
    except Exception as e:
        with _pipeline_lock:
            _pipeline_running = False
        return _json_error(f"Failed to prepare input files: {e}", 500)

    threading.Thread(target=_run_pipeline_thread, args=(slide_id, str(svs_src), has_cosmx, transform_type, mode), daemon=True).start()
    return jsonify({"status": "started", "slide_id": slide_id, "steps": steps})


def _run_pipeline_thread(slide_id: str, svs_path: str, has_cosmx: bool, transform_type: str, mode: str):
    global _pending_anchors
    try:
        _set_step(1, "H&E Tiling")
        import make_dzi
        make_dzi.run(svs_path=svs_path, out_dir=str(TILES_DIR), log_cb=_log)
        if has_cosmx:
            _make_he_thumbnail(slide_id, svs_path=svs_path)
            cosmx_png = _find_cosmx_file(slide_id)
            if cosmx_png is not None:
                w, h = _make_cosmx_thumbnail(cosmx_png, COSMX_DIR / f"{slide_id}_thumb.jpg", force=True)
                _log(f"[Thumb] CosMx thumbnail ready ({w}x{h})")
    except Exception as e:
        _fail("H&E Tiling", e)
        return

    if not has_cosmx:
        _finish(slide_id)
        return

    if mode == "auto":
        try:
            _set_step(2, "Auto Orientation")
            auto_orientation = _import_local_module("auto_orientation")
            auto_orientation.run(slide_id=slide_id, data_dir=str(DATA_DIR), svs_path=svs_path, mode="auto", refine=True, debug=False, size=1024, log_cb=_log)
        except Exception as e:
            _fail("Auto Registration", e)
            return
        cosmx_step = 3
    else:
        try:
            _set_step(2, "Anchor Placement")
            preview = _get_registration_preview(slide_id)
            if "error" in preview:
                raise RuntimeError(preview["error"])
            _pipeline_status["need_anchors"] = True
            _pipeline_status["anchor_payload"] = {**preview, "slide_id": slide_id, "transform_type": transform_type, "step": 2, "total": _pipeline_status.get("total", 4)}
            _log("[WAIT] Waiting for manual anchors from frontend...")
            if not _anchor_event.wait(timeout=7200) or _pending_anchors is None:
                raise RuntimeError("Timed out or cancelled while waiting for anchors.")
            anchors_data = _pending_anchors["anchors"]
            actual_transform_type = _pending_anchors.get("transform_type", transform_type)
            _pending_anchors = None
            _pipeline_status["need_anchors"] = False
            _pipeline_status["anchor_payload"] = None
            n_pairs = len(anchors_data.get("src", []))
            _set_step(3, f"Registration ({actual_transform_type}, {n_pairs} pairs)")
            import register_anchors
            register_anchors.run(slide_id=slide_id, data_dir=str(DATA_DIR), transform_type=actual_transform_type, mode="manual", anchors=anchors_data, log_cb=_log)
        except Exception as e:
            _fail("Registration", e)
            return
        cosmx_step = 4

    try:
        _set_step(cosmx_step, "CosMx Tiling")
        import make_cosmx_dzi
        make_cosmx_dzi.run(slide_id=slide_id, data_dir=str(DATA_DIR), log_cb=_log)
    except Exception as e:
        _fail("CosMx Tiling", e)
        return

    _finish(slide_id)


@app.route("/api/pipeline/status", methods=["GET"])
def pipeline_status():
    return jsonify(_pipeline_status)


@app.route("/api/registration/preview/<slide_id>", methods=["GET"])
def registration_preview(slide_id):
    preview = _get_registration_preview(slide_id)
    if "error" in preview:
        return jsonify(preview), 400
    return jsonify(preview)


@app.route("/api/registration/anchors", methods=["POST"])
def submit_anchors():
    global _pending_anchors
    data = request.get_json(force=True, silent=True) or {}
    slide_id = data.get("slide_id")
    transform_type = data.get("transform_type", "affine")
    anchors = data.get("anchors", {})
    if not slide_id:
        return _json_error("slide_id is required", 400)
    src = anchors.get("src", [])
    dst = anchors.get("dst", [])
    if len(src) != len(dst):
        return _json_error("src and dst anchor counts do not match", 400)
    orientation = anchors.get("orientation") or {}
    oriented_anchor_mode = orientation.get("coord_space") == "oriented_cosmx_thumbnail"
    # When the user manually rotates/flips CosMx before placing anchors, the
    # backend solves the remaining scale+translation. Two pairs are enough, but
    # the UI still encourages four or more for stability.
    min_needed = 2 if oriented_anchor_mode else (4 if transform_type == "affine" else 2)
    if len(src) < min_needed:
        return _json_error(f"Not enough anchors for {transform_type}. Need at least {min_needed}.", 400)
    if oriented_anchor_mode:
        _log(f"[Anchor] Orientation from UI: R={orientation.get('rotation', 0)} FX={orientation.get('flipX', False)} FY={orientation.get('flipY', False)}")
    _pending_anchors = {"slide_id": slide_id, "transform_type": transform_type, "anchors": anchors}
    _anchor_event.set()
    _log(f"[Anchor] Received {len(src)} pairs for {slide_id} ({transform_type})")
    return jsonify({"status": "ok", "pairs": len(src)})


@app.route("/api/registration/cancel", methods=["POST"])
def cancel_registration():
    global _pending_anchors
    _pending_anchors = None
    _pipeline_status["need_anchors"] = False
    _pipeline_status["anchor_payload"] = None
    _anchor_event.set()
    _log("[CANCEL] Anchor placement cancelled.")
    return jsonify({"status": "cancelled"})

# =============================================================================
# ANNOTATION / QC ENDPOINTS
# =============================================================================

@app.route("/api/annotations/<slide_id>", methods=["GET"])
def get_annotations(slide_id):
    annotation_file = ANNOTATIONS_DIR / f"{slide_id}.json"
    if not annotation_file.exists():
        return jsonify({"type": "FeatureCollection", "features": []})
    return jsonify(json.loads(annotation_file.read_text(encoding="utf-8")))


@app.route("/api/annotations/<slide_id>", methods=["POST"])
def save_annotations(slide_id):
    annotation_file = ANNOTATIONS_DIR / f"{slide_id}.json"
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        return _json_error("Invalid GeoJSON format", 400)
    annotation_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return jsonify({"status": "success", "saved": len(data.get("features", []))})


@app.route("/api/annotations/<slide_id>", methods=["DELETE"])
def delete_annotations(slide_id):
    annotation_file = ANNOTATIONS_DIR / f"{slide_id}.json"
    if annotation_file.exists():
        annotation_file.unlink()
        return jsonify({"status": "deleted"})
    return jsonify({"status": "not_found"}), 404



# =============================================================================
# MAGIC WAND ANNOTATION
# =============================================================================

def _resolve_he_source_path(slide_id: str) -> Optional[Path]:
    """Resolve the original H&E file used for a registered/tiled slide.

    Resolution priority:
      1) Current registration session (_preview_context)
      2) Persisted thumbnail_scale.json metadata
      3) Browser-uploaded H&E files under data/uploads/slides
      4) Legacy data/slides/<slide_id>.* location

    This avoids relying on one stale absolute path and keeps Magic Wand working
    for slides registered through the browser upload workflow.
    """

    # 1) Current registration session: this is the most reliable source for a
    # slide that was just registered in the same app session.
    try:
        if _preview_context.get("slide_id") == slide_id:
            src = _preview_context.get("svs_path")
            if src:
                p = Path(src)
                if p.exists():
                    return p
    except Exception:
        pass

    # 2) Persisted metadata written when the H&E thumbnail was created.
    meta = TILES_DIR / slide_id / "thumbnail_scale.json"
    if meta.exists():
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
            src = payload.get("source_svs_path")
            if src:
                p = Path(src)
                if p.exists():
                    return p
        except Exception:
            pass

    # 3) Browser-uploaded H&E files.
    upload_dir = UPLOADS_DIR / "slides"
    if upload_dir.exists():
        supported_exts = (".svs", ".tif", ".tiff", ".ndpi", ".scn")

        # Exact stem match first.
        for ext in supported_exts:
            p = upload_dir / f"{slide_id}{ext}"
            if p.exists():
                return p

        # _save_uploaded_file() appends _1, _2, ... when the same filename
        # already exists. Accept those variants as a fallback.
        try:
            candidates = [
                p for p in upload_dir.iterdir()
                if p.is_file() and p.suffix.lower() in supported_exts
            ]

            exact = [p for p in candidates if p.stem == slide_id]
            if exact:
                return exact[0]

            numbered = [
                p for p in candidates
                if p.stem.startswith(slide_id + "_")
                and p.stem[len(slide_id) + 1:].isdigit()
            ]
            if numbered:
                # Prefer the newest upload because duplicate filenames are
                # versioned as slide_1, slide_2, ...
                numbered.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return numbered[0]
        except Exception:
            pass

    # 4) Legacy copied-slide location.
    return _find_slide_file(slide_id)


def _magic_wand_segment_patch(
    rgb,
    seed_x: int,
    seed_y: int,
    tolerance: float,
):
    """Select the connected tissue region around the clicked seed.

    This runs on a coarse OpenSlide pyramid level, not level 0.  The patch is
    lightly blurred and converted from float RGB to true CIE-L*a*b* before
    computing Euclidean color distance.
    """
    import cv2
    import numpy as np

    if rgb is None or rgb.size == 0:
        raise ValueError("Empty H&E patch")

    h, w = rgb.shape[:2]
    seed_x = int(max(0, min(w - 1, seed_x)))
    seed_y = int(max(0, min(h - 1, seed_y)))

    # Coarse-scale smoothing suppresses nuclei / compression texture so the
    # selection follows tissue-scale color regions rather than individual cells.
    rgb_f = rgb.astype(np.float32) / 255.0
    rgb_f = cv2.GaussianBlur(rgb_f, (0, 0), 2.0)

    # float32 RGB in [0,1] -> true OpenCV CIE-L*a*b* ranges
    # (L* ≈ 0..100; a*/b* approximately centered around zero).
    lab = cv2.cvtColor(rgb_f, cv2.COLOR_RGB2LAB)

    # Robust seed color from a small local neighborhood.
    r = 2
    y0, y1 = max(0, seed_y - r), min(h, seed_y + r + 1)
    x0, x1 = max(0, seed_x - r), min(w, seed_x + r + 1)
    seed_lab = np.median(lab[y0:y1, x0:x1].reshape(-1, 3), axis=0)

    # Reject near-white, low-chroma background and keep it out of the flood fill.
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    tissue = ~((lab[..., 0] > 90.0) & (chroma < 8.0))
    if not bool(tissue[seed_y, seed_x]):
        raise ValueError("Clicked on background. Click inside tissue.")

    dist = np.linalg.norm(lab - seed_lab[None, None, :], axis=2)
    raw = ((dist <= float(tolerance)) & tissue).astype(np.uint8)

    # Tissue-scale cleanup at coarse resolution.
    kernel = np.ones((5, 5), np.uint8)
    raw = cv2.morphologyEx(raw, cv2.MORPH_OPEN, kernel, iterations=1)
    raw = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, kernel, iterations=2)

    # Restore the seed if morphology removed the tiny center region.
    cv2.circle(raw, (seed_x, seed_y), 2, 1, thickness=-1)

    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(raw, connectivity=8)
    seed_label = int(labels[seed_y, seed_x])
    if seed_label == 0 or seed_label >= n_labels:
        raise ValueError("No connected tissue region found at the clicked point")

    area = int(stats[seed_label, cv2.CC_STAT_AREA])
    if area < 20:
        raise ValueError(
            "Selected region is too small. Increase tolerance or click farther inside the tissue."
        )

    left = int(stats[seed_label, cv2.CC_STAT_LEFT])
    top = int(stats[seed_label, cv2.CC_STAT_TOP])
    width = int(stats[seed_label, cv2.CC_STAT_WIDTH])
    height = int(stats[seed_label, cv2.CC_STAT_HEIGHT])

    # Selection touching the local read window may be silently clipped, so make
    # that condition explicit for the frontend.
    touch = {
        "left": left <= 0,
        "top": top <= 0,
        "right": left + width >= w,
        "bottom": top + height >= h,
    }

    mask = (labels == seed_label).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("Could not trace the selected tissue boundary")

    contour = max(contours, key=cv2.contourArea)
    contour_area = float(cv2.contourArea(contour))
    if contour_area < 15:
        raise ValueError("Selected boundary is too small")

    perimeter = float(cv2.arcLength(contour, True))
    epsilon = max(0.75, 0.0025 * perimeter)
    approx = cv2.approxPolyDP(contour, epsilon, True)

    if approx is None or len(approx) < 3:
        approx = contour
    if approx is None or len(approx) < 3:
        raise ValueError("Could not generate a polygon from the selected region")

    points = [[float(p[0][0]), float(p[0][1])] for p in approx]
    return points, area, contour_area, touch


@app.route("/api/magic-wand", methods=["POST"])
def magic_wand():
    """Generate one tissue-scale polygon around a level-0 H&E click."""
    data = request.get_json(force=True, silent=True) or {}

    slide_id = str(data.get("slide_id") or "").strip()
    if not slide_id:
        return _json_error("slide_id is required", 400)
    if Path(slide_id).name != slide_id or slide_id in (".", ".."):
        return _json_error("invalid slide_id", 400)

    try:
        x = float(data.get("x"))
        y = float(data.get("y"))
    except Exception:
        return _json_error("x and y must be numeric H&E pixel coordinates", 400)

    try:
        tolerance = float(data.get("tolerance", 12))
    except Exception:
        tolerance = 12.0
    tolerance = max(2.0, min(40.0, tolerance))

    try:
        target_downsample = float(data.get("target_downsample", 16))
    except Exception:
        target_downsample = 16.0
    target_downsample = max(1.0, min(64.0, target_downsample))

    try:
        read_size = int(data.get("read_size", 2048))
    except Exception:
        read_size = 2048
    read_size = max(768, min(4096, read_size))

    src = _resolve_he_source_path(slide_id)
    if src is None or not src.exists():
        return _json_error(
            "Original H&E source file could not be found. "
            "Keep the original SVS/TIFF at the path used during registration.",
            404,
        )

    slide = None
    try:
        import numpy as np
        from PIL import Image
        from openslide import OpenSlide

        slide = OpenSlide(str(src))
        slide_w, slide_h = map(int, slide.dimensions)

        x = max(0.0, min(float(slide_w - 1), x))
        y = max(0.0, min(float(slide_h - 1), y))

        # Analyze at a coarse pyramid level so nuclei/cell texture are aggregated
        # into tissue-scale regions.
        level = int(slide.get_best_level_for_downsample(target_downsample))
        level_ds = float(slide.level_downsamples[level])

        # read_region location is always level-0 coordinates; size is in selected-level pixels.
        half_level0 = (read_size * level_ds) / 2.0
        left = int(round(x - half_level0))
        top = int(round(y - half_level0))

        span_w0 = int(round(read_size * level_ds))
        span_h0 = int(round(read_size * level_ds))
        left = max(0, min(max(0, slide_w - span_w0), left))
        top = max(0, min(max(0, slide_h - span_h0), top))

        # Near slide edges, the requested level patch may be smaller.
        read_w = min(read_size, max(1, int((slide_w - left) / level_ds)))
        read_h = min(read_size, max(1, int((slide_h - top) / level_ds)))

        region_rgba = slide.read_region((left, top), level, (int(read_w), int(read_h)))

        # OpenSlide can return transparent pixels outside valid tissue/canvas.
        # Composite them onto white instead of letting transparent black bias Lab.
        if region_rgba.mode != "RGBA":
            region_rgba = region_rgba.convert("RGBA")
        bg = Image.new("RGB", region_rgba.size, (255, 255, 255))
        bg.paste(region_rgba, mask=region_rgba.getchannel("A"))
        rgb = np.asarray(bg)

        seed_x = int(round((x - left) / level_ds))
        seed_y = int(round((y - top) / level_ds))
        seed_x = max(0, min(read_w - 1, seed_x))
        seed_y = max(0, min(read_h - 1, seed_y))

        local_points, area_level, contour_area_level, touch = _magic_wand_segment_patch(
            rgb, seed_x, seed_y, tolerance
        )

        # Use exact connected-component boundary contact from the helper.
        # Contact with a true slide boundary is valid; contact with an interior
        # patch boundary means the selection may have been clipped.
        truncated = (
            (touch["left"] and left > 0)
            or (touch["top"] and top > 0)
            or (touch["right"] and left + read_w * level_ds < slide_w - level_ds)
            or (touch["bottom"] and top + read_h * level_ds < slide_h - level_ds)
        )

        # Convert selected-level contour coordinates back to original level-0 pixels.
        polygon = [
            [left + px * level_ds, top + py * level_ds]
            for px, py in local_points
        ]

        # Do not duplicate the first vertex at the end; SVG polygon closes implicitly.
        area_level0 = float(area_level) * (level_ds ** 2)
        contour_area_level0 = float(contour_area_level) * (level_ds ** 2)

        return jsonify({
            "status": "success",
            "slide_id": slide_id,
            "polygon": polygon,
            "truncated": bool(truncated),
            "slide_dimensions": [slide_w, slide_h],
            "algorithm": "lab_flood_v1",
            "parameters": {
                "blur_sigma": 2.0,
                "morphology_kernel": 5,
                "morphology_open_iterations": 1,
                "morphology_close_iterations": 2,
                "background_l_threshold": 90.0,
                "background_chroma_threshold": 8.0,
                "color_space": "CIE-Lab float32",
                "connectivity": 8,
                "contour_retrieval": "RETR_EXTERNAL",
                "polygon_approximation_epsilon_fraction": 0.0025,
                "polygon_approximation_epsilon_min": 0.75,
                "seed_median_radius": 2,
                "min_area_level_pixels": 20,
                "min_contour_area_level_pixels": 15,
                "read_size_level_pixels": int(read_size),
            },
            "tolerance": tolerance,
            "analysis_level": level,
            "level_downsample": level_ds,
            "target_downsample": target_downsample,
            "area_level_pixels": area_level,
            "area_level0_pixels": area_level0,
            "contour_area_level0_pixels": contour_area_level0,
            "patch": {
                "left_level0": left,
                "top_level0": top,
                "width_level_pixels": int(read_w),
                "height_level_pixels": int(read_h),
                "span_width_level0": float(read_w * level_ds),
                "span_height_level0": float(read_h * level_ds),
            },
            "source": "coarse_openslide_level",
        })

    except ValueError as e:
        # Interactive wand misses are expected user-level outcomes, not server failures.
        return _json_error(str(e), 400)
    except Exception as e:
        print(f"[Magic Wand ERROR] {e}")
        print(traceback.format_exc())
        return _json_error(f"Magic Wand failed: {e}", 500)
    finally:
        if slide is not None:
            try:
                slide.close()
            except Exception:
                pass


# =============================================================================
# SEGMENTATION EVALUATION (Pathologist GT vs AI segmentation)
# =============================================================================

def _segmentation_class_name(feature: Dict[str, Any]) -> str:
    """Extract a class label from the GeoJSON formats used by Pathogene/QuPath."""
    props = feature.get("properties") or {}
    classification = props.get("classification")
    if isinstance(classification, dict):
        raw = classification.get("name") or classification.get("label") or ""
    elif classification is not None:
        raw = classification
    else:
        raw = (
            props.get("className") or props.get("label") or props.get("name") or
            props.get("type") or props.get("objectType") or ""
        )
    return str(raw).strip().rstrip("*").replace("_", " ").lower()


def _segmentation_union(geojson_obj: Dict[str, Any], wanted_class: str):
    """Return one repaired Shapely geometry containing all polygons for a class."""
    try:
        from shapely.geometry import shape
        from shapely.ops import unary_union
    except ImportError as e:
        raise RuntimeError(
            "Segmentation evaluation requires Shapely. Install it with: pip install shapely"
        ) from e

    features = geojson_obj.get("features", []) if isinstance(geojson_obj, dict) else []
    geoms = []
    wanted = wanted_class.lower()

    for feat in features:
        if not isinstance(feat, dict) or _segmentation_class_name(feat) != wanted:
            continue
        geom_obj = feat.get("geometry")
        if not isinstance(geom_obj, dict) or geom_obj.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            g = shape(geom_obj)
            if g.is_empty:
                continue
            if not g.is_valid:
                g = g.buffer(0)
            if not g.is_empty:
                geoms.append(g)
        except Exception:
            continue

    if not geoms:
        return None, 0

    merged = unary_union(geoms)
    if not merged.is_valid:
        merged = merged.buffer(0)
    return merged, len(geoms)


def _geometry_bounds_payload(geom):
    if geom is None or geom.is_empty:
        return None
    minx, miny, maxx, maxy = geom.bounds
    return [float(minx), float(miny), float(maxx), float(maxy)]


@app.route("/api/evaluate/segmentation", methods=["POST"])
def evaluate_segmentation():
    """Calculate GT coverage (primary), plus Dice/IoU (secondary) for Tumor/Stroma."""
    data = request.get_json(force=True, silent=True) or {}
    gt = data.get("ground_truth")
    pred = data.get("prediction")

    if not isinstance(gt, dict) or gt.get("type") != "FeatureCollection":
        return _json_error("ground_truth must be a GeoJSON FeatureCollection", 400)
    if not isinstance(pred, dict) or pred.get("type") != "FeatureCollection":
        return _json_error("prediction must be a GeoJSON FeatureCollection", 400)

    try:
        results: Dict[str, Any] = {}
        coverage_values = []
        dice_values = []
        iou_values = []
        warnings = []

        for class_name in ("tumor", "stroma"):
            gt_geom, gt_count = _segmentation_union(gt, class_name)
            pred_geom, pred_count = _segmentation_union(pred, class_name)

            class_result: Dict[str, Any] = {
                "gt_polygons": gt_count,
                "prediction_polygons": pred_count,
                "gt_bounds": _geometry_bounds_payload(gt_geom),
                "prediction_bounds": _geometry_bounds_payload(pred_geom),
            }

            if gt_geom is None:
                class_result.update({
                    "gt_coverage": None, "dice": None, "iou": None,
                    "error": "No GT polygons for this class"
                })
                results[class_name] = class_result
                continue
            if pred_geom is None:
                class_result.update({
                    "gt_coverage": 0.0, "dice": 0.0, "iou": 0.0,
                    "error": "No prediction polygons for this class"
                })
                coverage_values.append(0.0)
                dice_values.append(0.0)
                iou_values.append(0.0)
                results[class_name] = class_result
                continue

            gt_area = float(gt_geom.area)
            pred_area = float(pred_geom.area)
            intersection_area = float(gt_geom.intersection(pred_geom).area)
            union_area = float(gt_geom.union(pred_geom).area)

            # Primary metric: what fraction of the pathologist GT area is covered
            # by the AI segmentation. AI area outside the GT is not penalized here.
            gt_coverage = (intersection_area / gt_area) if gt_area > 0 else None

            # Secondary overlap metrics retained for reference only.
            dice_den = gt_area + pred_area
            dice = (2.0 * intersection_area / dice_den) if dice_den > 0 else None
            iou = (intersection_area / union_area) if union_area > 0 else None

            class_result.update({
                "gt_coverage": gt_coverage,
                "dice": dice,
                "iou": iou,
                "gt_area": gt_area,
                "prediction_area": pred_area,
                "intersection_area": intersection_area,
                "union_area": union_area,
            })

            if gt_coverage is not None:
                coverage_values.append(gt_coverage)
            if dice is not None:
                dice_values.append(dice)
            if iou is not None:
                iou_values.append(iou)
            results[class_name] = class_result

        # Warn on an obvious coordinate-scale mismatch, but do not block scoring.
        gt_parts = []
        pred_parts = []
        for class_name in ("tumor", "stroma"):
            g, _ = _segmentation_union(gt, class_name)
            p, _ = _segmentation_union(pred, class_name)
            if g is not None and not g.is_empty:
                gt_parts.append(g)
            if p is not None and not p.is_empty:
                pred_parts.append(p)
        if gt_parts and pred_parts:
            from shapely.ops import unary_union
            gb = unary_union(gt_parts).bounds
            pb = unary_union(pred_parts).bounds
            gw, gh = max(gb[2] - gb[0], 1e-9), max(gb[3] - gb[1], 1e-9)
            pw, ph = max(pb[2] - pb[0], 1e-9), max(pb[3] - pb[1], 1e-9)
            ratio = max(gw / pw, pw / gw, gh / ph, ph / gh)
            if ratio > 8.0:
                warnings.append(
                    "GT and prediction coordinate extents differ greatly. Confirm that both files use the same slide pixel coordinate system."
                )

        return jsonify({
            "status": "success",
            "classes_evaluated": ["tumor", "stroma"],
            "ignored_classes": {
                "ground_truth": ["region"],
                "prediction": ["in-situ", "other", "region"],
            },
            "results": results,
            "mean": {
                "gt_coverage": (sum(coverage_values) / len(coverage_values)) if coverage_values else None,
                "dice": (sum(dice_values) / len(dice_values)) if dice_values else None,
                "iou": (sum(iou_values) / len(iou_values)) if iou_values else None,
            },
            "warnings": warnings,
        })
    except Exception as e:
        return _json_error(f"Segmentation evaluation failed: {e}", 500)


@app.route("/api/qc/<slide_id>", methods=["GET"])
def get_qc_status(slide_id):
    qc_file = QC_DIR / f"{slide_id}.json"
    if qc_file.exists():
        return jsonify(json.loads(qc_file.read_text(encoding="utf-8")))
    return jsonify({"status": "unreviewed"})


@app.route("/api/qc/<slide_id>", methods=["POST"])
def save_qc_status(slide_id):
    qc_file = QC_DIR / f"{slide_id}.json"
    data = request.get_json(force=True, silent=True) or {}
    qc_data = {"slide_id": slide_id, "status": data.get("status"), "timestamp": datetime.now().isoformat(), "reviewer": data.get("reviewer", "admin")}
    qc_file.write_text(json.dumps(qc_data, indent=2), encoding="utf-8")
    return jsonify({"status": "success", "qc_status": qc_data["status"]})

# =============================================================================
# HEALTH / CONFIG
# =============================================================================

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({"project_dir": str(PROJECT_DIR), "backend_dir": str(BACKEND_DIR), "frontend_dir": str(FRONTEND_DIR), "data_dir": str(DATA_DIR), "slides_dir": str(SLIDES_DIR), "tiles_dir": str(TILES_DIR), "cosmx_dir": str(COSMX_DIR), "cosmx_tiles_dir": str(COSMX_TILES_DIR)})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "Pathogene Web Backend"})


@app.route("/api/routes", methods=["GET"])
def list_routes_debug():
    routes = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
        routes.append({
            "rule": str(rule),
            "endpoint": rule.endpoint,
            "methods": sorted(list(rule.methods)),
        })
    return jsonify(routes)


if __name__ == "__main__":
    # Default to localhost-only; set PATHOGENE_HOST=0.0.0.0 to expose on the
    # network (only do this on a trusted network — this serves patient slides).
    _bind_host = os.environ.get("PATHOGENE_HOST", "127.0.0.1")
    print("=" * 72)
    print("Pathogene Web Backend")
    print("=" * 72)
    print(f"BACKEND_DIR     : {BACKEND_DIR}")
    print(f"FRONTEND_DIR    : {FRONTEND_DIR}")
    print(f"DATA_DIR        : {DATA_DIR}")
    print(f"TILES_DIR       : {TILES_DIR}")
    print(f"COSMX_DIR       : {COSMX_DIR}")
    print(f"COSMX_TILES_DIR : {COSMX_TILES_DIR}")
    print("=" * 72)
    print("Open: http://localhost:8000/")
    print("=" * 72)
    app.run(debug=False, host=_bind_host, port=8000, threaded=True, use_reloader=False)
