# Pathogene - Build Guide

## Project structure (before build)

```
Pathogene/
├── build.bat               <- Windows build script  (double-click)
├── build.sh                <- macOS build script    (./build.sh)
├── pathogene.spec          <- PyInstaller config
├── launcher.py             <- App entry point (auto-opens browser)
├── requirements.txt        <- Python dependencies
├── README_BUILD.md
├── backend/
│   ├── app.py              <- Flask server
│   ├── make_dzi.py
│   ├── make_cosmx_dzi.py
│   ├── register_anchors.py
│   ├── register_fine.py
│   ├── auto_orientation.py
│   └── openslide-win64/    <- Windows only, bundled automatically if present
└── frontend/
    ├── index.html
    ├── viewer.js
    └── logo.png            <- optional
```

---

## Windows Build

### Prerequisites
- Python 3.10+ (added to PATH)
- Internet connection

### Run
```bat
build.bat
```

### Output
```
dist\Pathogene\
├── Pathogene.exe       <- entry point
├── _internal\          <- Python runtime (do not touch)
└── data\               <- slides / tiles / annotations (auto-created)
```

> Distribute the entire `dist\Pathogene\` folder as a zip.
> `Pathogene.exe` alone will not run without `_internal\`.

---

## macOS Build

### Prerequisites
```bash
brew install openslide      # required
brew install python@3.11    # if not already installed
```

### Run
```bash
chmod +x build.sh
./build.sh
```

### Gatekeeper warning
```bash
xattr -cr dist/Pathogene/Pathogene
```

---

## How it works

1. A console window opens and Flask starts on port 8000.
2. The default browser opens `http://127.0.0.1:8000` automatically.
3. Closing the console window stops the app.

> To hide the console window: set `console=False` in `pathogene.spec` and rebuild.

---

## Distribute

```bat
# Windows
cd dist
powershell Compress-Archive -Path Pathogene -DestinationPath Pathogene-Windows.zip
```

```bash
# macOS
cd dist && tar -czf Pathogene-macOS.tar.gz Pathogene/
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `openslide` import error | DLL / library missing | Windows: place `openslide-win64/` in `backend/`. Mac: `brew install openslide` |
| Browser does not open | Server failed to start | Check console for error messages |
| `cv2` import error | Wrong OpenCV package | `pip install opencv-python-headless` |
| Tile path errors after build | app.py PATH CONFIG not patched | Already patched in this zip |
| macOS "app is damaged" | Gatekeeper | Run `xattr -cr` command above |

---

## Development run (no build needed)

```bash
cd backend
pip install -r ../requirements.txt
python app.py
# Open http://localhost:8000
```
