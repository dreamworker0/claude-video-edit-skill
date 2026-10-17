"""Edit-from-raw, privacy pass: sample the screen every N seconds of the edit, drop near-duplicates, and write
2x2 contact sheets (960x540 tiles, labelled '#id clipN t=mm:ss') to <work>/privacy/ for visual review.

Usage: python privacy_review.py <work_dir> <media_dir> [--every 4] [--decisions decisions.json]
Look at every sheet: names, birth dates, phone numbers, e-mails, addresses, seals/signatures, invite codes.
Demo data typed for the recording is usually fine; real records (old reports, exported Excel) are not.
"""
import argparse, json, os, subprocess
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageDraw

ap = argparse.ArgumentParser()
ap.add_argument('work'); ap.add_argument('media')
ap.add_argument('--every', type=float, default=4); ap.add_argument('--decisions')
a = ap.parse_args()
FPS = 30000 / 1001
clips = json.load(open(os.path.join(a.work, 'edl.json'), encoding='utf-8'))
dec = json.load(open(a.decisions, encoding='utf-8')) if a.decisions else {}
stem = lambda f: os.path.splitext(os.path.basename(f))[0]
title = lambda c: any(stem(c['cam']) == r[0] and r[1] <= c['s'] < r[2] for r in dec.get('title', {}).get('ranges', []))

jobs, pos = [], 0.0
for i, c in enumerate(clips):
    d = c['e'] - c['s']
    if not title(c):
        k = 0
        while k * a.every < d:
            jobs.append((i, pos + k * a.every, c['s'] + c['lag'] + k * a.every, c['scr'])); k += 1
    pos += round(d * FPS) / FPS

def grab(j):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{j[2]:.3f}', '-i', os.path.join(a.media, j[3]), '-frames:v', '1',
                          '-vf', 'scale=960:540', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(540, 960, 3)

with ThreadPoolExecutor(8) as ex:
    frames = list(ex.map(grab, jobs))
keep, last = [], None
for j, f in zip(jobs, frames):
    if last is None or np.abs(f.astype(int) - last.astype(int)).mean() > 3:
        keep.append((j, f)); last = f
out = os.path.join(a.work, 'privacy'); os.makedirs(out, exist_ok=True)
meta = []
for s in range(0, len(keep), 4):
    sheet = Image.new('RGB', (1920, 1080), 'white')
    for k, (j, f) in enumerate(keep[s:s + 4]):
        im = Image.fromarray(f); d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 300, 22], fill=(0, 0, 0))
        d.text((4, 4), f'#{s + k} clip{j[0]} t={int(j[1] // 60)}:{j[1] % 60:04.1f} src={j[2]:.1f}', fill=(255, 255, 0))
        sheet.paste(im, ((k % 2) * 960, (k // 2) * 540))
        meta.append({'id': s + k, 'clip': j[0], 't': round(j[1], 2), 'src': round(j[2], 2), 'scr': j[3]})
    sheet.save(os.path.join(out, f'sheet_{s // 4:02d}.png'))
json.dump(meta, open(os.path.join(out, 'meta.json'), 'w', encoding='utf-8'), ensure_ascii=False)
print(f'{len(jobs)} samples -> {len(keep)} distinct -> {(len(keep) + 3) // 4} sheets in {out}')
