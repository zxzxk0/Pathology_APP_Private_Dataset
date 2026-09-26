"""
make_cosmx_dzi.py -- CosMx registered image -> sparse DZI tiles

Purpose
-------
This version is for very large CosMx image files, including OME-TIFF.

Main changes
------------
1. pyvips is REQUIRED. If pyvips is not installed, the script stops.
2. PIL fallback is DISABLED to avoid freezing the computer on huge CosMx images.
3. Empty tiles outside the transformed CosMx bounding box are SKIPPED.
4. The script does NOT create a full H&E-sized canvas in memory.
   It extracts/resizes only the CosMx region needed for each tile.
5. It still prefers transform_registered.json over transform.json.
6. drop_top_levels=1 by default.
   This skips the highest-resolution DZI level to reduce tiling time and disk size.

Expected paths
--------------
data/
  cosmx/<slide_id>.(ome.tif|ome.tiff|tif|tiff|png|jpg|jpeg)
  cosmx_tiles/<slide_id>/transform_registered.json or transform.json

Output
------
data/cosmx_tiles/<slide_id>/<slide_id>_registered.dzi
data/cosmx_tiles/<slide_id>/<slide_id>_registered_files/<level>/<col>_<row>.jpeg
"""

from pathlib import Path
import argparse
import json
import math
import sys

COSMX_EXTENSIONS = ('.ome.tif', '.ome.tiff', '.tif', '.tiff', '.png', '.jpg', '.jpeg')

def _find_cosmx_file(cosmx_dir: Path, slide_id: str):
    sid = slide_id.lower()
    for ext in COSMX_EXTENSIONS:
        p = cosmx_dir / f'{slide_id}{ext}'
        if p.exists():
            return p
    if cosmx_dir.exists():
        for p in cosmx_dir.iterdir():
            if not p.is_file():
                continue
            lower = p.name.lower()
            for ext in COSMX_EXTENSIONS:
                if lower.endswith(ext) and lower[:-len(ext)] == sid:
                    return p
    return None


# ============================================================================
# DZI HELPERS
# ============================================================================

def dzi_max_level(w, h):
    return math.ceil(math.log2(max(w, h)))


def dzi_level_size(w, h, level, max_level):
    s = 2 ** (max_level - level)
    return max(1, math.ceil(w / s)), max(1, math.ceil(h / s))


def dzi_tile_bounds(col, row, tile_size, overlap, lw, lh):
    cols = math.ceil(lw / tile_size)
    rows = math.ceil(lh / tile_size)

    gx = col * tile_size
    gy = row * tile_size

    x1 = gx - overlap if col > 0 else 0
    y1 = gy - overlap if row > 0 else 0
    x2 = min(gx + tile_size + (overlap if col < cols - 1 else 0), lw)
    y2 = min(gy + tile_size + (overlap if row < rows - 1 else 0), lh)

    return int(x1), int(y1), int(x2), int(y2)


def write_dzi(path, w, h, tile_size, overlap, fmt):
    Path(path).write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Image TileSize="{tile_size}" Overlap="{overlap}" Format="{fmt}"'
        f' xmlns="http://schemas.microsoft.com/deepzoom/2008">\n'
        f'  <Size Width="{w}" Height="{h}"/>\n'
        f'</Image>',
        encoding='utf-8'
    )


# ============================================================================
# TRANSFORM / COORDINATE HELPERS
# ============================================================================

def compute_full_res_params(transform, original_sizes, proc_size=1024, log_cb=None):
    if log_cb is None:
        log_cb = print

    he_ow, he_oh = original_sizes['he']
    cx_ow, cx_oh = original_sizes['cosmx']

    he_ts = min(proc_size / he_ow, proc_size / he_oh)
    cx_ts = min(proc_size / cx_ow, proc_size / cx_oh)

    he_full = 1.0 / he_ts
    dx_full = transform['translateX_pixels'] * he_full
    dy_full = transform['translateY_pixels'] * he_full
    cx_full_scale = transform['scale'] * cx_ts * he_full

    log_cb(f'    he_thumb_scale={he_ts:.8f}  cx_thumb_scale={cx_ts:.8f}')
    log_cb(f'    he_full={he_full:.2f}  dx_full={dx_full:.1f}  dy_full={dy_full:.1f}')
    log_cb(f'    cx_full_scale={cx_full_scale:.6f}')

    return dx_full, dy_full, cx_full_scale


def _load_transform_json(tiles_dir):
    for name in ('transform_registered.json', 'transform.json'):
        path = tiles_dir / name
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                return path, json.load(f)
    return None, None


