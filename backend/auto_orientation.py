"""
Auto Orientation Detection for CosMx-H&E Registration (V8.1)
V8   +        

V8.1   (V8      ):
  1. [phase_correlation] (0,0) fallback   →   template matching   
     - dx==0 and dy==0 and iou < 0.4 → 'phase_zero_fallback'  
     - _run_all_orientations          template matching  

  2. [dual-mode  ] threshold  
     -    : partial_best > best + 0.03 → partial_best > best - 0.01
       (partial       ,   partial  )
     - trigger threshold: score < 0.35 → score < 0.45
       (0.35~0.45     full score   )

  3. V8      :
     - scale bonus (effective_score, combined_score)
     - fiducial  
     - NCC tiebreaker
     - dual-mode  
"""

import numpy as np
import cv2
import json
from pathlib import Path
import argparse
from PIL import Image
import sys
import io

# Windows encoding fix removed (handled by launcher)

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



# ============================================================================
# IMAGE LOADING
# ============================================================================

def load_he_image(he_path, max_size=1024):
    """Load a small H&E preview efficiently from SVS or pyramidal TIFF/OME-TIFF."""
    he_path = Path(he_path)
    print(f"  [Load] H&E: {he_path.name}")

    try:
        from openslide import OpenSlide
        slide = OpenSlide(str(he_path))
        w, h = slide.dimensions
        thumb = slide.get_thumbnail((max_size, max_size)).convert('RGB')
        slide.close()
        return cv2.cvtColor(np.array(thumb), cv2.COLOR_RGB2BGR), (w, h)
    except Exception:
        pass

    try:
        import pyvips
        src = pyvips.Image.new_from_file(str(he_path), access='sequential')
        orig_size = (int(src.width), int(src.height))
        img = pyvips.Image.thumbnail(str(he_path), max_size, height=max_size, size='down')
        try:
            if img.hasalpha():
                img = img.flatten(background=[255, 255, 255])
        except Exception:
            pass
        if img.bands == 1:
            img = img.colourspace('srgb')
        if img.bands > 3:
            img = img.extract_band(0, n=3)
        mem = img.write_to_memory()
        arr = np.ndarray(buffer=mem, dtype=np.uint8, shape=[img.height, img.width, img.bands])
        if img.bands == 1:
            arr = np.repeat(arr, 3, axis=2)
        return cv2.cvtColor(arr[:, :, :3].copy(), cv2.COLOR_RGB2BGR), orig_size
    except Exception:
        pass

    pil_img = Image.open(str(he_path))
    orig_size = pil_img.size
    if pil_img.mode != 'RGB':
        pil_img = pil_img.convert('RGB')
    pil_img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR), orig_size


def load_cosmx_image(cosmx_path, max_size=1024):
    """Load a small CosMx RGB preview efficiently, including pyramidal OME-TIFF."""
    cosmx_path = Path(cosmx_path)
    print(f"  [Load] CosMx: {cosmx_path.name}")

    try:
        from openslide import OpenSlide
        slide = OpenSlide(str(cosmx_path))
        w, h = slide.dimensions
        thumb = slide.get_thumbnail((max_size, max_size)).convert('RGB')
        slide.close()
        return cv2.cvtColor(np.array(thumb), cv2.COLOR_RGB2BGR), (w, h)
    except Exception:
        pass

    try:
        import pyvips
        src = pyvips.Image.new_from_file(str(cosmx_path), access='sequential')
        orig_size = (int(src.width), int(src.height))
        img = pyvips.Image.thumbnail(str(cosmx_path), max_size, height=max_size, size='down')
        try:
            if img.hasalpha():
                img = img.flatten(background=[255, 255, 255])
        except Exception:
            pass
        if img.bands == 1:
            img = img.colourspace('srgb')
        if img.bands > 3:
            img = img.extract_band(0, n=3)
        mem = img.write_to_memory()
        arr = np.ndarray(buffer=mem, dtype=np.uint8, shape=[img.height, img.width, img.bands])
        if img.bands == 1:
            arr = np.repeat(arr, 3, axis=2)
        return cv2.cvtColor(arr[:, :, :3].copy(), cv2.COLOR_RGB2BGR), orig_size
    except Exception:
        pass

    pil_img = Image.open(str(cosmx_path))
    orig_size = pil_img.size
    if pil_img.mode == 'RGBA':
        bg = Image.new('RGB', pil_img.size, (255, 255, 255))
        bg.paste(pil_img, mask=pil_img.split()[3])
        pil_img = bg
    elif pil_img.mode != 'RGB':
        pil_img = pil_img.convert('RGB')
    pil_img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR), orig_size


# ============================================================================
# MASK GENERATION
# ============================================================================

