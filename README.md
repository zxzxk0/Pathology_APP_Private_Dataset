# Pathology_APP / Pathogene

Pathogene is a local web-based pathology viewer for H&E whole-slide images, CosMx spatial image alignment, annotation, and registration preview.

The application runs a local Flask backend and opens the viewer in your web browser at:

```text
http://127.0.0.1:8000
```

---

## 1. Project Overview

This project is designed to run locally on a user's computer.

Main features include:

- H&E whole-slide image viewing
- CosMx image loading and alignment
- H&E / CosMx overlay preview
- Manual and automatic registration support
- Annotation import/export
- Local browser-based viewer
- Standalone executable build using PyInstaller

The app does **not** require a public server.  
When the executable is launched, it starts a local backend server and opens the browser viewer automatically.

---

## 2. Repository Structure

```text
Pathology_APP/
├── backend/
│   ├── app.py
│   ├── make_dzi.py
│   ├── make_cosmx_dzi.py
│   ├── register_anchors.py
│   ├── register_fine.py
│   └── auto_orientation.py
│
├── frontend/
│   ├── index.html
│   ├── viewer.js
│   └── logo.png
│
├── launcher.py
├── pathogene.spec
├── requirements.txt
├── build.bat
├── build.sh
├── README.md
├── README_BUILD.md
└── .gitignore
```

After building, PyInstaller creates a `dist/` folder:

```text
dist/
└── Pathogene/
    ├── Pathogene.exe        # Windows executable
    ├── _internal/           # Required runtime files
    └── data/                # Auto-created data folder
```

Important:

> Do **not** move or distribute `Pathogene.exe` alone.  
> The executable needs the `_internal/` folder and other bundled files.  
> If you want to share the built app, zip the entire `dist/Pathogene/` folder.

---

## 3. Download the Project

Clone this repository:

```bash
git clone https://github.com/zxzxk0/Pathology_APP.git
cd Pathology_APP
```

---

## 4. Build Instructions

Choose the build script based on your operating system.

---

### Option A. Windows Build

#### Requirements

- Windows 10 or later
- Python 3.10 or later
- Git
- Internet connection for installing Python packages

#### Build

Open **Command Prompt**, **PowerShell**, or **Git Bash** inside the project folder and run:

```bat
build.bat
```

The script installs the required packages and builds the app using PyInstaller.

After the build is complete, the output will be created here:

```text
dist\Pathogene\
```

Run the application:

```text
dist\Pathogene\Pathogene.exe
```

You can double-click:

```text
Pathogene.exe
```

The app will start a local Flask server and open the browser automatically.

---

### Option B. macOS Build

#### Requirements

Install OpenSlide first:

```bash
brew install openslide
```

Then run:

```bash
chmod +x build.sh
./build.sh
```

After the build is complete, the output will be created here:

```text
dist/Pathogene/
```

Run the application from the built folder.

If macOS blocks the app because of Gatekeeper, run:

```bash
xattr -cr dist/Pathogene/Pathogene
```

Then try launching it again.

---

### Option C. Linux Build

Linux users can usually use the same shell script:

```bash
chmod +x build.sh
./build.sh
```

Depending on your Linux distribution, OpenSlide may need to be installed manually.

For Ubuntu/Debian:

```bash
sudo apt update
sudo apt install openslide-tools libopenslide-dev
```

Then run the build script again.

---

## 5. Running the Built App

After building, go to the `dist` folder:

### Windows

```text
dist\Pathogene\Pathogene.exe
```

### macOS / Linux

```text
dist/Pathogene/Pathogene
```

When the app starts:

1. A local Flask server starts on port `8000`.
2. Your default browser opens automatically.
3. The viewer loads at:

```text
http://127.0.0.1:8000
```

To stop the app, close the terminal or console window running the server.

---

## 6. How to Share the App

If you want to share the already-built Windows app, zip the entire folder:

```text
dist\Pathogene\
```

For example:

```text
Pathogene-Windows.zip
```

The zip file should contain:

```text
Pathogene/
├── Pathogene.exe
├── _internal/
└── data/
```

Again, do **not** send only `Pathogene.exe`.

---

## 7. Development Mode

If you want to run the app without building an executable:

```bash
pip install -r requirements.txt
cd backend
python app.py
```

Then open:

```text
http://127.0.0.1:8000
```

Development mode is useful when editing `backend/` or `frontend/` files.

---

## 8. Python Dependencies

Install dependencies manually with:

```bash
pip install -r requirements.txt
```

Main dependencies include:

- Flask
- Flask-CORS
- OpenSlide
- Pillow
- OpenCV
- NumPy
- SciPy
- pyvips
- psutil
- PyInstaller

---

## 9. Notes About Large Pathology Files

Whole-slide image files and generated tile folders can be very large.

The following types of files should generally **not** be committed to GitHub:

```text
*.svs
*.ndpi
*.tif
*.tiff
*.dzi
*_files/
dist/
build/
.venv-build/
data/
```

These should be kept locally or shared separately.

---

## 10. Troubleshooting

### `openslide` import error

Windows users should make sure OpenSlide binaries are included or installed correctly.  
macOS users can run:

```bash
brew install openslide
```

Linux users can run:

```bash
sudo apt install openslide-tools libopenslide-dev
```

---

### Browser does not open automatically

Open the browser manually and go to:

```text
http://127.0.0.1:8000
```

---

### Port 8000 is already in use

Close the previous app window or stop the process using port `8000`, then restart the app.

---

### `cv2` import error

Reinstall OpenCV:

```bash
pip install opencv-python-headless
```

---

### Build succeeds but app does not run

Make sure you are running the executable from inside the full built folder:

```text
dist/Pathogene/
```

Do not copy only the executable to another location.

---

## 11. Recommended GitHub Upload

The GitHub repository should include source code and build scripts:

```text
backend/
frontend/
launcher.py
pathogene.spec
requirements.txt
build.bat
build.sh
README.md
README_BUILD.md
.gitignore
```

The repository should **not** include:

```text
dist/
build/
.venv-build/
large slide files
generated tile folders
```

Users can build the app themselves after cloning the repository.

---

## 12. License

Add a license here if needed.
