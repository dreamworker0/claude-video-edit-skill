"""Find short dark flashes in an edit - typically a tab or page that opened right at a cut and was recorded black
for a few frames. Long dark stretches (dark scenes, fades longer than --max-frames) are not reported.

Usage:
  python black_frames.py <video.mp4> [--work <work_dir>] [--share 0.4] [--max-frames 60]

Prints each flash as edit time + frame count. With --work (edl.json from scratch_edl.py, i.e. the edit this video was
rendered from) it also prints the screen-file time of that frame - the "at" for scratch_timeline's
decisions["skip_screen_frames"]. A flash in the last second is usually the fade-out.
"""
import argparse, json, os, subprocess
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('video'); ap.add_argument('--work')
ap.add_argument('--share', type=float, default=0.4, help='fraction of near-black pixels that counts as dark')
ap.add_argument('--max-frames', type=int, default=60)
a = ap.parse_args()
FPS = 30000 / 1001
W, H = 48, 27

raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', a.video, '-vf', f'scale={W}:{H}', '-f', 'rawvideo',
                      '-pix_fmt', 'gray', '-'], capture_output=True).stdout
dark = (np.frombuffer(raw, np.uint8).reshape(-1, H, W) < 30).mean((1, 2)) > a.share
clips = json.load(open(os.path.join(a.work, 'edl.json'), encoding='utf-8')) if a.work else []

def screen_time(t):
    pos = 0.0
    for c in clips:
        n = round((c['e'] - c['s']) * FPS) / FPS
        if pos <= t < pos + n:
            return c['s'] + c['lag'] + (t - pos)
        pos += n

i, found = 0, 0
while i < len(dark):
    if dark[i]:
        j = i
        while j < len(dark) and dark[j]:
            j += 1
        if j - i <= a.max_frames:
            t = i / FPS
            st = screen_time(t) if clips else None
            print(f'{int(t // 60)}:{t % 60:05.2f}  {j - i} frames' + (f'  screen {st:.2f}s' if st is not None else ''))
            found += 1
        i = j
    else:
        i += 1
print(f'{found} short dark run(s)')
