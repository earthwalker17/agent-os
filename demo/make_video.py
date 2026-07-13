"""
Assemble the final 1280x720 H.264 MP4 from the seven beat clips + an opening
and end card. Each clip is trimmed to its content window (from clips.json),
normalized to 30fps/yuv420p, and has its on-screen label burned in via FFmpeg
drawtext. Segments are concatenated with short crossfades.

Run from the demo/ directory (relative asset paths). Requires ffmpeg on PATH.
"""
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
RAW = "raw"
SEG = "build/segments"
os.makedirs(SEG, exist_ok=True)

W, H, FPS = 1280, 720, 30
BG = "0x0d1117"
FONT_SB = "assets/font-semibold.ttf"
FONT_RG = "assets/font-regular.ttf"
FONT_BK = "assets/font-black.ttf"
LAB = "assets/labels"

# ---- text assets (write them so the script is self-contained) ----------------
os.makedirs(LAB, exist_ok=True)
TEXTS = {
    "L1.txt": "Agent OS is the harness around the model.",
    "L2.txt": "Main Agent = brain · Coding Agent = hands",
    "L3.txt": "Controlled execution · auditable trace",
    "L4.txt": "The model can't mark its own homework.",
    "L5.txt": "Failure → Evidence → Bounded recovery",
    "L6.txt": "External actions require explicit approval.",
    "b7cap.txt": "Pulseboard — planned, built, verified & shipped through Agent OS",
    "open_eyebrow.txt": "A G E N T   O S",
    "open_main.txt": "A diff is not finished software.",
    "tag.txt": "LLM + Harness = Agent",
    "end_title.txt": "Agent OS",
    "end_tag.txt": "Agent OS · LLM + Harness = Agent",
    "url.txt": "github.com/earthwalker17/agent-os",
}
for name, val in TEXTS.items():
    with open(os.path.join(LAB, name), "w", encoding="utf-8") as f:
        f.write(val)

# ---- per-beat label file (None => no caption) --------------------------------
BEAT_LABEL = {
    "b1_cockpit": "L1.txt",
    "b2_thread": "L2.txt",
    "b3_trace": "L3.txt",
    "b4_verify": "L4.txt",
    "b5_recovery": "L5.txt",
    "b6_approval": "L6.txt",
    "b7_result": "b7cap.txt",
}


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        print(r.stdout[-2500:])
        raise SystemExit(f"ffmpeg failed: {' '.join(cmd[:6])} ...")


def caption_filter(label_file):
    """A centered caption near the bottom with drawtext's own translucent box.
    (A separate drawbox filter chained before drawtext segfaults this FFmpeg
    build, so we use the built-in box.)"""
    return (
        f"drawtext=fontfile={FONT_SB}:textfile={LAB}/{label_file}:fontcolor=white:"
        f"fontsize=27:x=(w-tw)/2:y=h-58:box=1:boxcolor=0x090c12@0.72:boxborderw=15"
    )


def build_beat(m):
    src = os.path.join(RAW, m["clip"])
    out = os.path.join(SEG, m["name"] + ".mp4")
    start, dur = m["start"], m["dur"]
    # Clips are recorded natively at 1280x720, so no scale/pad is needed.
    vf = f"fps={FPS}," + caption_filter(BEAT_LABEL[m["name"]]) + ",format=yuv420p"
    run(["ffmpeg", "-nostdin", "-y", "-i", src, "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-vf", vf, "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-r", str(FPS), out])
    return out


def build_card(name, dur, lines, fade_in=0.5, fade_out=0.5):
    """lines: list of (textfile, fontfile, fontsize, color, y_expr)."""
    out = os.path.join(SEG, name + ".mp4")
    parts = []
    for tf, ff, fs, col, y in lines:
        parts.append(
            f"drawtext=fontfile={ff}:textfile={LAB}/{tf}:fontcolor={col}:"
            f"fontsize={fs}:x=(w-tw)/2:y={y}"
        )
    fade = f"fade=t=in:st=0:d={fade_in},fade=t=out:st={dur-fade_out:.2f}:d={fade_out}"
    vf = ",".join(parts) + f",{fade},format=yuv420p"
    run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", f"color=c={BG}:s={W}x{H}:d={dur}:r={FPS}",
         "-vf", vf, "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium",
         "-pix_fmt", "yuv420p", out])
    return out


def main():
    data = json.load(open("build/clips.json", encoding="utf-8"))
    clips = data["clips"]

    # opening card
    opening = build_card("00_open", 2.8, [
        ("open_eyebrow.txt", FONT_SB, 26, "0x8b94a7", "h/2-140"),
        ("open_main.txt", FONT_BK, 52, "white", "h/2-70"),
        ("tag.txt", FONT_SB, 30, "0xa99bff", "h/2+20"),
    ])

    # beat segments
    segs = [build_beat(m) for m in clips]

    # end card
    ending = build_card("99_end", 3.4, [
        ("end_title.txt", FONT_BK, 60, "white", "h/2-96"),
        ("end_tag.txt", FONT_SB, 30, "0xa99bff", "h/2-8"),
        ("url.txt", FONT_SB, 27, "0x8b94a7", "h/2+52"),
    ])

    order = [opening] + segs + [ending]

    # concat via demuxer (all identical codec/params)
    listfile = os.path.join(SEG, "concat.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for s in order:
            f.write(f"file '{os.path.abspath(s)}'\n")
    out = "agent-os-demo.mp4"
    run(["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-r", str(FPS), out])
    print("\nwrote", os.path.abspath(out))


if __name__ == "__main__":
    main()
