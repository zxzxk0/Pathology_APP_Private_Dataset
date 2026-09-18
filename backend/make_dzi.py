"""
make_dzi.py  --  SVS -> DeepZoom DZI tiles

Optimizations:
  - Multi-threaded tile generation (ThreadPoolExecutor)
  - Resume: skip already-existing tiles
  - Per-tile progress + ETA + speed
  - RAM logging (psutil optional)
  - Slow tile detection

Change:
  - drop_top_levels=1 by default, but now implemented as a VALID reduced-resolution
    DZI pyramid instead of leaving missing top levels.
  - drop_top_levels=1 means a 2x linear downsample (1/4 as many full-resolution
    pixels); drop_top_levels=2 means 4x linear downsample (1/16 as many).
  - A dzi_meta.json sidecar records the original/viewer dimensions and scale so
    the frontend can preserve original-slide coordinates for GeoJSON I/O.
"""

from pathlib import Path
from openslide import OpenSlide
from openslide.deepzoom import DeepZoomGenerator
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys, json, argparse, time, os, threading

# ── Settings ────────────────────────────────────────────────────────────────
NUM_WORKERS   = min((os.cpu_count() or 4), 8)  # parallel worker threads (max 8)
LOG_INTERVAL  = 2000    # print progress every N tiles
SLOW_TILE_MS  = 3000    # warn if a tile takes longer than this (ms)
RESUME        = True    # True: skip existing tiles (resume support)
USE_PSUTIL    = True    # False: disable RAM logging
# ─────────────────────────────────────────────────────────────────────────────

def _ram_mb():
    if not USE_PSUTIL:
        return None
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except Exception:
        return None


def run(
    svs_path,
    out_dir,
    log_cb=None,
    tile_size=254,
    overlap=1,
    quality=90,
    drop_top_levels=1,
):
    ok = export_deepzoom(
        svs_path,
        out_dir,
        tile_size=tile_size,
        overlap=overlap,
        quality=quality,
        log_cb=log_cb,
        drop_top_levels=drop_top_levels,
    )
    if not ok:
        raise RuntimeError(f'H&E tiling failed: {svs_path}')