def _remove_fiducial_blobs(mask, img_h, img_w, max_area_ratio=0.003, min_aspect=0.6):
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    clean    = np.zeros_like(mask)
    img_area = img_h * img_w
    for i in range(1, n_labels):
        area      = stats[i, cv2.CC_STAT_AREA]
        bw        = stats[i, cv2.CC_STAT_WIDTH]
        bh        = stats[i, cv2.CC_STAT_HEIGHT]
        if bw == 0 or bh == 0:
            continue
        aspect      = min(bw, bh) / max(bw, bh)
        solidity    = area / (bw * bh)
        area_ratio  = area / img_area
        is_fiducial = (area_ratio < max_area_ratio and aspect > min_aspect
                       and solidity > 0.65 and area > 10)
        if not is_fiducial:
            clean[labels == i] = 255
    return clean


def _remove_rectangular_border(mask, min_fill=0.55, max_thickness=30):
    """
    ✅ V8.1:            
    connected component          .
    → edge strip fill_rate    1px       .
    """
    h, w = mask.shape
    for t in range(3, max_thickness + 1):
        if 2 * t >= h or 2 * t >= w:
            break
        top = (mask[t - 1, t:-t] > 0).mean()
        bot = (mask[-t,   t:-t] > 0).mean()
        lft = (mask[t:-t, t - 1] > 0).mean()
        rgt = (mask[t:-t, -t  ] > 0).mean()
        if top > min_fill and bot > min_fill and lft > min_fill and rgt > min_fill:
            cleaned = mask.copy()
            cleaned[:t,  :] = 0
            cleaned[-t:, :] = 0
            cleaned[:, :t ] = 0
            cleaned[:, -t:] = 0
            print(f"    [Border] Rectangular frame removed (thickness={t}px)")
            return cleaned
    return mask


def create_he_mask(img):
    gray     = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, raw   = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY_INV)
    h_img, w_img = raw.shape

    # ✅ V8.1: connected component
    raw = _remove_rectangular_border(raw)

    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(raw, connectivity=8)
    clean_raw = np.zeros_like(raw)
    for i in range(1, n_labels):
        area      = stats[i, cv2.CC_STAT_AREA]
        bx        = stats[i, cv2.CC_STAT_LEFT]
        by        = stats[i, cv2.CC_STAT_TOP]
        bw        = stats[i, cv2.CC_STAT_WIDTH]
        bh        = stats[i, cv2.CC_STAT_HEIGHT]
        bbox_area = bw * bh
        if bbox_area == 0:
            continue
        solid_ratio    = area / bbox_area
        touches_border = bx <= 3 or by <= 3 or bx+bw >= w_img-3 or by+bh >= h_img-3
        large_bbox     = bw > w_img * 0.5 and bh > h_img * 0.5
        if touches_border and large_bbox and solid_ratio < 0.25:
            continue
        clean_raw[labels == i] = 255

    clean_raw = _remove_fiducial_blobs(clean_raw, h_img, w_img)
    k         = np.ones((5, 5), np.uint8)
    mask      = cv2.morphologyEx(clean_raw, cv2.MORPH_OPEN,  k)
    mask      = cv2.morphologyEx(mask,      cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    return mask


def create_cosmx_mask(img, dilate_iterations=3):
    gray       = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    saturation = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1]
    raw_mask   = (((gray < 250) & (gray > 5)) | (saturation > 20)).astype(np.uint8) * 255
    h_img, w_img = raw_mask.shape

    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(raw_mask, connectivity=8)
    clean_raw = np.zeros_like(raw_mask)
    for i in range(1, n_labels):
        area      = stats[i, cv2.CC_STAT_AREA]
        bx        = stats[i, cv2.CC_STAT_LEFT]
        by        = stats[i, cv2.CC_STAT_TOP]
        bw        = stats[i, cv2.CC_STAT_WIDTH]
        bh        = stats[i, cv2.CC_STAT_HEIGHT]
        bbox_area = bw * bh
        if bbox_area == 0:
            continue
        solid_ratio    = area / bbox_area
        touches_border = bx <= 3 or by <= 3 or bx+bw >= w_img-3 or by+bh >= h_img-3
        large_bbox     = bw > w_img * 0.5 and bh > h_img * 0.5
        if touches_border and large_bbox and solid_ratio < 0.25:
            continue
        clean_raw[labels == i] = 255

    clean_raw = _remove_fiducial_blobs(clean_raw, h_img, w_img)
    kernel    = np.ones((7, 7), np.uint8)
    mask      = cv2.dilate(clean_raw, kernel, iterations=dilate_iterations)
    mask      = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    return mask


# ============================================================================
# COVERAGE DETECTION
# ============================================================================

