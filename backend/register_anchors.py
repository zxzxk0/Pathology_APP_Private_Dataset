"""
register_anchors.py  —  Anchor   CosMx-H&E Registration

   :
  auto   : SIFT feature matching        
  manual :   JS anchor          

     :
  rigid  : SVD   rigid body  (rotation + translation, scale=1  )
  affine : estimateAffinePartial2D  (rotation + scale + translation)

   : cosmx_tiles/{slide_id}/transform_registered.json
  make_cosmx_dzi.py      :
    transform.rotation          : 90      (0/90/180/270)
    transform.flipX / flipY     : False (anchor        )
    transform.scale             : CosMx_thumb -> H&E_thumb  
    transform.translateX_pixels : H&E thumbnail (proc_size=1024)   tx
    transform.translateY_pixels : H&E thumbnail (proc_size=1024)   ty
    original_sizes.he/cosmx     :      

        (  make_cosmx_dzi.py      ):
    warp_matrix  : 2x3 affine   (CosMx_thumb -> H&E_thumb    )
    rotation_exact :       (degrees)

 : rotation   90    5         .
      make_cosmx_dzi.py   warp_matrix        
     .
"""

from __future__ import annotations
import sys
import io
import json
import argparse
import numpy as np
import cv2
from pathlib import Path
from PIL import Image

# ── Windows (cp949 ) ──────────────────────
if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding='utf-8', errors='replace')
    except AttributeError:
        pass

Image.MAX_IMAGE_PIXELS = None

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

PROC_SIZE = 1024   # make_cosmx_dzi.py


# ══════════════════════════════════════════════════════════════════════════════
#
# ══════════════════════════════════════════════════════════════════════════════

def _load_gray(path: Path, max_size: int = PROC_SIZE):
    """Load a grayscale thumbnail without decoding full-resolution OME-TIFF when possible."""
    path = Path(path)
    try:
        from openslide import OpenSlide
        slide = OpenSlide(str(path))
        orig = slide.dimensions
        img = slide.get_thumbnail((max_size, max_size)).convert('RGB')
        slide.close()
        thumb = img.size
        gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        return gray, orig, thumb
    except Exception:
        pass

    try:
        import pyvips
        src = pyvips.Image.new_from_file(str(path), access='sequential')
        orig = (int(src.width), int(src.height))
        img = pyvips.Image.thumbnail(str(path), max_size, height=max_size, size='down')
        if img.bands == 1:
            mem = img.write_to_memory()
            arr = np.ndarray(buffer=mem, dtype=np.uint8, shape=[img.height, img.width])
            return arr.copy(), orig, (img.width, img.height)
        if img.bands > 3:
            img = img.extract_band(0, n=3)
        mem = img.write_to_memory()
        arr = np.ndarray(buffer=mem, dtype=np.uint8, shape=[img.height, img.width, img.bands])
        gray = cv2.cvtColor(arr[:, :, :3].copy(), cv2.COLOR_RGB2GRAY)
        return gray, orig, (img.width, img.height)
    except Exception:
        pass

    img = Image.open(str(path))
    if img.mode == 'RGBA':
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    orig = img.size
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    thumb = img.size
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY), orig, thumb
def _compute_rigid(src_pts, dst_pts):
    """
    SVD   Rigid Body  (rotation + translation, no scale change).
    src -> dst     3x3 homogeneous    .
    """
    src = np.array(src_pts, dtype=np.float64)
    dst = np.array(dst_pts, dtype=np.float64)

    src_c = src.mean(axis=0)
    dst_c = dst.mean(axis=0)

    H = (src - src_c).T @ (dst - dst_c)
    U, _, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T

    # reflection (det < 0 = )
    if np.linalg.det(R) < 0:
        Vt[-1, :] *= -1
        R = Vt.T @ U.T

    t = dst_c - R @ src_c

    M = np.eye(3, dtype=np.float64)
    M[:2, :2] = R
    M[:2, 2]  = t
    return M, len(src_pts)


def _compute_affine(src_pts, dst_pts):
    """
    Similarity transform  (rotation + isotropic scale + translation).
    OpenCV estimateAffinePartial2D + RANSAC  .
    Returns: (M 3x3, inlier_count)
    """
    src = np.array(src_pts, dtype=np.float32)
    dst = np.array(dst_pts, dtype=np.float32)

    M23, inliers = cv2.estimateAffinePartial2D(
        src, dst,
        method=cv2.RANSAC,
        ransacReprojThreshold=5.0,
        maxIters=2000,
        confidence=0.99,
    )
    if M23 is None:
        raise RuntimeError(
            'estimateAffinePartial2D failed — try adding more anchor pairs')

    M = np.eye(3, dtype=np.float64)
    M[:2] = M23.astype(np.float64)
    n_inliers = int(inliers.sum()) if inliers is not None else len(src_pts)
    return M, n_inliers