def _normalize_transform(tj):
    tf = tj.get('transform', {})
    return {
        'rotation': int(tf.get('rotation', 0)) % 360,
        'flipX': bool(tf.get('flipX', False)),
        'flipY': bool(tf.get('flipY', False)),
        'scale': float(tf.get('scale', 1.0)),
        'translateX_pixels': float(tf.get('translateX_pixels', 0)),
        'translateY_pixels': float(tf.get('translateY_pixels', 0)),
    }


# ============================================================================
# PYVIPS SPARSE TILE GENERATION
# ============================================================================

def _require_pyvips():
    try:
        import pyvips
        return pyvips
    except Exception as e:
        raise RuntimeError(
            'pyvips is required for CosMx DZI generation. '
            'PIL fallback is disabled to avoid freezing on large CosMx PNG files. '
            'Install pyvips in this environment before running CosMx tiling.'
        ) from e


def _prepare_vips_cosmx(cosmx_path, rotation, flip_x, flip_y, log_cb):
    pyvips = _require_pyvips()

    log_cb(f'  [pyvips] Loading CosMx: {cosmx_path.name}')
    img = pyvips.Image.new_from_file(str(cosmx_path), access='random')
    log_cb(f'  [pyvips] Original CosMx: {img.width}x{img.height}, bands={img.bands}')

    # Match the original PIL convention used in the previous script.
    if rotation == 90:
        img = img.rot('d90')
    elif rotation == 180:
        img = img.rot('d180')
    elif rotation == 270:
        img = img.rot('d270')

    if flip_x:
        img = img.fliphor()
    if flip_y:
        img = img.flipver()

    # Normalize bands to RGB.
    if img.hasalpha():
        img = img.flatten(background=[255, 255, 255])
    if img.bands == 1:
        img = img.colourspace('srgb')
    if img.bands > 3:
        img = img.extract_band(0, n=3)

    log_cb(f'  [pyvips] Transformed CosMx: {img.width}x{img.height}, bands={img.bands}')
    return pyvips, img