def estimate_coverage_ratio(he_mask, cosmx_mask):
    he_area    = (he_mask > 0).sum()
    cosmx_area = (cosmx_mask > 0).sum()
    if he_area == 0:
        return 0, 'unknown'

    he_h, he_w       = he_mask.shape
    cosmx_h, cosmx_w = cosmx_mask.shape
    he_density        = he_area / (he_h * he_w)
    cosmx_density     = cosmx_area / (cosmx_h * cosmx_w)
    img_size_ratio    = (cosmx_h * cosmx_w) / (he_h * he_w)
    tissue_ratio      = cosmx_area / he_area

    print(f"    H&E:   {he_w}x{he_h},    tissue={he_area} px ({he_density*100:.1f}%)")
    print(f"    CosMx: {cosmx_w}x{cosmx_h}, tissue={cosmx_area} px ({cosmx_density*100:.1f}%)")
    print(f"    Image size ratio: {img_size_ratio:.2f}  Tissue area ratio: {tissue_ratio:.2f}")

    if img_size_ratio < 0.35:
        mode, reason = 'partial', "CosMx image much smaller"
    elif tissue_ratio > 2.0:
        mode, reason = 'partial', "CosMx tissue > H&E tissue"
    elif tissue_ratio < 0.25:
        mode, reason = 'partial', "CosMx tissue much smaller"
    else:
        mode, reason = 'full', "Similar coverage"

    print(f"    Decision: {mode.upper()} ({reason})")
    return tissue_ratio, mode


# ============================================================================
# TRANSFORMATION
# ============================================================================

def apply_transform(img, rotation, flip_x, flip_y):
    result = img.copy()
    k = rotation // 90
    if k > 0:
        result = np.rot90(result, k=k)
    if flip_x:
        result = np.fliplr(result)
    if flip_y:
        result = np.flipud(result)
    return np.ascontiguousarray(result)


def translate_image(img, dx, dy):
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, M, (img.shape[1], img.shape[0]))


# ============================================================================
# FULL MATCHING (Phase Correlation)
# ============================================================================

def phase_correlation_match(he_mask, cosmx_mask_transformed):
    he_h, he_w    = he_mask.shape
    cosmx_resized = cv2.resize(cosmx_mask_transformed, (he_w, he_h),
                               interpolation=cv2.INTER_AREA)
    f1 = (np.float32(he_mask)    - he_mask.mean())    / (he_mask.std()    + 1e-6)
    f2 = (np.float32(cosmx_resized) - cosmx_resized.mean()) / (cosmx_resized.std() + 1e-6)

    shift, _ = cv2.phaseCorrelate(f1, f2)
    dx, dy   = int(round(shift[0])), int(round(shift[1]))
    aligned  = translate_image(cosmx_resized, dx, dy)

    inter = np.logical_and(he_mask > 0, aligned > 0).sum()
    union = np.logical_or(he_mask  > 0, aligned > 0).sum()
    iou   = inter / (union + 1e-6)

    if dx < -he_w // 4 or dy < -he_h // 4:
        inter_c = np.logical_and(he_mask > 0, cosmx_resized > 0).sum()
        union_c = np.logical_or(he_mask  > 0, cosmx_resized > 0).sum()
        iou_c   = inter_c / (union_c + 1e-6)
        if iou_c > iou:
            dx, dy = 0, 0
            iou    = iou_c

    # ✅ V8.1: (0,0) fallback
    is_zero_fallback = (dx == 0 and dy == 0 and iou < 0.40)

    return {
        'dx': dx, 'dy': dy,
        'norm_dx': dx / he_w, 'norm_dy': dy / he_h,
        'score': iou, 'scale': 1.0,
        'method': 'phase_zero_fallback' if is_zero_fallback else 'phase_correlation'
    }


# ============================================================================
# PARTIAL MATCHING — V8
# ============================================================================