def _decompose(M):
    """
    2D similarity/rigid   (scale, angle_deg, tx, ty)    .
    M: 3x3 homogeneous  (src -> dst)
    """
    a = float(M[0, 0])
    b = float(M[0, 1])
    scale     = float(np.sqrt(a**2 + b**2))
    angle_deg = float(np.degrees(np.arctan2(b, a)))
    if angle_deg < 0:
        angle_deg += 360.0
    tx = float(M[0, 2])
    ty = float(M[1, 2])
    return scale, angle_deg, tx, ty


def _nearest_90(angle_deg: float) -> int:
    """    90     .  : 0 / 90 / 180 / 270"""
    return int(round(angle_deg / 90.0) % 4) * 90


def _reprojection_error(M, src_pts, dst_pts) -> float:
    """      ( )."""
    src = np.array(src_pts, dtype=np.float64)
    dst = np.array(dst_pts, dtype=np.float64)
    n   = len(src)
    if n == 0:
        return 0.0
    src_h = np.hstack([src, np.ones((n, 1))])
    proj  = (M @ src_h.T).T[:, :2]
    return float(np.linalg.norm(proj - dst, axis=1).mean())



def _compute_scale_translate(src_pts, dst_pts):
    """
    Compute isotropic scale + translation only, with no residual rotation.

    This is used for manual anchors after the user has already rotated/flipped
    the CosMx thumbnail in the anchor UI. In that case the remaining mapping
    should be approximately:
        H&E_thumb = scale * CosMx_oriented_thumb + [tx, ty]
    """
    src = np.array(src_pts, dtype=np.float64)
    dst = np.array(dst_pts, dtype=np.float64)
    src_c = src.mean(axis=0)
    dst_c = dst.mean(axis=0)
    src0 = src - src_c
    dst0 = dst - dst_c
    denom = float((src0 * src0).sum())
    if denom <= 1e-9:
        raise RuntimeError('Anchor points are degenerate; spread anchors over the tissue.')
    scale = float((src0 * dst0).sum() / denom)
    if not np.isfinite(scale) or scale <= 0:
        raise RuntimeError(f'Invalid scale estimated from anchors: {scale}')
    t = dst_c - scale * src_c
    M = np.eye(3, dtype=np.float64)
    M[0, 0] = scale
    M[1, 1] = scale
    M[0, 2] = t[0]
    M[1, 2] = t[1]
    return M, len(src_pts)


# ══════════════════════════════════════════════════════════════════════════════
# Auto : SIFT feature matching
# ══════════════════════════════════════════════════════════════════════════════

def _auto_match(he_gray, cosmx_gray, max_pairs: int = 60, log=print):
    """
    SIFT feature matching (CosMx_thumb -> H&E_thumb  ).
    Returns: (cosmx_pts, he_pts)
    """
    sift = cv2.SIFT_create(nfeatures=3000, contrastThreshold=0.03)

    kp_cx, des_cx = sift.detectAndCompute(cosmx_gray, None)
    kp_he, des_he = sift.detectAndCompute(he_gray,    None)
    log(f'[SIFT] CosMx keypoints: {len(kp_cx)},  H&E keypoints: {len(kp_he)}')

    if len(kp_cx) < 4 or len(kp_he) < 4:
        raise RuntimeError(
            f'Too few keypoints (CosMx={len(kp_cx)}, H&E={len(kp_he)}). '
            f'Try semi-auto mode with manual anchors.')

    FLANN_INDEX_KDTREE = 1
    flann   = cv2.FlannBasedMatcher(
        dict(algorithm=FLANN_INDEX_KDTREE, trees=5),
        dict(checks=50),
    )
    matches = flann.knnMatch(des_cx, des_he, k=2)

    # Lowe's ratio test
    good = [m for m, n in matches if m.distance < 0.75 * n.distance]
    log(f'[SIFT] Good matches after ratio test: {len(good)}')

    if len(good) < 4:
        raise RuntimeError(
            f'Not enough SIFT matches ({len(good)} < 4). '
            f'Try semi-auto mode with manual anchors.')

    good = good[:max_pairs]
    cosmx_pts = [[kp_cx[m.queryIdx].pt[0], kp_cx[m.queryIdx].pt[1]] for m in good]
    he_pts    = [[kp_he[m.trainIdx].pt[0], kp_he[m.trainIdx].pt[1]] for m in good]
    return cosmx_pts, he_pts


