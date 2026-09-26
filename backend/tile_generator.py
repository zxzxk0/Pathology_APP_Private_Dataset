"""
SVS to DeepZoom (DZI) Tile Generator
-----------------------------------

Converts whole-slide images (.svs) into DeepZoom (.dzi + _files/) tiles for OpenSeadragon.

Dependencies:
  pip install pyvips
  (Plus a libvips build that can read SVS; on many systems you also need OpenSlide)

Usage:
  # Single file
  python tile_generator.py /path/to/slide.svs data/tiles

  # Directory (batch)
  python tile_generator.py data/slides data/tiles

Output layout (per slide):
  data/tiles/<slide_name>/<slide_name>.dzi
  data/tiles/<slide_name>/<slide_name>_files/...

Notes:
  - This script stores tiles in a folder named after the slide.
  - The Flask backend expects DZI at: data/tiles/<slide>/<slide>.dzi
"""

import sys
from pathlib import Path
import pyvips

def generate_dzi_tiles(svs_path: Path, output_dir: Path, tile_size: int = 256, overlap: int = 1, suffix: str = ".jpg"):
    if not svs_path.exists():
        raise FileNotFoundError(f"SVS not found: {svs_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    slide_name = svs_path.stem
    tile_dir = output_dir / slide_name
    tile_dir.mkdir(parents=True, exist_ok=True)

    dzi_path = tile_dir / f"{slide_name}.dzi"

    print(f"📂 Loading slide: {svs_path.name}")

    try:
        image = pyvips.Image.new_from_file(str(svs_path), access='sequential')
    except Exception as e:
        raise RuntimeError(
            f"❌ Error loading slide with pyvips: {e}\n"
            "Tip: Ensure libvips supports SVS. On macOS: brew install vips openslide\n"
            "On Ubuntu: apt install libvips openslide-tools (and libvips via apt)\n"
        )

    print(f"📐 Image size: {image.width} x {image.height}")
    print(f"🧩 Generating DeepZoom tiles -> {dzi_path}")

    # dzsave writes: <basename>.dzi and <basename>_files/
    # We want basename to be inside tile_dir, and to be slide_name
    base = tile_dir / slide_name

    image.dzsave(
        str(base),
        tile_size=tile_size,
        overlap=overlap,
        suffix=suffix,
        depth='one'  # creates lower levels; 'one' is OK for DeepZoom pyramid
    )

    if not dzi_path.exists():
        # Some libvips versions write without .dzi extension if misconfigured, sanity check
        raise RuntimeError(f"Tile generation finished but DZI missing: {dzi_path}")

    print("✅ Done")


def main():
    if len(sys.argv) < 3:
        print("\nUsage:\n  python tile_generator.py <input_svs_or_dir> <output_tiles_dir>\n")
        sys.exit(1)

    input_path = Path(sys.argv[1]).expanduser().resolve()
    output_dir = Path(sys.argv[2]).expanduser().resolve()

    if input_path.is_dir():
        svs_files = sorted(list(input_path.glob("*.svs")))
        if not svs_files:
            print(f"❌ No .svs files found in: {input_path}")
            sys.exit(1)

        print(f"🔍 Found {len(svs_files)} SVS file(s)")

        for i, svs in enumerate(svs_files, 1):
            print(f"\n[{i}/{len(svs_files)}] Processing {svs.name}...")
            try:
                generate_dzi_tiles(svs, output_dir)
            except Exception as e:
                print(str(e))
                print("(continuing...)")

    else:
        generate_dzi_tiles(input_path, output_dir)


if __name__ == "__main__":
    main()