def template_matching_multiscale(he_mask, cosmx_mask_transformed, scales=None):
    SCALE_BONUS = 0.12
    SCALE_MIN   = 0.5
    SCALE_MAX   = 1.15

    if scales is None:
        scales = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85,
                  0.9, 0.95, 1.0, 1.05, 1.1, 1.15]

    he_h, he_w       = he_mask.shape
    cosmx_h, cosmx_w = cosmx_mask_transformed.shape
    best_eff         = -1
    best_result      = None

    for scale in scales:
        new_w = int(cosmx_w * scale)
        new_h = int(cosmx_h * scale)
        if new_w >= he_w or new_h >= he_h:
            continue
        if new_w < 20 or new_h < 20:
            continue

        scaled_template = cv2.resize(cosmx_mask_transformed, (new_w, new_h),
                                     interpolation=cv2.INTER_AREA)
        try:
            result             = cv2.matchTemplate(he_mask, scaled_template,
                                                   cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            scale_norm = max(0.0, min(1.0, (scale - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)))
            eff_score  = max_val + SCALE_BONUS * scale_norm

            if eff_score > best_eff:
                best_eff    = eff_score
                best_result = {
                    'dx': max_loc[0], 'dy': max_loc[1],
                    'norm_dx': max_loc[0] / he_w, 'norm_dy': max_loc[1] / he_h,
                    'scale': scale, 'score': max_val, 'eff_score': eff_score,
                    'method': 'template_matching'
                }
        except cv2.error:
            continue

    if best_result is None:
        return {'dx': 0, 'dy': 0, 'norm_dx': 0, 'norm_dy': 0,
                'scale': 1.0, 'score': 0, 'method': 'template_matching_failed'}
    return best_result


def compute_coverage_ratio(he_mask, cosmx_mask):
    """  tissue     (partial vs full  )"""
    return float((cosmx_mask > 0).sum()) / float((he_mask > 0).sum() + 1e-6)


def compute_overlap_score(he_mask, cosmx_mask_transformed, location, scale,
                          coverage_ratio=1.0):
    """
    ✅ V8.2  : coverage_ratio    IoU ↔ Precision    
    - coverage_ratio < 0.4 (partial): Precision = inter / cosmx_area
      → "CosMx  HE        " → HE          
    - coverage_ratio > 0.7 (full):    IoU = inter / union
    -  :    
    """
    he_h, he_w       = he_mask.shape
    cosmx_h, cosmx_w = cosmx_mask_transformed.shape
    new_w = int(cosmx_w * scale); new_h = int(cosmx_h * scale)
    if new_w < 1 or new_h < 1:
        return 0.0

    scaled = cv2.resize(cosmx_mask_transformed, (new_w, new_h), interpolation=cv2.INTER_AREA)
    x, y   = location
    dst_x1 = max(0, x);            dst_y1 = max(0, y)
    dst_x2 = min(he_w, x + new_w); dst_y2 = min(he_h, y + new_h)
    src_x1 = max(0, -x);           src_y1 = max(0, -y)
    rh = dst_y2 - dst_y1; rw = dst_x2 - dst_x1
    if rh <= 0 or rw <= 0:
        return 0.0
    if src_y1 + rh > scaled.shape[0] or src_x1 + rw > scaled.shape[1]:
        return 0.0

    he_r  = he_mask[dst_y1:dst_y2, dst_x1:dst_x2]
    cx_r  = scaled[src_y1:src_y1 + rh, src_x1:src_x1 + rw]
    inter = float(np.logical_and(he_r > 0, cx_r > 0).sum())
    union = float(np.logical_or(he_r  > 0, cx_r > 0).sum())
    cx_area = float((cx_r > 0).sum())

    iou       = inter / (union   + 1e-6)
    precision = inter / (cx_area + 1e-6)   # CosMx HE

    # coverage
    if coverage_ratio < 0.4:
        alpha = 0.0          # precision
    elif coverage_ratio > 0.7:
        alpha = 1.0          # IoU
    else:
        alpha = (coverage_ratio - 0.4) / 0.3

    return alpha * iou + (1.0 - alpha) * precision


# : compute_iou_at_location
def compute_iou_at_location(he_mask, cosmx_mask_transformed, location, scale,
                            coverage_ratio=1.0):
    return compute_overlap_score(he_mask, cosmx_mask_transformed, location, scale,
                                 coverage_ratio)


# ============================================================================
# NCC TIEBREAKER
# ============================================================================

def _ncc_score(he_gray, cosmx_gray, rotation, flip_x, flip_y, dx, dy, scale):
    he_h, he_w = he_gray.shape
    he_inv     = (255 - he_gray).astype(np.float32)
    cosmx_t    = apply_transform(cosmx_gray, rotation, flip_x, flip_y)
    if abs(scale - 1.0) > 0.01:
        nw = max(1, int(cosmx_t.shape[1] * scale))
        nh = max(1, int(cosmx_t.shape[0] * scale))
        cosmx_t = cv2.resize(cosmx_t, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((he_h, he_w), dtype=np.float32)
    ch, cw = cosmx_t.shape[:2]
    dx1 = max(0, dx);        dy1 = max(0, dy)
    dx2 = min(he_w, dx + cw); dy2 = min(he_h, dy + ch)
    sx1 = max(0, -dx);       sy1 = max(0, -dy)
    rh  = dy2 - dy1;         rw  = dx2 - dx1
    if rh > 0 and rw > 0 and sy1 + rh <= ch and sx1 + rw <= cw:
        canvas[dy1:dy2, dx1:dx2] = \
            cosmx_t[sy1:sy1 + rh, sx1:sx1 + rw].astype(np.float32)
    tissue = he_inv > 20
    if tissue.sum() < 100:
        return 0.0
    f1 = he_inv[tissue]; f2 = canvas[tissue]
    ncc = ((f1 - f1.mean()) * (f2 - f2.mean())).mean() / \
          ((f1.std() + 1e-6) * (f2.std() + 1e-6))
    return float(ncc)


def ncc_tiebreaker(candidates, he_gray, cosmx_gray, top_n=4):
    sorted_c   = sorted(candidates, key=lambda x: -x['combined_score'])
    top        = sorted_c[:top_n]
    print(f"\n  [NCC Tiebreaker] Top-{top_n}    ...")
    best_final = -999
    best_cand  = sorted_c[0]
    for c in top:
        ncc   = _ncc_score(he_gray, cosmx_gray,
                           c['rotation'], c['flipX'], c['flipY'],
                           c['dx'], c['dy'], c['scale'])
        final = 0.7 * c['combined_score'] + 0.3 * max(ncc, 0.0)
        print(f"    Rot={c['rotation']:>3} FX={str(c['flipX']):<5} FY={str(c['flipY']):<5} "
              f"Score={c['combined_score']:.4f}  NCC={ncc:+.4f}  → {final:.4f}")
        c['ncc_score'] = ncc; c['final_score'] = final
        if final > best_final:
            best_final = final; best_cand = c
    return best_cand


# ============================================================================
# HYBRID MATCHING — V8.1
# ============================================================================

def _run_all_orientations(he_mask, cosmx_mask, match_fn, label="", coverage_ratio=1.0):
    """16     . phase_zero_fallback     template matching  ."""
    candidates = []
    for rotation in [0, 90, 180, 270]:
        for flip_x in [False, True]:
            for flip_y in [False, True]:
                transformed = apply_transform(cosmx_mask, rotation, flip_x, flip_y)
                result      = match_fn(he_mask, transformed)

                # ✅ V8.1: phase (0,0) fallback → template matching
                if result.get('method') == 'phase_zero_fallback':
                    tmpl = template_matching_multiscale(he_mask, transformed)
                    if tmpl['score'] > result['score']:
                        result = tmpl
                        print(f"    [V8.2] Rot={rotation} FX={flip_x} FY={flip_y}: "
                              f"phase(0,0) → template (score {tmpl['score']:.4f})")

                if label == 'partial' and result['score'] > 0:
                    # ✅ V8.2: coverage_ratio → precision/IoU
                    overlap = compute_iou_at_location(
                        he_mask, transformed,
                        (result['dx'], result['dy']), result['scale'],
                        coverage_ratio=coverage_ratio)
                else:
                    overlap = result['score']

                # V8: overlap 50% + precision/IoU 30% + scale 20%
                scale_norm = max(0.0, min(1.0, (result['scale'] - 0.5) / 0.65))
                combined   = 0.50 * result['score'] + 0.30 * overlap + 0.20 * scale_norm

                candidates.append({
                    'rotation': rotation, 'flipX': flip_x, 'flipY': flip_y,
                    'dx': result['dx'], 'dy': result['dy'],
                    'norm_dx': result['norm_dx'], 'norm_dy': result['norm_dy'],
                    'scale': result['scale'],
                    'match_score': result['score'], 'iou_score': overlap,
                    'combined_score': combined, 'method': result['method']
                })
    return candidates


def find_best_alignment_hybrid(he_mask, cosmx_mask, mode='auto',
                               he_gray=None, cosmx_gray=None):
    print("\n[Algorithm] Hybrid Matching V8.2")

    if mode == 'auto':
        ratio, detected_mode = estimate_coverage_ratio(he_mask, cosmx_mask)
    else:
        detected_mode = mode
        print(f"    Forced mode: {detected_mode.upper()}")

    # ✅ V8.2: coverage_ratio → partial Precision
    cov_ratio = compute_coverage_ratio(he_mask, cosmx_mask)
    if cov_ratio < 0.4:
        score_mode = "Precision (partial)"
    elif cov_ratio > 0.7:
        score_mode = "IoU (full)"
    else:
        score_mode = f"Blend (ratio={cov_ratio:.2f})"
    print(f"    Coverage ratio: {cov_ratio:.3f}  →  Scoring: {score_mode}")

    print("\n  [Testing] 16 orientations...")
    print("  " + "-" * 95)
    print("  {:>3} {:>6} {:>6} {:>6} {:>10} {:>10} {:>8} {:>12} {:>12}".format(
        "#", "Rot", "FlipX", "FlipY", "Score", "IoU", "Scale", "Position", "Method"))
    print("  " + "-" * 95)

    if detected_mode == 'full':
        candidates = _run_all_orientations(
            he_mask, cosmx_mask, phase_correlation_match,
            label='full', coverage_ratio=cov_ratio)
    else:
        candidates = _run_all_orientations(
            he_mask, cosmx_mask, template_matching_multiscale,
            label='partial', coverage_ratio=cov_ratio)

    for i, c in enumerate(candidates, 1):
        print("  {:>3} {:>6} {:>6} {:>6} {:>10.4f} {:>10.4f} {:>8.2f} {:>12} {:>12}".format(
            i, f"{c['rotation']}°",
            "Y" if c['flipX'] else "N", "Y" if c['flipY'] else "N",
            c['match_score'], c['iou_score'], c['scale'],
            f"({c['dx']},{c['dy']})", c['method'][:12]))
    print("  " + "-" * 95)

    best = max(candidates, key=lambda x: x['combined_score'])

    # ✅ V8.1: trigger threshold 0.35 → 0.45, margin 0.03 → -0.01
    TRIGGER_THRESHOLD = 0.45   # mode
    SWITCH_MARGIN     = -0.01  # partial (0.01 )

    if detected_mode == 'full' and best['combined_score'] < TRIGGER_THRESHOLD:
        print(f"\n  [V8.1 Dual-mode] Full={best['combined_score']:.4f} < {TRIGGER_THRESHOLD} "
              f"→ running Partial...")
        partial_candidates = _run_all_orientations(
            he_mask, cosmx_mask, template_matching_multiscale,
            label='partial', coverage_ratio=cov_ratio)
        partial_best = max(partial_candidates, key=lambda x: x['combined_score'])

        print(f"    Full best:    {best['combined_score']:.4f}")
        print(f"    Partial best: {partial_best['combined_score']:.4f}")

        if partial_best['combined_score'] > best['combined_score'] + SWITCH_MARGIN:
            print("    → Switching to Partial result")
            candidates    = partial_candidates
            best          = partial_best
            detected_mode = 'partial_v8.1'
        else:
            print("    → Keeping Full result")

    elif detected_mode == 'partial' and best['combined_score'] < TRIGGER_THRESHOLD:
        print(f"\n  [V8.1 Dual-mode] Partial={best['combined_score']:.4f} < {TRIGGER_THRESHOLD} "
              f"→ running Full...")
        full_candidates = _run_all_orientations(
            he_mask, cosmx_mask, phase_correlation_match,
            label='full', coverage_ratio=cov_ratio)
        full_best = max(full_candidates, key=lambda x: x['combined_score'])

        print(f"    Partial best: {best['combined_score']:.4f}")
        print(f"    Full best:    {full_best['combined_score']:.4f}")

        if full_best['combined_score'] > best['combined_score'] + SWITCH_MARGIN:
            print("    → Switching to Full result")
            candidates    = full_candidates
            best          = full_best
            detected_mode = 'full_v8.1'
        else:
            print("    → Keeping Partial result")

    # NCC tiebreaker
    if he_gray is not None and cosmx_gray is not None:
        best = ncc_tiebreaker(candidates, he_gray, cosmx_gray, top_n=4)

    sorted_c = sorted(candidates, key=lambda x: -x['combined_score'])
    print("\n  [Top 3]")
    for i, c in enumerate(sorted_c[:3], 1):
        print(f"    {i}. Rot={c['rotation']}° FX={c['flipX']} FY={c['flipY']} "
              f"Scale={c['scale']:.2f} Score={c['combined_score']:.4f}")

    print(f"\n  [Best] Rot={best['rotation']}° FX={best['flipX']} FY={best['flipY']} "
          f"Scale={best['scale']:.2f} Score={best['combined_score']:.4f}")

    return best, candidates, detected_mode


# ============================================================================
# REFINEMENT
# ============================================================================

def refine_alignment(he_mask, cosmx_mask, best, search_range=30, search_step=3):
    print("\n  [Refining] Local search...")
    transformed = apply_transform(cosmx_mask, best['rotation'], best['flipX'], best['flipY'])
    scale       = best['scale']
    init_dx, init_dy = best['dx'], best['dy']
    best_score  = best['combined_score']
    best_dx, best_dy = init_dx, init_dy

    for dx in range(init_dx - search_range, init_dx + search_range + 1, search_step):
        for dy in range(init_dy - search_range, init_dy + search_range + 1, search_step):
            if dx < 0 or dy < 0:
                continue
            iou = compute_iou_at_location(he_mask, transformed, (dx, dy), scale)
            if iou > best_score:
                best_score = iou; best_dx, best_dy = dx, dy

    print(f"    ({init_dx},{init_dy}) → ({best_dx},{best_dy})  "
          f"score {best['combined_score']:.4f} → {best_score:.4f}")
    return best_dx, best_dy, best_score


# ============================================================================
# PROCESS
# ============================================================================

def process_single_slide(slide_id, data_dir, mode, refine, debug, size, svs_path=None):
    slides_dir      = data_dir / 'slides'
    cosmx_dir       = data_dir / 'cosmx'
    cosmx_tiles_dir = data_dir / 'cosmx_tiles' / slide_id

    # H&E file path
    # In the web app flow, SVS files are NOT copied into data/slides because they
    # can be several GB. Prefer the original local path supplied by app.py.
    he_path = Path(svs_path) if svs_path else None
    if he_path is not None and not he_path.exists():
        print(f"[WARN] supplied H&E path does not exist: {he_path}")
        he_path = None
    if he_path is None:
        he_svs  = slides_dir / f"{slide_id}.svs"
        he_png  = slides_dir / f"{slide_id}.png"
        he_path = he_svs if he_svs.exists() else (he_png if he_png.exists() else None)
    if he_path is None:
        print(f"[SKIP] H&E not found for {slide_id}. svs_path={svs_path}"); return None

    cosmx_path = _find_cosmx_file(cosmx_dir, slide_id)
    if cosmx_path is None:
        print(f"[SKIP] CosMx not found for {slide_id}"); return None

    print(f"\n[Processing] {slide_id}")
    he_img,    he_orig    = load_he_image(he_path, size)
    cosmx_img, cosmx_orig = load_cosmx_image(cosmx_path, size)

    he_mask    = create_he_mask(he_img)
    cosmx_mask = create_cosmx_mask(cosmx_img)
    he_gray    = cv2.cvtColor(he_img,    cv2.COLOR_BGR2GRAY)
    cosmx_gray = cv2.cvtColor(cosmx_img, cv2.COLOR_BGR2GRAY)

    best, candidates, detected_mode = find_best_alignment_hybrid(
        he_mask, cosmx_mask, mode=mode,
        he_gray=he_gray, cosmx_gray=cosmx_gray)

    if refine:
        dx, dy, score = refine_alignment(he_mask, cosmx_mask, best)
        best['dx'] = dx; best['dy'] = dy
        best['norm_dx'] = dx / he_mask.shape[1]
        best['norm_dy'] = dy / he_mask.shape[0]
        best['refined_score'] = score

    he_orig_w, he_orig_h       = he_orig
    cosmx_orig_w, cosmx_orig_h = cosmx_orig
    size_ratio  = (cosmx_orig_w / he_orig_w + cosmx_orig_h / he_orig_h) / 2
    final_scale = best['scale']

    transform_data = {
        "version": "8.2",
        "slide_id": slide_id,
        "method": f"auto_orientation_v8.2_{detected_mode}",
        "algorithm": "hybrid v8.2 (precision/IoU auto-switch + border removal)",
        "coverage_mode": detected_mode,
        "original_sizes": {
            "he": [he_orig_w, he_orig_h],
            "cosmx": [cosmx_orig_w, cosmx_orig_h],
            "size_ratio": size_ratio
        },
        "transform": {
            "rotation":          best['rotation'],
            "flipX":             best['flipX'],
            "flipY":             best['flipY'],
            "translateX":        best['norm_dx'],
            "translateY":        best['norm_dy'],
            "translateX_pixels": best['dx'],
            "translateY_pixels": best['dy'],
            "scale":             final_scale
        },
        "detection": {
            "combined_score": best['combined_score'],
            "match_score":    best['match_score'],
            "iou_score":      best['iou_score'],
            "detection_scale": best['scale'],
            "processing_size": size,
            "top_candidates": [
                {"rotation": c['rotation'], "flipX": c['flipX'], "flipY": c['flipY'],
                 "scale": round(c['scale'], 3), "score": round(c['combined_score'], 4),
                 "position": [c['dx'], c['dy']]}
                for c in sorted(candidates, key=lambda x: -x['combined_score'])[:5]
            ]
        }
    }

    cosmx_tiles_dir.mkdir(parents=True, exist_ok=True)
    out_json = cosmx_tiles_dir / "transform.json"
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(transform_data, f, indent=2)
    print(f"  [Saved] {out_json}")

    #
    try:
        test_dir = Path(r"D:\ \test")
        test_dir.mkdir(parents=True, exist_ok=True)
        h_he, w_he = he_img.shape[:2]
        cosmx_t    = apply_transform(cosmx_img, best['rotation'], best['flipX'], best['flipY'])
        sc = best.get('scale', 1.0)
        if abs(sc - 1.0) > 0.01:
            nw = int(cosmx_t.shape[1] * sc); nh = int(cosmx_t.shape[0] * sc)
            if nw > 0 and nh > 0:
                cosmx_t = cv2.resize(cosmx_t, (nw, nh))
        dx = best.get('dx', 0); dy = best.get('dy', 0)
        canvas = np.zeros((h_he, w_he, 3), dtype=np.uint8)
        ch, cw = cosmx_t.shape[:2]
        x1=max(0,dx); y1=max(0,dy); x2=min(w_he,dx+cw); y2=min(h_he,dy+ch)
        sx=max(0,-dx); sy=max(0,-dy)
        if x2 > x1 and y2 > y1:
            canvas[y1:y2, x1:x2] = cosmx_t[sy:sy+(y2-y1), sx:sx+(x2-x1)]
        he_g   = cv2.cvtColor(he_img, cv2.COLOR_BGR2GRAY)
        cx_g   = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
        he_inv = 255 - he_g
        cd     = cx_g.copy(); cd[cx_g > 240] = 0; cd[cx_g < 5] = 0
        overlay = np.zeros((h_he, w_he, 3), dtype=np.uint8)
        overlay[..., 2] = he_inv; overlay[..., 1] = cd
        font    = cv2.FONT_HERSHEY_SIMPLEX
        ncc_str = f"NCC={best.get('ncc_score', 0):.3f}" if 'ncc_score' in best else ""
        cv2.putText(overlay,
                    f"Rot={best['rotation']} FX={best['flipX']} FY={best['flipY']} "
                    f"scale={sc:.2f} dx={dx} dy={dy}",
                    (20, 50), font, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(overlay,
                    f"Score={best['combined_score']:.4f}  {ncc_str}  [V8.2]",
                    (20, 85), font, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
        out_ov = test_dir / f"{slide_id}_orientation_overlay.png"
        Image.fromarray(cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)).save(str(out_ov))
        print(f"  [Overlay] → {out_ov}")
    except Exception as e:
        print(f"  [Overlay] Failed: {e}")

    return {
        'slide_id': slide_id, 'score': best['combined_score'],
        'rotation': best['rotation'], 'flipX': best['flipX'],
        'flipY': best['flipY'], 'scale': final_scale
    }



def run(slide_id, data_dir, svs_path=None, mode='auto', refine=True, debug=False, size=1024, log_cb=None):
    """Programmatic entry point used by app.py.

    Runs the original auto_orientation algorithm and writes:
        data/cosmx_tiles/<slide_id>/transform.json

    Important:
    Do NOT redirect stdout to log_cb here. app.py's log_cb prints to stdout,
    and redirecting stdout while calling that callback causes infinite recursion
    (maximum recursion depth exceeded).
    """
    data_dir = Path(data_dir)

    result = process_single_slide(
        slide_id,
        data_dir,
        mode,
        refine,
        debug,
        size,
        svs_path=svs_path,
    )

    if result is None:
        raise RuntimeError(f'auto_orientation failed for {slide_id}')

    if log_cb:
        try:
            log_cb(
                f"[AutoOrientation] {slide_id}: "
                f"score={result.get('score', 0):.4f}, "
                f"rot={result.get('rotation')}, "
                f"flipX={result.get('flipX')}, "
                f"flipY={result.get('flipY')}, "
                f"scale={result.get('scale', 1.0):.4f}"
            )
        except Exception:
            pass

    return result

def main():
    parser = argparse.ArgumentParser(description='Auto Orientation V8.1')
    parser.add_argument('--slide-id')
    parser.add_argument('--all',      action='store_true')
    parser.add_argument('--data-dir', default=r'D:\ \data')
    parser.add_argument('--mode',     choices=['auto', 'full', 'partial'], default='auto')
    parser.add_argument('--refine',   action='store_true')
    parser.add_argument('--debug',    action='store_true')
    parser.add_argument('--size',     type=int, default=1024)
    parser.add_argument('--svs-path', default=None, help='Original H&E SVS path. Used by web app without copying SVS into data/slides.')
    args = parser.parse_args()

    print("=" * 70)
    print("Auto Orientation V8.2 — V8 + phase_fallback fix + dual-mode threshold fix")
    print("=" * 70)

    data_dir = Path(args.data_dir)

    if args.all:
        cosmx_dir = data_dir / 'cosmx'
        if not cosmx_dir.exists():
            print(f"[ERROR] {cosmx_dir}"); return False
        files = [p for p in cosmx_dir.iterdir() if p.is_file() and any(p.name.lower().endswith(ext) for ext in COSMX_EXTENSIONS) and not p.name.lower().endswith('_thumb.jpg')]
        print(f"\n[Batch] {len(files)} files")
        results = []
        for i, f in enumerate(files, 1):
            print(f"\n[{i}/{len(files)}] {f.stem}")
            try:
                r = process_single_slide(f.stem, data_dir, args.mode,
                                         args.refine, args.debug, args.size, svs_path=args.svs_path)
                if r: results.append(r)
            except Exception as e:
                print(f"  [ERROR] {e}")
        if results:
            avg  = sum(r['score'] for r in results) / len(results)
            low  = [r for r in results if r['score'] < 0.4]
            print(f"\nProcessed: {len(results)}/{len(files)}  Avg: {avg:.4f}")
            if low:
                print("⚠️  Low score:")
                for r in low:
                    print(f"    {r['slide_id']}: {r['score']:.4f}")
        return True

    if not args.slide_id:
        print("[ERROR] --slide-id or --all required"); return False

    r = process_single_slide(args.slide_id, data_dir, args.mode,
                              args.refine, args.debug, args.size, svs_path=args.svs_path)
    if r:
        print("\n" + "=" * 70)
        print(f"  Rot={r['rotation']}°  FX={r['flipX']}  FY={r['flipY']}")
        print(f"  Scale={r['scale']:.4f}  Score={r['score']:.4f}")
        print("=" * 70)
    return r is not None


if __name__ == '__main__':
    try:
        sys.exit(0 if main() else 1)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] {e}"); traceback.print_exc(); sys.exit(1)