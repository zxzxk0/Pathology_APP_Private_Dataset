# pathogene.spec — PyInstaller build config
#
# Usage:
#   pyinstaller pathogene.spec
#
# Output:
#   dist/Pathogene/
#     Pathogene.exe
#     _internal/
#     data/

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
)

SPEC_DIR     = Path(SPECPATH)
BACKEND_DIR  = SPEC_DIR / "backend"
FRONTEND_DIR = SPEC_DIR / "frontend"

# ---------------------------------------------------------------------------
# Verify build environment
# ---------------------------------------------------------------------------
# This makes the build fail early if pyvips is not installed in .venv-build.
# Otherwise PyInstaller can finish but the EXE fails later with:
# ModuleNotFoundError: No module named 'pyvips'
try:
    import pyvips
    print(f"[SPEC] pyvips found: {pyvips.__version__}")
except Exception as e:
    raise RuntimeError(
        "[SPEC ERROR] pyvips is not importable in the build environment. "
        "Run: .venv-build\\Scripts\\python.exe -m pip install -r requirements.txt"
    ) from e

# Dice / IoU evaluation uses Shapely. Verify it is installed in the build venv
# before PyInstaller starts, so a missing dependency fails at build time rather
# than later inside the packaged app.
try:
    import shapely
    print(f"[SPEC] shapely found: {shapely.__version__}")
except Exception as e:
    raise RuntimeError(
        "[SPEC ERROR] shapely is not importable in the build environment. "
        "Run: .venv-build\\Scripts\\python.exe -m pip install -r requirements.txt"
    ) from e


# ---------------------------------------------------------------------------
# datas
# ---------------------------------------------------------------------------
datas = []

# Project folders
datas += [(str(BACKEND_DIR),  "backend")]
datas += [(str(FRONTEND_DIR), "frontend")]

# openslide-python >= 1.3 ships DLLs via openslide-bin package.
# collect_data_files picks up the .dll files inside the package.
try:
    datas += collect_data_files("openslide", includes=["*.dll", "*.so", "*.dylib"])
except Exception:
    pass

try:
    datas += collect_data_files("openslide_bin", includes=["*.dll", "*.so", "*.dylib"])
except Exception:
    pass

# Manually bundled openslide-win64 folder (optional fallback)
openslide_win64 = BACKEND_DIR / "openslide-win64"
if openslide_win64.exists():
    datas += [(str(openslide_win64), "openslide-win64")]


# ---------------------------------------------------------------------------
# binaries
# ---------------------------------------------------------------------------
binaries = []

# Explicitly collect openslide DLLs so they land in _internal/
try:
    binaries += collect_dynamic_libs("openslide")
except Exception:
    pass

try:
    binaries += collect_dynamic_libs("openslide_bin")
except Exception:
    pass


# ---------------------------------------------------------------------------
# pyvips / libvips collection
# ---------------------------------------------------------------------------
# pyvips is the Python wrapper.
# pyvips-binary contains prebuilt libvips binaries on supported platforms.
# PyInstaller often misses these unless explicitly collected.
pyvips_datas = []
pyvips_binaries = []
pyvips_hiddenimports = []

try:
    d, b, h = collect_all("pyvips")
    pyvips_datas += d
    pyvips_binaries += b
    pyvips_hiddenimports += h
    print("[SPEC] collected pyvips")
except Exception as e:
    print(f"[SPEC WARN] collect_all('pyvips') failed: {e}")

# The distribution name is pyvips-binary, but the import/package name may be
# pyvips_binary depending on installation. Try it, but do not fail if absent.
try:
    d, b, h = collect_all("pyvips_binary")
    pyvips_datas += d
    pyvips_binaries += b
    pyvips_hiddenimports += h
    print("[SPEC] collected pyvips_binary")
except Exception as e:
    print(f"[SPEC WARN] collect_all('pyvips_binary') failed: {e}")

# cffi is used by pyvips.
try:
    d, b, h = collect_all("cffi")
    pyvips_datas += d
    pyvips_binaries += b
    pyvips_hiddenimports += h
    print("[SPEC] collected cffi")
except Exception as e:
    print(f"[SPEC WARN] collect_all('cffi') failed: {e}")

datas += pyvips_datas
binaries += pyvips_binaries


# ---------------------------------------------------------------------------
# Shapely / GEOS collection
# ---------------------------------------------------------------------------
# Shapely includes compiled extension modules and GEOS runtime libraries.
# Explicit collection prevents PyInstaller from producing an EXE where
# `import shapely` fails even though Shapely is installed in the build venv.
shapely_datas = []
shapely_binaries = []
shapely_hiddenimports = []

try:
    d, b, h = collect_all("shapely")
    shapely_datas += d
    shapely_binaries += b
    shapely_hiddenimports += h
    print("[SPEC] collected shapely")
except Exception as e:
    print(f"[SPEC WARN] collect_all('shapely') failed: {e}")

datas += shapely_datas
binaries += shapely_binaries


# ---------------------------------------------------------------------------
# hidden imports
# ---------------------------------------------------------------------------
hiddenimports = [
    "flask",
    "flask_cors",
    "werkzeug",
    "werkzeug.utils",
    "werkzeug.routing",
    "werkzeug.serving",
    "jinja2",
    "click",
    "itsdangerous",

    "PIL",
    "PIL.Image",
    "PIL._imaging",
    "PIL.ImageFile",

    "cv2",
    "numpy",
    "numpy.core._multiarray_umath",

    "openslide",
    "openslide.deepzoom",

    "scipy",
    "scipy.ndimage",
    "scipy.optimize",

    # Required for Dice / IoU polygon evaluation
    "shapely",
    "shapely.geometry",
    "shapely.ops",

    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",

    "importlib.util",
    "concurrent.futures",
    "threading",
    "multiprocessing",
    "psutil",

    # Required for CosMx DZI generation
    "pyvips",
    "cffi",
    "_cffi_backend",
]

hiddenimports += pyvips_hiddenimports
hiddenimports += shapely_hiddenimports

# Remove duplicates while preserving order
hiddenimports = list(dict.fromkeys(hiddenimports))


# ---------------------------------------------------------------------------
# excludes
# ---------------------------------------------------------------------------
excludes = [
    "matplotlib",
    "IPython",
    "notebook",
    "pytest",
    "setuptools",
    "pip",
    "docutils",
    "sphinx",
    "PyQt5",
    "PyQt6",
    "wx",
    "gi",
    "gtk",
]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(BACKEND_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Pathogene",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Pathogene",
)
