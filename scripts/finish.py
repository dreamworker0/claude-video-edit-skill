"""Edit-from-raw, last step: render.py output -> deliverable. Loudness to a target (two-pass loudnorm), THEN a
picture + sound fade-out at the end, re-encoded once.

Usage:
  python finish.py <render.mp4> <out.mp4> [--lufs -16] [--tp -1.5] [--fade 1.5] [--crf 18]

* Order matters: loudnorm is dynamic and lifts quiet passages, so a fade applied BEFORE it comes out flattened
  (measured: the first second of a 1.5 s fade lost only 3-5 dB). Measure on the un-faded render, then
  loudnorm -> afade in one filter chain.
* Screen + camera recordings are often very quiet (a green-screen talk measured -34 LUFS); -16 LUFS / -1.5 dBTP
  is a common target for web video.
* Give the fade room: scratch_edl.py decisions["tail"] extends the last clip past the last word, so the fade runs
  over silence instead of the closing greeting.
* If <out.mp4> is open in a player (Windows locks it), the result is written next to it as <name>_new.mp4.
"""
import argparse, json, os, subprocess

ap = argparse.ArgumentParser()
ap.add_argument('src'); ap.add_argument('out')
ap.add_argument('--lufs', type=float, default=-16); ap.add_argument('--tp', type=float, default=-1.5)
ap.add_argument('--fade', type=float, default=1.5); ap.add_argument('--crf', default='18')
a = ap.parse_args()

target = f'I={a.lufs}:TP={a.tp}:LRA=11'
r = subprocess.run(['ffmpeg', '-hide_banner', '-i', a.src, '-vn', '-af', f'loudnorm={target}:print_format=json',
                    '-f', 'null', '-'], capture_output=True, text=True, encoding='utf-8', errors='replace')
m = json.loads(r.stderr[r.stderr.rindex('{'):r.stderr.rindex('}') + 1])
dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', a.src],
                           capture_output=True, text=True).stdout)
st = round(dur - a.fade, 3)
af = (f"loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
      f"measured_thresh={m['input_thresh']}:offset={m['target_offset']},aresample=48000")
vf = 'null'
if a.fade > 0:
    af += f',afade=t=out:st={st}:d={a.fade}'
    vf = f'fade=t=out:st={st}:d={a.fade}'
print(f"input {m['input_i']} LUFS -> {a.lufs}; {dur:.2f}s; fade from {st}s")

tmp = os.path.splitext(a.out)[0] + '.tmp.mp4'
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', a.src, '-vf', vf, '-af', af,
                '-c:v', 'libx264', '-crf', a.crf, '-preset', 'fast', '-pix_fmt', 'yuv420p',
                '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', tmp], check=True)
out = a.out
try:
    os.replace(tmp, out)
except PermissionError:                      # open in a player
    out = os.path.splitext(a.out)[0] + '_new.mp4'
    os.replace(tmp, out)
print('wrote', out)