def generate_dzi_sparse_pyvips(
    cosmx_path,
    he_orig_w,
    he_orig_h,
    dx_full,
    dy_full,
    cx_full_scale,
    rotation,
    flip_x,
    flip_y,
    out_dir,
    name,
    log_cb=None,
    tile_size=254,
    overlap=1,
    fmt='jpeg',
    quality=85,
    skip_empty=True,
    drop_top_levels=1,
):
    if log_cb is None:
        log_cb = print

    if fmt not in ('jpeg', 'png'):
        raise ValueError('fmt must be jpeg or png')

    pyvips, cx_img = _prepare_vips_cosmx(
        Path(cosmx_path),
        rotation,
        flip_x,
        flip_y,
        log_cb,
    )

    file_ext = 'jpeg' if fmt == 'jpeg' else 'png'

    max_level = dzi_max_level(he_orig_w, he_orig_h)
    drop_top_levels = max(0, int(drop_top_levels or 0))
    effective_max_level = max(0, max_level - drop_top_levels)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    write_dzi(out_dir / f'{name}.dzi', he_orig_w, he_orig_h, tile_size, overlap, fmt)

    files_dir = out_dir / f'{name}_files'
    files_dir.mkdir(parents=True, exist_ok=True)

    cx_w = cx_img.width
    cx_h = cx_img.height

    total_possible = 0

    for lv in range(effective_max_level + 1):
        lw, lh = dzi_level_size(he_orig_w, he_orig_h, lv, max_level)
        total_possible += math.ceil(lw / tile_size) * math.ceil(lh / tile_size)

    log_cb(
        f'  [DZI] {name}.dzi  canvas={he_orig_w}x{he_orig_h}, '
        f'original_levels={max_level + 1}'
    )
    log_cb(
        f'  [DZI] generating levels 0..{effective_max_level}  '
        f'drop_top_levels={drop_top_levels}'
    )
    log_cb(f'  [Sparse] skip_empty={skip_empty}; empty white tiles will not be saved')
    log_cb(f'  [Plan] max possible tiles: {total_possible:,}')

    written = 0
    skipped_empty = 0
    processed = 0

    for lv in range(effective_max_level + 1):
        lw, lh = dzi_level_size(he_orig_w, he_orig_h, lv, max_level)
        lv_scale = lw / he_orig_w
        combined = cx_full_scale * lv_scale

        dx_lv = int(round(dx_full * lv_scale))
        dy_lv = int(round(dy_full * lv_scale))

        cx_bbox_w = max(1, int(round(cx_w * combined)))
        cx_bbox_h = max(1, int(round(cx_h * combined)))

        lv_dir = files_dir / str(lv)
        lv_dir.mkdir(parents=True, exist_ok=True)

        cols = math.ceil(lw / tile_size)
        rows = math.ceil(lh / tile_size)

        level_written = 0
        level_skipped = 0

        for col in range(cols):
            for row in range(rows):
                processed += 1

                x1, y1, x2, y2 = dzi_tile_bounds(
                    col,
                    row,
                    tile_size,
                    overlap,
                    lw,
                    lh,
                )

                tw = x2 - x1
                th = y2 - y1

                tile_path = lv_dir / f'{col}_{row}.{file_ext}'

                if tile_path.exists() and tile_path.stat().st_size > 0:
                    written += 1
                    level_written += 1
                    continue

                # No overlap between this DZI tile and the transformed CosMx bbox.
                no_cosmx = (
                    x2 <= dx_lv or
                    x1 >= dx_lv + cx_bbox_w or
                    y2 <= dy_lv or
                    y1 >= dy_lv + cx_bbox_h
                )

                if no_cosmx:
                    skipped_empty += 1
                    level_skipped += 1

                    if skip_empty:
                        continue

                    tile = pyvips.Image.black(tw, th, bands=3) + 255

                    if fmt == 'jpeg':
                        tile.jpegsave(str(tile_path), Q=quality)
                    else:
                        tile.pngsave(str(tile_path), compression=6)

                    written += 1
                    level_written += 1
                    continue

                if combined <= 0:
                    skipped_empty += 1
                    level_skipped += 1
                    continue

                # Map the H&E-level tile bounds into transformed CosMx coordinates.
                cx_x1 = max(0, math.floor((x1 - dx_lv) / combined))
                cx_y1 = max(0, math.floor((y1 - dy_lv) / combined))
                cx_x2 = min(cx_w, math.ceil((x2 - dx_lv) / combined) + 1)
                cx_y2 = min(cx_h, math.ceil((y2 - dy_lv) / combined) + 1)

                if cx_x2 <= cx_x1 or cx_y2 <= cx_y1:
                    skipped_empty += 1
                    level_skipped += 1
                    continue

                region = cx_img.crop(
                    cx_x1,
                    cx_y1,
                    cx_x2 - cx_x1,
                    cx_y2 - cx_y1,
                )

                # Resize the small crop to the current DZI level scale.
                target_w = max(1, int(round((cx_x2 - cx_x1) * combined)))
                target_h = max(1, int(round((cx_y2 - cx_y1) * combined)))

                resized = region.resize(
                    target_w / max(1, region.width),
                    vscale=target_h / max(1, region.height),
                )

                # Position inside the output tile.
                px = int(round(cx_x1 * combined + dx_lv - x1))
                py = int(round(cx_y1 * combined + dy_lv - y1))

                # If rounding pushes slightly outside, crop the resized region.
                if px < 0:
                    resized = resized.crop(
                        -px,
                        0,
                        max(1, resized.width + px),
                        resized.height,
                    )
                    px = 0

                if py < 0:
                    resized = resized.crop(
                        0,
                        -py,
                        resized.width,
                        max(1, resized.height + py),
                    )
                    py = 0

                if px >= tw or py >= th:
                    skipped_empty += 1
                    level_skipped += 1
                    continue

                if px + resized.width > tw:
                    resized = resized.crop(
                        0,
                        0,
                        max(1, tw - px),
                        resized.height,
                    )

                if py + resized.height > th:
                    resized = resized.crop(
                        0,
                        0,
                        resized.width,
                        max(1, th - py),
                    )

                tile = pyvips.Image.black(tw, th, bands=3) + 255
                tile = tile.insert(resized, px, py, expand=False)

                if fmt == 'jpeg':
                    tile.jpegsave(str(tile_path), Q=quality)
                else:
                    tile.pngsave(str(tile_path), compression=6)

                written += 1
                level_written += 1

        log_cb(
            f'    Level {lv:>2}: {lw}x{lh} '
            f'written={level_written:,} skipped_empty={level_skipped:,} '
            f'total_written={written:,}'
        )

    log_cb(
        f'  [Done] processed={processed:,} written={written:,} '
        f'skipped_empty={skipped_empty:,}'
    )

    return True


# ============================================================================
# PROCESS SINGLE SLIDE
# ============================================================================

