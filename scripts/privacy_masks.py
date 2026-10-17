"""Edit-from-raw, privacy pass: mask spec -> per-clip mosaic dicts for scratch_timeline.py --mosaic.

Usage: python privacy_masks.py <work_dir> <media_dir> <spec.json> <out_masks.json> [--block 14]

spec.json = list of regions in SCREEN-recording coordinates (1920x1080 px) and screen-file time:
  [{"scr": "2026-08-16 08-42-22.mp4", "from": 821.4, "to": 829.95, "rect": [946, 236, 1064, 1010]},
   {"scr": "...", "from": 181.8, "to": 235.6, "detector": "my_detectors.py:report_masks"}]
"rect" = static box shown while from <= t <= to (fixed columns, side panels, a static Excel view).
"detector" = "file.py:function"; function(rgb_frame HxWx3 uint8) -> list of [x0,y0,x1,y1] or None per slot,
  called on every frame for content that scrolls (anchor on something you can find each frame, e.g. a
  coloured header cell or a red seal - see references/privacy_detector_example.py).
Blocks are fixed-size squares (block_px) so thin strips are obscured too.
"""
import argparse, importlib.util, json, os, subprocess
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('work'); ap.add_argument('media'); ap.add_argument('spec'); ap.add_argument('out')
ap.add_argument('--block', type=int, default=14)
a = ap.parse_args()
W, H, FPS = 1920, 1080, 30000 / 1001
clips = json.load(open(os.path.join(a.work, 'edl.json'), encoding='utf-8'))
spec = json.load(open(a.spec, encoding='utf-8'))

def norm(r):
    x0, y0, x1, y1 = r
    return [[x0 / W, y0 / H], [x1 / W, y0 / H], [x1 / W, y1 / H], [x0 / W, y1 / H]]

def load_detector(ref):
    path, fn = ref.rsplit(':', 1)
    sp = importlib.util.spec_from_file_location('det', path); m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m); return getattr(m, fn)

def frames(scr, t0, t1):
    n = int(round((t1 - t0) * FPS)) + 2
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t0 - 0.25 / FPS:.4f}', '-i', os.path.join(a.media, scr),
                          '-frames:v', str(n), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True).stdout
    x = np.frombuffer(raw, np.uint8); x = x[:len(x) // (W * H * 3) * W * H * 3].reshape(-1, H, W, 3)
    return [(t0 + i / FPS, f) for i, f in enumerate(x)]

out = {}
for ci, c in enumerate(clips):
    t0, t1 = c['s'] + c['lag'], c['e'] + c['lag']
    regs = [r for r in spec if r['scr'] == c['scr'] and r['from'] < t1 and r['to'] > t0]
    if not regs:
        continue
    times = [t0 + i / FPS for i in range(int(round((t1 - t0) * FPS)) + 2)]
    masks = []
    for r in regs:
        if 'rect' in r:
            masks.append([[round(t, 4), norm(r['rect']) if r['from'] <= t <= r['to'] else None] for t in times])
        else:
            det = load_detector(r['detector'])
            per = [(t, det(f) if r['from'] <= t <= r['to'] else None) for t, f in frames(c['scr'], t0, t1)]
            slots = max((len(m) for _, m in per if m), default=0)
            for k in range(slots):
                masks.append([[round(t, 4), norm(m[k]) if m and m[k] else None] for t, m in per])
    out[str(ci)] = {'h': 1, 'v': 1, 'sharp': False, 'block_px': a.block,
                    'masks': [{'keys': ks, 'feather': 2.0, 'opacity': 100.0, 'expansion': 0.0, 'inverted': False} for ks in masks]}
    print(f'clip {ci}: {len(masks)} mask track(s)')
json.dump(out, open(a.out, 'w', encoding='utf-8'))
print(f'{len(out)} clips with masks -> {a.out}')
