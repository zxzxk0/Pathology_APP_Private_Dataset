"""
CosMx-H&E Fine Registration (V1)
V8 auto_orientation  transform.json     
position(dx,dy) + scale  grid search  fine-tune.

rotation / flipX / flipY   V8      .

 :
  1. V8 transform.json   → rotation/flip/scale/dx/dy  
  2. Coarse grid search: scale ±20% × position ±10%  
  3. Fine grid search:       ±5%      
  4. NCC tiebreaker (  flip    )
  5.   transform_registered.json     + overlay  

 :
  python register_fine.py --slide-id SLIDE_ID
  python register_fine.py --all
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
# MASK
# ============================================================================

def create_he_mask(img):
    gray    = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY_INV)
    #
    mask = _remove_rect_border(mask)
    k    = np.ones((5,5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9,9), np.uint8))
    return mask


def create_cosmx_mask(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sat  = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:,:,1]
    raw  = (((gray < 250) & (gray > 5)) | (sat > 20)).astype(np.uint8) * 255
    # fiducial
    raw  = _remove_fiducial(raw, *raw.shape[:2])
    k    = np.ones((7,7), np.uint8)
    mask = cv2.dilate(raw, k, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15,15), np.uint8))
    return mask


def _remove_rect_border(mask, min_fill=0.55, max_t=30):
    h, w = mask.shape
    for t in range(3, max_t+1):
        if 2*t >= h or 2*t >= w: break
        if all([(mask[t-1, t:-t]>0).mean() > min_fill,
                (mask[-t,  t:-t]>0).mean() > min_fill,
                (mask[t:-t, t-1]>0).mean() > min_fill,
                (mask[t:-t, -t ]>0).mean() > min_fill]):
            c = mask.copy()
            c[:t,:]=0; c[-t:,:]=0; c[:,:t]=0; c[:,-t:]=0
            return c
    return mask


def _remove_fiducial(mask, h, w, max_r=0.003):
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    clean = np.zeros_like(mask)
    for i in range(1, n):
        a  = stats[i, cv2.CC_STAT_AREA]
        bw = stats[i, cv2.CC_STAT_WIDTH]
        bh = stats[i, cv2.CC_STAT_HEIGHT]
        if bw==0 or bh==0: continue
        if (a/(h*w) < max_r and min(bw,bh)/max(bw,bh) > 0.6
                and a/(bw*bh) > 0.65 and a > 10):
            continue
        clean[labels==i] = 255
    return clean


# ============================================================================
# TRANSFORM
# ============================================================================

def apply_transform(img, rotation, flip_x, flip_y):
    r = img.copy()
    k = rotation // 90
    if k: r = np.rot90(r, k=k)
    if flip_x: r = np.fliplr(r)
    if flip_y: r = np.flipud(r)
    return np.ascontiguousarray(r)


# ============================================================================
# SCORING — coverage-aware precision/IoU
# ============================================================================

def coverage_score(he_mask, cosmx_scaled, dx, dy, cov_ratio=1.0):
    # Anchor/manual transforms can store dx, dy as floats.
    # NumPy slice indices must be integers, so round once at the scoring boundary.
    dx = int(round(float(dx)))
    dy = int(round(float(dy)))

    """
    ✅ F1   score — Precision × Recall  
    
    Precision = inter / cosmx_tissue  → CosMx         
    Recall    = inter / he_tissue     → H&E   CosMx     
    F1        = 2*P*R / (P+R)        →          
    
    "CosMx       , H&E      "     .
    cov_ratio    Recall    :
      - cov < 0.4 (partial): F1  Precision     (CosMx    )
      - cov > 0.7 (full):    F1   (Recall   )
    """
    he_h, he_w = he_mask.shape
    ch, cw     = cosmx_scaled.shape
    x1=max(0,dx); y1=max(0,dy)
    x2=min(he_w,dx+cw); y2=min(he_h,dy+ch)
    sx=max(0,-dx); sy=max(0,-dy)
    rw=x2-x1; rh=y2-y1
    if rw<=0 or rh<=0: return 0.0
    if sy+rh>ch or sx+rw>cw: return 0.0

    he_r     = he_mask[y1:y2, x1:x2]
    cx_r     = cosmx_scaled[sy:sy+rh, sx:sx+rw]
    inter    = float(np.logical_and(he_r>0, cx_r>0).sum())
    cx_total = float((cosmx_scaled > 0).sum())          # CosMx tissue
    he_total = float((he_mask > 0).sum())                # H&E tissue

    prec   = inter / (cx_total + 1e-6)   # CosMx H&E
    recall = inter / (he_total + 1e-6)   # H&E CosMx
    f1     = 2 * prec * recall / (prec + recall + 1e-6)

    # partial : recall weighted F1 (precision )
    if cov_ratio < 0.4:
        # precision 70% + recall 30%
        score = 0.7 * prec + 0.3 * recall
    elif cov_ratio > 0.7:
        # F1 (coverage )
        score = f1
    else:
        alpha = (cov_ratio - 0.4) / 0.3   # 0→1
        score = (1-alpha) * (0.7*prec + 0.3*recall) + alpha * f1

    return score


# ============================================================================
# GRID SEARCH REGISTRATION
# ============================================================================

def grid_search(he_mask, cosmx_transformed, init_dx, init_dy, init_scale,
                cov_ratio, dx_range, dy_range,
                scale_down, scale_up,          # ✅ scale
                dx_step, dy_step, scale_step, label=""):
    """
    (dx, dy, scale) 3D grid search.
    scale_down: init        ( : 0.15 → init*0.85 )
    scale_up:   init         ( : 0.50 → init*1.50 )
    → scale                 
    """
    he_h, he_w   = he_mask.shape
    cx_h, cx_w   = cosmx_transformed.shape[:2]

    best_score = -1
    best       = {'dx': init_dx, 'dy': init_dy, 'scale': init_scale, 'score': -1}

    s_lo = max(0.30, init_scale * (1.0 - scale_down))
    # ✅ : H&E (template matching )
    # CosMx H&E coverage_score
    s_hi = init_scale * (1.0 + scale_up)
    scales = np.arange(s_lo, s_hi + scale_step * 0.5, scale_step)

    dx_vals = range(int(init_dx - dx_range),
                    int(init_dx + dx_range) + 1, int(max(1, dx_step)))
    dy_vals = range(int(init_dy - dy_range),
                    int(init_dy + dy_range) + 1, int(max(1, dy_step)))

    n_total = len(scales) * len(dx_vals) * len(dy_vals)
    print(f"    {label}: scale {s_lo:.2f}~{s_hi:.2f} ({len(scales)} steps) × "
          f"{len(dx_vals)}×{len(dy_vals)} pos = {n_total} evals")

    for scale in scales:
        nw = max(1, int(cx_w * scale))
        nh = max(1, int(cx_h * scale))
        # ✅ : template matching
        # registration IoU/Precision CosMx >= H&E
        # , overlap=0 H&E 200%
        if nw > he_w * 2 or nh > he_h * 2 or nw < 10 or nh < 10:
            continue
        cosmx_s = cv2.resize(cosmx_transformed, (nw, nh), interpolation=cv2.INTER_AREA)

        for dx in dx_vals:
            for dy in dy_vals:
                sc = coverage_score(he_mask, cosmx_s, dx, dy, cov_ratio)
                if sc > best_score:
                    best_score = sc
                    best = {'dx': int(dx), 'dy': int(dy),
                            'scale': float(scale), 'score': sc}

    best['score'] = best_score
    return best


def tissue_centroid(mask):
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return mask.shape[1]//2, mask.shape[0]//2
    return int(xs.mean()), int(ys.mean())


def tissue_bbox_wh(mask):
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return mask.shape[1], mask.shape[0]
    return int(xs.max()-xs.min()+1), int(ys.max()-ys.min()+1)


def centroid_candidates(he_mask, cosmx_transformed, init_scale, cov_ratio, n_scales=6):
    """
    tissue centroid        /   .
    H&E centroid ↔ CosMx centroid    (dx, dy)  scale   .
    """
    he_h, he_w  = he_mask.shape
    cx_h, cx_w  = cosmx_transformed.shape[:2]
    he_cx, he_cy = tissue_centroid(he_mask)
    cx_cx, cx_cy = tissue_centroid(cosmx_transformed)
    he_bw, he_bh = tissue_bbox_wh(he_mask)
    cx_bw, cx_bh = tissue_bbox_wh(cosmx_transformed)

    # tissue bbox center scale
    s_w = he_bw / (cx_bw + 1e-6)
    s_h = he_bh / (cx_bh + 1e-6)
    s_center = (s_w + s_h) / 2.0
    s_lo = max(0.30, s_center * 0.60)
    s_hi = s_center * 1.60

    candidates = []
    for scale in np.linspace(s_lo, s_hi, n_scales):
        nw = max(1, int(cx_w * scale))
        nh = max(1, int(cx_h * scale))
        if nw < 10 or nh < 10:
            continue
        # centroid dx, dy
        dx = he_cx - int(cx_cx * scale)
        dy = he_cy - int(cx_cy * scale)
        #
        dx = max(-nw//2, min(he_w, dx))
        dy = max(-nh//2, min(he_h, dy))
        cx_s = cv2.resize(cosmx_transformed, (nw, nh), interpolation=cv2.INTER_AREA)
        sc   = coverage_score(he_mask, cx_s, dx, dy, cov_ratio)
        candidates.append({'dx': dx, 'dy': dy, 'scale': float(scale), 'score': sc})

    candidates.sort(key=lambda x: -x['score'])
    return candidates


def register_slide(he_mask, cosmx_mask, init_transform, cov_ratio, he_size):
    rotation = init_transform['rotation']
    flip_x   = init_transform['flipX']
    flip_y   = init_transform['flipY']
    he_h, he_w = he_mask.shape

    init_scale = init_transform.get('scale', 1.0)
    init_dx    = init_transform.get('dx_px', 0)
    init_dy    = init_transform.get('dy_px', 0)

    print(f"\n  Initial: scale={init_scale:.3f} dx={init_dx} dy={init_dy}")
    print(f"  Coverage ratio: {cov_ratio:.3f}  "
          f"→ {'Precision-weighted' if cov_ratio<0.4 else 'F1' if cov_ratio>0.7 else 'Blend'}")

    # ── init score ───────────────────────────────────────────────────────
    cosmx_t = apply_transform(cosmx_mask, rotation, flip_x, flip_y)
    nw0 = max(1, int(cosmx_t.shape[1] * init_scale))
    nh0 = max(1, int(cosmx_t.shape[0] * init_scale))
    if nw0 > 0 and nh0 > 0:
        cx_s0      = cv2.resize(cosmx_t, (nw0, nh0), interpolation=cv2.INTER_AREA)
        init_score = coverage_score(he_mask, cx_s0, init_dx, init_dy, cov_ratio)
    else:
        init_score = 0.0
    print(f"  Init score: {init_score:.4f}")

    # ── Stage 0: 16 Rescue ────────────────────────────────────────
    # init score < 0.15 → rotation 16
    # : (A) tissue centroid + (B) canvas sweep
    if init_score < 0.15:
        print("\n  [Stage 0] 16-orientation rescue (centroid + sweep)...")
        all_orientations = [
            (rot, fx, fy)
            for rot in [0, 90, 180, 270]
            for fx  in [False, True]
            for fy  in [False, True]
        ]
        rescue_best_score = -1
        rescue_best       = None
        rescue_best_ori   = (rotation, flip_x, flip_y)

        for rot, fx, fy in all_orientations:
            cx_t = apply_transform(cosmx_mask, rot, fx, fy)

            # (A) centroid ( )
            cands = centroid_candidates(he_mask, cx_t, init_scale, cov_ratio, n_scales=8)
            best_cand = cands[0] if cands else None

            # (B) canvas sweep
            sweep = grid_search(
                he_mask, cx_t,
                init_dx    = he_w // 2,
                init_dy    = he_h // 2,
                init_scale = init_scale,
                cov_ratio  = cov_ratio,
                dx_range   = he_w * 0.55,
                dy_range   = he_h * 0.55,
                scale_down = 0.15,
                scale_up   = 0.70,
                dx_step    = max(30, he_w // 20),   #
                dy_step    = max(30, he_h // 20),
                scale_step = 0.10,
                label      = f"R={rot} FX={fx} FY={fy}"
            )

            #
            local_best = sweep
            if best_cand and best_cand['score'] > sweep['score']:
                local_best = best_cand

            print(f"    Rot={rot:>3} FX={fx} FY={fy}: "
                  f"centroid={cands[0]['score']:.3f}@s={cands[0]['scale']:.2f} "
                  f"sweep={sweep['score']:.3f}  → {local_best['score']:.3f}")

            if local_best['score'] > rescue_best_score:
                rescue_best_score = local_best['score']
                rescue_best       = local_best
                rescue_best_ori   = (rot, fx, fy)

        rotation, flip_x, flip_y = rescue_best_ori
        cosmx_t    = apply_transform(cosmx_mask, rotation, flip_x, flip_y)
        init_dx    = rescue_best['dx']
        init_dy    = rescue_best['dy']
        init_scale = rescue_best['scale']
        print(f"\n  → Rescue best: Rot={rotation} FX={flip_x} FY={flip_y} "
              f"scale={init_scale:.3f} ({init_dx},{init_dy}) "
              f"score={rescue_best_score:.4f}")

    else:
        # init flip 4
        print(f"\n  [Flip scan] Testing 4 flip combinations (Rot={rotation} fixed)...")
        flip_combos = [
            (flip_x,     flip_y,     "V8 init"),
            (not flip_x, flip_y,     "FX flip"),
            (flip_x,     not flip_y, "FY flip"),
            (not flip_x, not flip_y, "both flip"),
        ]
        best_flip_score = init_score
        for fx, fy, label in flip_combos:
            cx_t = apply_transform(cosmx_mask, rotation, fx, fy)
            nw = max(1, int(cx_t.shape[1] * init_scale))
            nh = max(1, int(cx_t.shape[0] * init_scale))
            if nw > 0 and nh > 0:
                cx_s = cv2.resize(cx_t, (nw, nh), interpolation=cv2.INTER_AREA)
                sc   = coverage_score(he_mask, cx_s, init_dx, init_dy, cov_ratio)
            else:
                sc = 0.0
            print(f"    FX={fx} FY={fy} ({label}): {sc:.4f}")
            if sc > best_flip_score:
                best_flip_score = sc
                flip_x, flip_y  = fx, fy
        cosmx_t = apply_transform(cosmx_mask, rotation, flip_x, flip_y)
        print(f"  → Best flip: FX={flip_x} FY={flip_y} ({best_flip_score:.4f})")

    # ── Stage 1: Coarse search ──────────────────────────────────────────────
    print("\n  [Stage 1] Coarse search (scale bias: upward)...")
    coarse = grid_search(
        he_mask, cosmx_t, init_dx, init_dy, init_scale, cov_ratio,
        dx_range   = he_w  * 0.25,
        dy_range   = he_h  * 0.25,
        scale_down = 0.15,
        scale_up   = 0.60,
        dx_step    = max(10, he_w // 50),
        dy_step    = max(10, he_h // 50),
        scale_step = 0.05,
        label="Coarse"
    )
    print(f"    Coarse best: scale={coarse['scale']:.3f} "
          f"dx={coarse['dx']} dy={coarse['dy']} score={coarse['score']:.4f}")

    # ── Stage 2: Fine search ────────────────────────────────────────────────
    print("\n  [Stage 2] Fine search...")
    fine = grid_search(
        he_mask, cosmx_t, coarse['dx'], coarse['dy'], coarse['scale'], cov_ratio,
        dx_range   = max(40, he_w * 0.05),
        dy_range   = max(40, he_h * 0.05),
        scale_down = 0.10,
        scale_up   = 0.10,
        dx_step    = max(3, he_w // 150),
        dy_step    = max(3, he_h // 150),
        scale_step = 0.01,
        label="Fine"
    )
    print(f"    Fine best:   scale={fine['scale']:.3f} "
          f"dx={fine['dx']} dy={fine['dy']} score={fine['score']:.4f}")

    # ── Stage 3: Pixel-level micro search ──────────────────────────────────
    print("\n  [Stage 3] Micro search...")
    micro = grid_search(
        he_mask, cosmx_t, fine['dx'], fine['dy'], fine['scale'], cov_ratio,
        dx_range   = 20,
        dy_range   = 20,
        scale_down = 0.02,
        scale_up   = 0.02,
        dx_step    = 1,
        dy_step    = 1,
        scale_step = 0.005,
        label="Micro"
    )
    print(f"    Micro best:  scale={micro['scale']:.3f} "
          f"dx={micro['dx']} dy={micro['dy']} score={micro['score']:.4f}")

    micro['flipX'] = flip_x
    micro['flipY'] = flip_y
    return micro


# ============================================================================
# PROCESS
# ============================================================================

def process_single_slide(slide_id, data_dir, size, init_version='8', svs_path=None, transform_file='latest'):
    slides_dir      = data_dir / 'slides'
    cosmx_dir       = data_dir / 'cosmx'
    tiles_dir       = data_dir / 'cosmx_tiles' / slide_id

    # transform json
    # - latest: anchor/auto result transform_registered.json
    # - transform.json: auto_orientation
    # - transform_registered.json:
    if transform_file == 'latest':
        candidates = [tiles_dir / 'transform_registered.json', tiles_dir / 'transform.json']
    else:
        candidates = [tiles_dir / transform_file]

    json_path = None
    for c in candidates:
        if c.exists():
            json_path = c
            break
    if json_path is None:
        print(f"[SKIP] transform json not found in: {[str(c) for c in candidates]}"); return None

    with open(json_path, encoding='utf-8') as f:
        tj = json.load(f)

    tf = tj.get('transform', {})
    init = {
        'rotation': tf.get('rotation', 0),
        'flipX':    tf.get('flipX',    False),
        'flipY':    tf.get('flipY',    False),
        'scale':    tf.get('scale',    1.0),
        'dx_px':    tf.get('translateX_pixels', 0),
        'dy_px':    tf.get('translateY_pixels', 0),
    }
    print(f"\n[Register] {slide_id}")
    print(f"  Init from {json_path.name} v{tj.get('version','?')}: "
          f"Rot={init['rotation']} FX={init['flipX']} FY={init['flipY']} "
          f"scale={init['scale']:.3f} dx={init['dx_px']} dy={init['dy_px']}")

    # H&E
    # SVS data/slides ,
    # app.py svs_path .
    he_path = Path(svs_path) if svs_path else None
    if he_path is None or not he_path.exists():
        he_path = None
        for ext in ['.svs', '.png', '.jpg', '.tif', '.tiff', '.ndpi', '.scn']:
            p = slides_dir / f"{slide_id}{ext}"
            if p.exists():
                he_path = p
                break
    if he_path is None or not he_path.exists():
        print(f"[SKIP] H&E not found. svs_path={svs_path}"); return None
    print(f"  H&E source: {he_path}")

    # CosMx
    cosmx_path = _find_cosmx_file(cosmx_dir, slide_id)
    if cosmx_path is None:
        print(f"[SKIP] CosMx not found"); return None

    #
    he_img,    _ = load_he_image(he_path, size)
    cosmx_img, _ = load_cosmx_image(cosmx_path, size)

    he_mask    = create_he_mask(he_img)
    cosmx_mask = create_cosmx_mask(cosmx_img)

    he_area  = float((he_mask > 0).sum())
    cx_area  = float((cosmx_mask > 0).sum())
    cov_ratio = cx_area / (he_area + 1e-6)

    # Registration
    result = register_slide(he_mask, cosmx_mask, init, cov_ratio,
                            he_img.shape[:2])

    # transform.json
    he_h, he_w = he_mask.shape
    tj_out = dict(tj)
    tj_out['version']        = tj.get('version','?') + '_reg'
    tj_out['method']         = tj.get('method','') + '_fine_registered'
    tj_out['fine_registration_source_transform'] = str(json_path)
    tj_out['fine_registration_source_svs'] = str(he_path)
    tj_out['registration']   = {
        'initial_score': tj.get('detection',{}).get('combined_score', 0),
        'final_score':   result['score'],
        'coarse_search': 'dx±15% dy±15% scale±25%',
        'fine_search':   'dx±4%  dy±4%  scale±8%',
        'micro_search':  'dx±15px dy±15px scale±2%'
    }
    tj_out['transform']['scale']             = result['scale']
    tj_out['transform']['flipX']             = result.get('flipX', init['flipX'])
    tj_out['transform']['flipY']             = result.get('flipY', init['flipY'])
    tj_out['transform']['translateX_pixels'] = result['dx']
    tj_out['transform']['translateY_pixels'] = result['dy']
    tj_out['transform']['translateX']        = result['dx'] / he_w
    tj_out['transform']['translateY']        = result['dy'] / he_h

    out_json = tiles_dir / 'transform_registered.json'
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(tj_out, f, indent=2)
    print(f"\n  [Saved] {out_json}")

    #
    try:
        test_dir = Path(r"D:\ \test")
        test_dir.mkdir(parents=True, exist_ok=True)

        h_he, w_he = he_img.shape[:2]
        res_fx = result.get('flipX', init['flipX'])
        res_fy = result.get('flipY', init['flipY'])
        cosmx_t    = apply_transform(cosmx_img, init['rotation'], res_fx, res_fy)
        sc = result['scale']
        nw = max(1, int(cosmx_t.shape[1]*sc))
        nh = max(1, int(cosmx_t.shape[0]*sc))
        cosmx_t = cv2.resize(cosmx_t, (nw, nh))

        dx, dy = result['dx'], result['dy']
        canvas = np.zeros((h_he, w_he, 3), dtype=np.uint8)
        ch, cw = cosmx_t.shape[:2]
        x1=max(0,dx); y1=max(0,dy)
        x2=min(w_he,dx+cw); y2=min(h_he,dy+ch)
        sx=max(0,-dx); sy=max(0,-dy)
        if x2>x1 and y2>y1:
            canvas[y1:y2, x1:x2] = cosmx_t[sy:sy+(y2-y1), sx:sx+(x2-x1)]

        he_g   = cv2.cvtColor(he_img, cv2.COLOR_BGR2GRAY)
        cx_g   = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
        he_inv = 255 - he_g
        cd     = cx_g.copy(); cd[cx_g>240]=0; cd[cx_g<5]=0

        overlay = np.zeros((h_he, w_he, 3), dtype=np.uint8)
        overlay[...,2] = he_inv; overlay[...,1] = cd

        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(overlay,
                    f"Rot={init['rotation']} FX={init['flipX']} FY={init['flipY']} "
                    f"scale={sc:.3f} dx={dx} dy={dy}",
                    (20,50), font, 0.8, (255,255,255), 2, cv2.LINE_AA)
        cv2.putText(overlay,
                    f"Score={result['score']:.4f}  cov={cov_ratio:.2f}  [Reg]",
                    (20,85), font, 0.8, (255,255,255), 2, cv2.LINE_AA)

        out_ov = test_dir / f"{slide_id}_registered_overlay.png"
        overlay_img = Image.fromarray(cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB))
        overlay_img.save(str(out_ov))
        out_ov2 = tiles_dir / f"{slide_id}_registered_overlay.png"
        overlay_img.save(str(out_ov2))
        print(f"  [Overlay] → {out_ov}")
        print(f"  [Overlay] → {out_ov2}")
    except Exception as e:
        print(f"  [Overlay] Failed: {e}")

    return {
        'slide_id': slide_id,
        'init_score':  tj.get('detection',{}).get('combined_score', 0),
        'final_score': result['score'],
        'scale': result['scale'],
        'dx': result['dx'], 'dy': result['dy'],
        'rotation': init['rotation'],
        'flipX': init['flipX'], 'flipY': init['flipY'],
    }



# ============================================================================
# APP.PY ENTRY POINT
# ============================================================================

def run(slide_id, data_dir, svs_path=None, size=1024, transform_file='latest', log_cb=None):
    """
    app.py      wrapper.

    Parameters
    ----------
    slide_id : str
        Current slide id.
    data_dir : str | Path
        E:/ /data   data root.
    svs_path : str | Path | None
          H&E SVS  . SVS  data/slides             .
    size : int
        Thumbnail registration size.
    transform_file : 'latest' | 'transform.json' | 'transform_registered.json'
        fine registration      transform json.
        latest  transform_registered.json        transform.json  .
    log_cb : callable | None
        app.py    .   print      ,     log_cb   .
    """
    data_dir = Path(data_dir)
    result = process_single_slide(
        slide_id=slide_id,
        data_dir=data_dir,
        size=size,
        svs_path=str(svs_path) if svs_path else None,
        transform_file=transform_file,
    )
    if result is None:
        raise RuntimeError(f'register_fine failed for {slide_id}')
    if log_cb:
        log_cb(f"[Fine] {slide_id}: score={result['final_score']:.4f}, scale={result['scale']:.3f}, dx={result['dx']}, dy={result['dy']}")
    return result

# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='CosMx-H&E Fine Registration')
    parser.add_argument('--slide-id')
    parser.add_argument('--all',      action='store_true')
    parser.add_argument('--data-dir', default=r'D:\ \data')
    parser.add_argument('--size',     type=int, default=1024)
    parser.add_argument('--svs-path', default=None, help='  H&E SVS  . data/slides          ')
    parser.add_argument('--transform-file', default='latest', choices=['latest','transform.json','transform_registered.json'], help='fine registration   transform')
    args = parser.parse_args()

    print("=" * 65)
    print("CosMx-H&E Fine Registration (Grid Search, 3-stage)")
    print("=" * 65)

    data_dir = Path(args.data_dir)

    if args.all:
        tiles_dir = data_dir / 'cosmx_tiles'
        if not tiles_dir.exists():
            print(f"[ERROR] {tiles_dir}"); return False
        slide_ids = [d.name for d in tiles_dir.iterdir()
                     if d.is_dir() and (d/'transform.json').exists()]
        print(f"[Batch] {len(slide_ids)} slides with transform.json")
        results = []
        for i, sid in enumerate(slide_ids, 1):
            print(f"\n[{i}/{len(slide_ids)}] {sid}")
            try:
                r = process_single_slide(sid, data_dir, args.size, svs_path=args.svs_path, transform_file=args.transform_file)
                if r: results.append(r)
            except Exception as e:
                import traceback; print(f"  [ERROR] {e}"); traceback.print_exc()
        if results:
            improved = [r for r in results if r['final_score'] > r['init_score']]
            print(f"\n{'='*65}")
            print(f"Done: {len(results)}/{len(slide_ids)}  "
                  f"Improved: {len(improved)}/{len(results)}")
            for r in results:
                arrow = "↑" if r['final_score'] > r['init_score'] else "→"
                print(f"  {r['slide_id']}: {r['init_score']:.4f} {arrow} {r['final_score']:.4f}  "
                      f"scale={r['scale']:.3f} dx={r['dx']} dy={r['dy']}")
        return True

    if not args.slide_id:
        print("[ERROR] --slide-id or --all required"); return False

    r = process_single_slide(args.slide_id, data_dir, args.size, svs_path=args.svs_path, transform_file=args.transform_file)
    if r:
        print(f"\n{'='*65}")
        print(f"  Rot={r['rotation']}  FX={r['flipX']}  FY={r['flipY']}")
        print(f"  Scale:  {r['scale']:.4f}")
        print(f"  dx={r['dx']}  dy={r['dy']}")
        print(f"  Score:  {r['init_score']:.4f} → {r['final_score']:.4f}")
        print(f"{'='*65}")
    return r is not None


if __name__ == '__main__':
    try:
        sys.exit(0 if main() else 1)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] {e}"); traceback.print_exc(); sys.exit(1)