def process_single_slide(
    slide_id,
    data_dir,
    tile_size=254,
    overlap=1,
    fmt='jpeg',
    quality=85,
    log_cb=None,
    skip_empty=True,
    drop_top_levels=1,
):
    if log_cb is None:
        log_cb = print

    # Fail early if pyvips is missing. This prevents accidental PIL fallback.
    _require_pyvips()

    data_dir = Path(data_dir)
    cosmx_dir = data_dir / 'cosmx'
    tiles_dir = data_dir / 'cosmx_tiles' / slide_id

    json_path, tj = _load_transform_json(tiles_dir)

    if tj is None:
        log_cb(f'  [SKIP] No transform JSON in {tiles_dir}')
        return False

    transform = _normalize_transform(tj)
    original_sizes = tj.get('original_sizes', {})

    if not original_sizes or 'he' not in original_sizes or 'cosmx' not in original_sizes:
        log_cb('  [SKIP] original_sizes missing. Re-run auto_orientation/register_fine.')
        return False

    proc_size = tj.get('detection', {}).get('processing_size', 1024)

    he_ow, he_oh = original_sizes['he']
    cx_ow, cx_oh = original_sizes['cosmx']

    log_cb(f'\n[{slide_id}]')
    log_cb(f'  JSON  : {json_path.name}')
    log_cb(f'  H&E   : {he_ow}x{he_oh}')
    log_cb(f'  CosMx : {cx_ow}x{cx_oh}')
    log_cb(
        f'  Rot={transform["rotation"]} FX={transform["flipX"]} '
        f'FY={transform["flipY"]} scale={transform["scale"]:.6f} '
        f'dx={transform["translateX_pixels"]:.1f} dy={transform["translateY_pixels"]:.1f}'
    )

    log_cb('  [Coord conversion]')

    dx_full, dy_full, cx_full_scale = compute_full_res_params(
        transform,
        original_sizes,
        proc_size,
        log_cb=log_cb,
    )

    cosmx_path = _find_cosmx_file(cosmx_dir, slide_id)
    if cosmx_path is None:
        log_cb(f'  [SKIP] CosMx image not found for {slide_id}')
        return False

    tiles_dir.mkdir(parents=True, exist_ok=True)

    return generate_dzi_sparse_pyvips(
        cosmx_path=cosmx_path,
        he_orig_w=int(he_ow),
        he_orig_h=int(he_oh),
        dx_full=dx_full,
        dy_full=dy_full,
        cx_full_scale=cx_full_scale,
        rotation=transform['rotation'],
        flip_x=transform['flipX'],
        flip_y=transform['flipY'],
        out_dir=tiles_dir,
        name=f'{slide_id}_registered',
        log_cb=log_cb,
        tile_size=tile_size,
        overlap=overlap,
        fmt=fmt,
        quality=quality,
        skip_empty=skip_empty,
        drop_top_levels=drop_top_levels,
    )


# ============================================================================
# Entry point called from app.py
# ============================================================================

def run(slide_id: str, data_dir: str, log_cb=None, drop_top_levels=1):
    ok = process_single_slide(
        slide_id,
        data_dir,
        log_cb=log_cb,
        drop_top_levels=drop_top_levels,
    )

    if not ok:
        raise RuntimeError(f'CosMx DZI failed for {slide_id}')


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='CosMx registered PNG -> sparse DZI')

    parser.add_argument('--slide-id')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--data-dir', default=r'D:\data')
    parser.add_argument('--tile-size', type=int, default=254)
    parser.add_argument('--overlap', type=int, default=1)
    parser.add_argument('--fmt', choices=['jpeg', 'png'], default='jpeg')
    parser.add_argument('--quality', type=int, default=85)
    parser.add_argument(
        '--no-skip-empty',
        action='store_true',
        help='Write white empty tiles instead of skipping them',
    )
    parser.add_argument(
        '--drop-top-levels',
        type=int,
        default=1,
        help='Skip this many highest-resolution DZI levels. Default: 1 for faster demo tiling.',
    )

    args = parser.parse_args()

    data_dir = Path(args.data_dir)

    kw = dict(
        tile_size=args.tile_size,
        overlap=args.overlap,
        fmt=args.fmt,
        quality=args.quality,
        skip_empty=not args.no_skip_empty,
        drop_top_levels=args.drop_top_levels,
    )

    # Fail early.
    _require_pyvips()

    if args.all:
        root = data_dir / 'cosmx_tiles'

        ids = [
            d.name for d in root.iterdir()
            if d.is_dir()
            and (
                (d / 'transform_registered.json').exists()
                or (d / 'transform.json').exists()
            )
        ]

        print(f'[Batch] {len(ids)} slides')

        ok_count = 0

        for i, sid in enumerate(ids, 1):
            print(f'[{i}/{len(ids)}] {sid}')

            try:
                if process_single_slide(sid, data_dir, **kw):
                    ok_count += 1
            except Exception as e:
                import traceback
                print(f'  [ERROR] {e}')
                traceback.print_exc()

        print(f'[Batch Done] {ok_count}/{len(ids)}')

        return ok_count == len(ids)

    if not args.slide_id:
        print('[ERROR] --slide-id or --all required')
        return False

    return process_single_slide(args.slide_id, data_dir, **kw)


if __name__ == '__main__':
    try:
        sys.exit(0 if main() else 1)
    except KeyboardInterrupt:
        print('\n[Stopped]')
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f'\n[ERROR] {e}')
        traceback.print_exc()
        sys.exit(1)