# ══════════════════════════════════════════════════════════════════════════════
# run()
# ══════════════════════════════════════════════════════════════════════════════

def run(slide_id:       str,
        data_dir:       str,
        transform_type: str  = 'rigid',
        mode:           str  = 'auto',
        anchors:        dict = None,
        log_cb=None):
    """
    app.py     import  .

    Parameters
    ----------
    slide_id       :   ID (  stem)
    data_dir       : Pathogene     (Documents/Pathogene)
    transform_type : 'rigid' | 'affine'
    mode           : 'auto'  | 'manual'
    anchors        : manual    .
                     {
                       'src': [[x, y], ...],  # H&E thumbnail    
                       'dst': [[x, y], ...],  # CosMx thumbnail/oriented-thumbnail    
                     }
    log_cb         :     (  print    )
    """

    def log(msg: str):
        msg = str(msg)
        try:
            print(msg)
        except UnicodeEncodeError:
            # stdout ascii (cp949 )
            print(msg.encode('ascii', errors='replace').decode())
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    data          = Path(data_dir)
    tiles_he_dir  = data / 'tiles'       / slide_id
    cosmx_dir     = data / 'cosmx'
    cosmx_tiles   = data / 'cosmx_tiles' / slide_id
    cosmx_tiles.mkdir(parents=True, exist_ok=True)

    log(f'[Anchors] slide={slide_id}  type={transform_type}  mode={mode}')

    # ── ─────────────────────────────────────────────────

    # H&E: thumbnail_scale.json (app.py._make_he_thumbnail )
    scale_json = tiles_he_dir / 'thumbnail_scale.json'
    if scale_json.exists():
        sj = json.loads(scale_json.read_text(encoding='utf-8'))
        he_orig_w  = sj['slide_w']
        he_orig_h  = sj['slide_h']
        he_thumb_w = sj['thumb_w']
        he_thumb_h = sj['thumb_h']
    else:
        # fallback: OpenSlide
        log('[Warn] thumbnail_scale.json not found — reading H&E size via OpenSlide')
        slides_dir = data / 'slides'
        he_orig_w  = he_orig_h = PROC_SIZE
        he_thumb_w = he_thumb_h = PROC_SIZE
        for ext in ('.svs', '.tif', '.tiff', '.ndpi', '.scn'):
            c = slides_dir / f'{slide_id}{ext}'
            if c.exists():
                try:
                    from openslide import OpenSlide
                    sl = OpenSlide(str(c))
                    he_orig_w, he_orig_h = sl.dimensions
                    sl.close()
                    ts = min(PROC_SIZE / he_orig_w, PROC_SIZE / he_orig_h)
                    he_thumb_w = int(he_orig_w * ts)
                    he_thumb_h = int(he_orig_h * ts)
                except Exception as e:
                    log(f'[Warn] OpenSlide failed: {e}')
                break

    log(f'[Size] H&E  orig={he_orig_w}x{he_orig_h}  thumb={he_thumb_w}x{he_thumb_h}')

    # CosMx: PIL
    cosmx_path = _find_cosmx_file(cosmx_dir, slide_id)
    if cosmx_path is None:
        raise FileNotFoundError(f'CosMx image not found for {slide_id}')

    with Image.open(str(cosmx_path)) as cx_pil:
        cosmx_orig_w, cosmx_orig_h = cx_pil.size

    log(f'[Size] CosMx orig={cosmx_orig_w}x{cosmx_orig_h}')

    # CosMx thumbnail scale factor (full-res -> proc_size )
    # make_cosmx_dzi
    cx_ts = min(PROC_SIZE / cosmx_orig_w, PROC_SIZE / cosmx_orig_h)

    # ── H&E ───────────────────────────────────────────────────────

    he_thumb_path = tiles_he_dir / 'thumbnail.jpg'
    if not he_thumb_path.exists():
        raise FileNotFoundError(
            f'H&E thumbnail not found: {he_thumb_path}. '
            f'Run H&E tiling step first.')

    he_gray, _, _ = _load_gray(he_thumb_path, max_size=PROC_SIZE)
    log(f'[Load] H&E thumbnail: {he_gray.shape[1]}x{he_gray.shape[0]}')

    # ── Anchor ────────────────────────────────────────────────────

    if mode == 'auto':
        # CosMx proc_size
        cx_gray, _, _ = _load_gray(cosmx_path, max_size=PROC_SIZE)
        log(f'[Load] CosMx thumbnail: {cx_gray.shape[1]}x{cx_gray.shape[0]}')

        log('[Auto] SIFT feature matching...')
        # : (cosmx_thumb_pts, he_thumb_pts)
        cosmx_pts, he_pts = _auto_match(cx_gray, he_gray, log=log)
        log(f'[Auto] {len(cosmx_pts)} anchor pairs detected')

    else:   # manual
        if anchors is None or not anchors.get('src'):
            raise ValueError('Manual mode requires anchors dict with src/dst lists')

        he_pts_raw    = anchors['src']   # H&E thumbnail pixel coords
        cosmx_pts_raw = anchors['dst']   # CosMx displayed thumbnail pixel coords
        orientation   = anchors.get('orientation') or {}
        manual_rotation = int(orientation.get('rotation', 0) or 0) % 360
        manual_rotation = int(round(manual_rotation / 90.0) % 4) * 90
        manual_flip_x = bool(orientation.get('flipX', False))
        manual_flip_y = bool(orientation.get('flipY', False))
        manual_oriented = orientation.get('coord_space') == 'oriented_cosmx_thumbnail'

        n = len(he_pts_raw)
        # After manual rotate/flip, the remaining transform is scale + translation.
        # Two pairs are mathematically enough, but four or more are better.
        min_needed = 2 if manual_oriented else (4 if transform_type == 'affine' else 2)
        if n < min_needed:
            raise ValueError(
                f'Need at least {min_needed} anchor pairs for {transform_type}, got {n}')
        log(f'[Manual] {n} anchor pairs received')

        cosmx_pts = cosmx_pts_raw
        he_pts    = he_pts_raw
        if manual_oriented:
            log(f'[Manual] User orientation: rotation={manual_rotation} flipX={manual_flip_x} flipY={manual_flip_y}')
            log('[Manual] Using oriented CosMx thumbnail coordinates directly')
        else:
            log('[Manual] Using thumbnail-space anchors directly: CosMx_thumb -> H&E_thumb')

        # Basic coordinate-range diagnostics. These warnings do not stop the run.
        he_bad = [p for p in he_pts if p[0] < -5 or p[1] < -5 or p[0] > he_thumb_w + 5 or p[1] > he_thumb_h + 5]
        cx_thumb_w = max(1, int(round(cosmx_orig_w * cx_ts)))
        cx_thumb_h = max(1, int(round(cosmx_orig_h * cx_ts)))
        if manual_oriented and manual_rotation in (90, 270):
            cx_bound_w, cx_bound_h = cx_thumb_h, cx_thumb_w
        else:
            cx_bound_w, cx_bound_h = cx_thumb_w, cx_thumb_h
        cx_bad = [p for p in cosmx_pts if p[0] < -5 or p[1] < -5 or p[0] > cx_bound_w + 5 or p[1] > cx_bound_h + 5]
        if he_bad:
            log(f'[WARN] {len(he_bad)} H&E anchors are outside thumbnail bounds {he_thumb_w}x{he_thumb_h}')
        if cx_bad:
            log(f'[WARN] {len(cx_bad)} CosMx anchors are outside oriented thumbnail bounds {cx_bound_w}x{cx_bound_h}')

    # ── (CosMx_thumb/oriented_thumb -> H&E_thumb) ──────────────

    log(f'[Compute] transform_type={transform_type}  n_pairs={len(cosmx_pts)}')

    manual_oriented = (mode != 'auto') and bool((anchors or {}).get('orientation', {}).get('coord_space') == 'oriented_cosmx_thumbnail')
    if manual_oriented:
        M, n_inliers = _compute_scale_translate(cosmx_pts, he_pts)
        log('[Manual] Solved scale + translation after user rotation/flip')
    elif transform_type == 'affine':
        M, n_inliers = _compute_affine(cosmx_pts, he_pts)
        log(f'[Affine] RANSAC inliers: {n_inliers}/{len(cosmx_pts)}')
    else:   # rigid
        M, n_inliers = _compute_rigid(cosmx_pts, he_pts)
        log('[Rigid] SVD solution computed')

    scale_val, rot_exact, tx, ty = _decompose(M)
    if manual_oriented:
        rot_nearest = manual_rotation
        out_flip_x = manual_flip_x
        out_flip_y = manual_flip_y
        rot_for_log = manual_rotation
    else:
        rot_nearest = _nearest_90(rot_exact)
        out_flip_x = False
        out_flip_y = False
        rot_for_log = rot_exact
    reproj_err  = _reprojection_error(M, cosmx_pts, he_pts)

    log(f'[Result] scale={scale_val:.4f}  rot={rot_for_log:.1f}deg (stored={rot_nearest})')
    log(f'[Result] flipX={out_flip_x} flipY={out_flip_y} tx={tx:.1f}  ty={ty:.1f}  reprojection_error={reproj_err:.2f}px')

    # ── translateX_pixels ──────────────────────────────────────────────
    # tx/ty thumbnail
    # make_cosmx_dzi PROC_SIZE=1024
    # he_thumb proc_size tx = translateX_pixels ( )
    he_ts = min(PROC_SIZE / he_orig_w, PROC_SIZE / he_orig_h)  # H&E thumb scale
    # tx H&E thumb proc_size tx
    # (thumbnail min(PROC_SIZE/he_orig_w, PROC_SIZE/he_orig_h) )
    tx_pixels = round(tx, 2)
    ty_pixels = round(ty, 2)

    # ── JSON ─────────────────────────────────────────────────────────────

    result = {
        'version':  f'anchor_{transform_type}_thumb_manual_orientation_v3' if mode != 'auto' else f'anchor_{transform_type}_auto_v3',
        'method':   f'anchor_{transform_type}_{mode}',
        'slide_id': slide_id,

        # make_cosmx_dzi.py
        'transform': {
            'rotation':          rot_nearest,   # 90
            'flipX':             out_flip_x,
            'flipY':             out_flip_y,
            'scale':             round(scale_val, 6),
            'translateX_pixels': tx_pixels,
            'translateY_pixels': ty_pixels,
            'translateX':        round(tx_pixels / (he_thumb_w or PROC_SIZE), 6),
            'translateY':        round(ty_pixels / (he_thumb_h or PROC_SIZE), 6),
        },

        # ( make_cosmx_dzi.py warp_matrix )
        'warp_matrix':    None if manual_oriented else M[:2].tolist(),   # disabled for manual orientation; simple transform is exact
        'rotation_exact': round(rot_exact, 4),

        # (make_cosmx_dzi.compute_full_res_params )
        'original_sizes': {
            'he':    [he_orig_w, he_orig_h],
            'cosmx': [cosmx_orig_w, cosmx_orig_h],
        },

        #
        'anchor_registration': {
            'transform_type':        transform_type,
            'mode':                  mode,
            'n_pairs':               len(cosmx_pts),
            'inliers':               n_inliers,
            'reprojection_error_px': round(reproj_err, 3),
        },

        # make_cosmx_dzi detection.processing_size
        'detection': {
            'processing_size': PROC_SIZE,
            'combined_score':  max(0.0, 1.0 - reproj_err / PROC_SIZE),
        },
    }

    out_path = cosmx_tiles / 'transform_registered.json'
    out_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=True),
        encoding='utf-8',
    )
    log(f'[OK] Saved: {out_path}')

    # : rotation 90
    rot_diff = min(abs(rot_exact - rot_nearest),
                   360 - abs(rot_exact - rot_nearest))
    if (not manual_oriented) and rot_diff > 5:
        log(f'[WARN] rotation {rot_exact:.1f}deg rounded to {rot_nearest}deg '
            f'(diff={rot_diff:.1f}deg)')
        log('[WARN] Update make_cosmx_dzi.py to use warp_matrix for precise registration')

    return result


# ══════════════════════════════════════════════════════════════════════════════
#
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Anchor-based CosMx-H&E Registration')
    parser.add_argument('--slide-id',       required=True,
                        help='  ID (  stem)')
    parser.add_argument('--data-dir',
                        default=str(Path.home() / 'Documents' / 'Pathogene'),
                        help='Pathogene    ')
    parser.add_argument('--transform-type', choices=['rigid', 'affine'],
                        default='rigid',   help='   ')
    parser.add_argument('--mode',           choices=['auto', 'manual'],
                        default='auto',    help='Registration   (manual   app.py  )')
    args = parser.parse_args()

    print('=' * 60)
    print('Anchor Registration')
    print('=' * 60)
    try:
        run(
            slide_id=args.slide_id,
            data_dir=args.data_dir,
            transform_type=args.transform_type,
            mode=args.mode,
        )
    except Exception as e:
        import traceback
        print(f'[ERROR] {e}')
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()