def export_deepzoom(
    svs_path,
    out_dir,
    tile_size=254,
    overlap=1,
    fmt='jpeg',
    quality=90,
    log_cb=None,
    drop_top_levels=1,
):
    if log_cb is None:
        log_cb = print

    out_dir  = Path(out_dir)
    svs_path = Path(svs_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not svs_path.exists():
        log_cb(f'[H&E] ERROR: SVS not found: {svs_path}')
        return False

    slide_id  = svs_path.stem
    slide_out = out_dir / slide_id
    slide_out.mkdir(parents=True, exist_ok=True)

    ram = _ram_mb()
    log_cb(
        f'[H&E] === START {slide_id} ==='
        + (f'  RAM={ram:.0f}MB' if ram else '')
    )
    log_cb(
        f'[H&E] workers={NUM_WORKERS}  tile={tile_size}  quality={quality}'
        f'  resume={RESUME}  drop_top_levels={drop_top_levels}'
    )

    try:
        t0    = time.time()
        slide = OpenSlide(str(svs_path))
        w, h  = slide.dimensions

        log_cb(
            f'[H&E] Opened {time.time() - t0:.1f}s  |  {w}x{h} px'
            f'  |  OpenSlide levels: {slide.level_count}'
        )

        dz = DeepZoomGenerator(
            slide,
            tile_size=tile_size,
            overlap=overlap,
            limit_bounds=False,
        )

        # Build a VALID reduced-resolution DZI instead of advertising the
        # original dimensions while omitting the top tiles. The lower DeepZoom
        # levels generated from the original slide are exactly the pyramid for
        # a 2**drop_top_levels downsampled view.
        drop_top_levels = max(0, int(drop_top_levels or 0))
        max_drop = max(0, dz.level_count - 1)
        if drop_top_levels > max_drop:
            log_cb(
                f'[H&E] WARN drop_top_levels={drop_top_levels} is too large; '
                f'clamping to {max_drop}'
            )
            drop_top_levels = max_drop

        dzi_downsample = 2 ** drop_top_levels
        dzi_w = max(1, (w + dzi_downsample - 1) // dzi_downsample)
        dzi_h = max(1, (h + dzi_downsample - 1) // dzi_downsample)
        max_lv_exclusive = max(1, dz.level_count - drop_top_levels)

        # DZI descriptor uses the actual maximum view resolution. This keeps
        # OpenSeadragon from requesting non-existent high-resolution levels.
        (slide_out / f'{slide_id}.dzi').write_text(
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Image TileSize="{tile_size}" Overlap="{overlap}" Format="{fmt}"'
            f' xmlns="http://schemas.microsoft.com/deepzoom/2008">\n'
            f'  <Size Width="{dzi_w}" Height="{dzi_h}"/>\n'
            f'</Image>',
            encoding='utf-8'
        )

        # Sidecar used by the frontend to map viewer pixels back to original
        # SVS pixels when importing/exporting GeoJSON annotations.
        (slide_out / 'dzi_meta.json').write_text(
            json.dumps({
                'slide_w': w,
                'slide_h': h,
                'dzi_w': dzi_w,
                'dzi_h': dzi_h,
                'dzi_downsample': dzi_downsample,
                'drop_top_levels': drop_top_levels,
            }, indent=2),
            encoding='utf-8'
        )

        files_dir = slide_out / f'{slide_id}_files'
        files_dir.mkdir(parents=True, exist_ok=True)

        # Print per-level tile counts
        total   = 0
        lv_info = []

        log_cb(
            f'[H&E] --- Level plan ({max_lv_exclusive} exported of '
            f'{dz.level_count} source levels) ---'
        )
        log_cb(
            f'[H&E] Viewer resolution: {dzi_w}x{dzi_h} '
            f'(original {w}x{h}, downsample={dzi_downsample}x)'
        )

        for lv in range(max_lv_exclusive):
            c, r = dz.level_tiles[lv]
            n    = c * r
            total += n
            lv_info.append((lv, c, r, n))
            log_cb(f'[H&E]   lv {lv:>2}: {c}x{r} = {n:,} tiles')

        if drop_top_levels:
            log_cb(
                f'[H&E] SAFE_DOWNSAMPLE={dzi_downsample}x '
                f'→ source levels {max_lv_exclusive}..{dz.level_count - 1} '
                f'are intentionally excluded from the reduced DZI'
            )

        log_cb(f'[H&E] TOTAL: {total:,} tiles  workers={NUM_WORKERS}')

        # ── Shared counters (thread-safe via lock) ───────────────────────────
        _lock    = threading.Lock()
        counters = {'done': 0, 'skipped': 0, 'errors': 0}
        t_all    = time.time()

        def _save_tile(level, col, row, level_dir):
            """Save a single tile. Called from worker thread."""
            tile_path = level_dir / f'{col}_{row}.jpeg'

            # Resume: skip if tile already exists
            if RESUME and tile_path.exists() and tile_path.stat().st_size > 0:
                with _lock:
                    counters['done']    += 1
                    counters['skipped'] += 1
                return True, None

            t_tile = time.time()

            try:
                dz.get_tile(level, (col, row)).save(
                    tile_path,
                    format='JPEG',
                    quality=quality,
                )
                elapsed_ms = (time.time() - t_tile) * 1000

                with _lock:
                    counters['done'] += 1
                    done_now = counters['done']

                    # Periodic progress report
                    if done_now % LOG_INTERVAL == 0:
                        elapsed = time.time() - t_all
                        rate    = done_now / elapsed if elapsed > 0 else 0
                        eta_s   = (total - done_now) / rate if rate > 0 else 0
                        ram     = _ram_mb()

                        log_cb(
                            f'[H&E]   {done_now:,}/{total:,}'
                            f'  {done_now / total * 100:.1f}%'
                            f'  {rate:.0f} t/s'
                            f'  ETA {eta_s / 60:.1f} min'
                            + (f'  RAM={ram:.0f}MB' if ram else '')
                        )

                if elapsed_ms > SLOW_TILE_MS:
                    log_cb(
                        f'[H&E] SLOW ({level},{col},{row}): '
                        f'{elapsed_ms:.0f}ms'
                    )

                return True, None

            except Exception as e:
                with _lock:
                    counters['errors'] += 1
                return False, f'({level},{col},{row}): {e}'

        # ── Level loop ────────────────────────────────────────────────────────
        for lv, cols, rows, n_tiles in lv_info:
            level_dir = files_dir / str(lv)
            level_dir.mkdir(parents=True, exist_ok=True)

            t_lv = time.time()
            ram  = _ram_mb()

            log_cb(
                f'[H&E] >>> Level {lv} ({cols}x{rows}={n_tiles:,} tiles)'
                + (f'  RAM={ram:.0f}MB' if ram else '')
            )

            err_lv = 0

            with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
                futures = {
                    pool.submit(_save_tile, lv, col, row, level_dir): (col, row)
                    for col in range(cols)
                    for row in range(rows)
                }

                for fut in as_completed(futures):
                    ok_tile, err_msg = fut.result()
                    if not ok_tile:
                        err_lv += 1
                        log_cb(f'[H&E] WARN {err_msg}')

            lv_time = time.time() - t_lv
            done_now = counters['done']

            log_cb(
                f'[H&E] <<< Level {lv} done'
                f'  {lv_time:.1f}s'
                f'  err={err_lv}'
                f'  [{done_now:,}/{total:,}  {done_now / total * 100:.0f}%]'
            )

        total_time = time.time() - t_all

        log_cb(
            f'[H&E] === Tiling done ==='
            f'  {counters["done"]:,} tiles'
            f'  skipped={counters["skipped"]:,}'
            f'  errors={counters["errors"]}'
            f'  {total_time:.1f}s ({total_time / 60:.1f}min)'
        )

        # ── Thumbnail ──────────────────────────────────────────────────────
        try:
            t_th  = time.time()
            thumb = slide.get_thumbnail((1024, 1024))
            thumb.save(str(slide_out / 'thumbnail.jpg'), 'JPEG', quality=85)

            tw, th = thumb.size

            (slide_out / 'thumbnail_scale.json').write_text(
                json.dumps({
                    'slide_w': w,
                    'slide_h': h,
                    'thumb_w': tw,
                    'thumb_h': th,
                    'he_thumb_scale': min(tw / w, th / h),
                    'dzi_w': dzi_w,
                    'dzi_h': dzi_h,
                    'dzi_downsample': dzi_downsample,
                }),
                encoding='utf-8'
            )

            log_cb(f'[H&E] Thumbnail {tw}x{th}  {time.time() - t_th:.1f}s')

        except Exception as e:
            log_cb(f'[H&E] WARN thumbnail: {e}')

        slide.close()
        return True

    except Exception as e:
        import traceback
        log_cb(f'[H&E] ERROR: {e}\n{traceback.format_exc()}')
        return False


def batch_process(slides_dir, out_dir, log_cb=None, **kwargs):
    if log_cb is None:
        log_cb = print

    svs_files = list(Path(slides_dir).glob('*.svs'))

    if not svs_files:
        log_cb(f'[H&E] No SVS files in {slides_dir}')
        return

    ok = 0

    for i, f in enumerate(svs_files, 1):
        log_cb(f'[H&E] [{i}/{len(svs_files)}] {f.name}')

        if export_deepzoom(str(f), out_dir, log_cb=log_cb, **kwargs):
            ok += 1

    log_cb(f'[H&E] Batch done: {ok}/{len(svs_files)}')


if __name__ == '__main__':
    p = argparse.ArgumentParser()

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--svs')
    g.add_argument('--batch')

    p.add_argument('--out', required=True)
    p.add_argument('--tile-size', type=int, default=254)
    p.add_argument('--quality', type=int, default=90)
    p.add_argument('--workers', type=int, default=NUM_WORKERS)
    p.add_argument(
        '--drop-top-levels',
        type=int,
        default=1,
        help='Reduce viewer resolution by 2**N while keeping a valid DZI pyramid. 0=full, 1=2x, 2=4x. Default: 1.'
    )

    a = p.parse_args()

    NUM_WORKERS = a.workers

    kw = dict(
        tile_size=a.tile_size,
        quality=a.quality,
        drop_top_levels=a.drop_top_levels,
    )

    if a.svs:
        sys.exit(0 if export_deepzoom(a.svs, a.out, **kw) else 1)
    else:
        batch_process(a.batch, a.out, **kw)