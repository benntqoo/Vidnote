"""Deduplicate scene-detected frames by dHash and build contact sheets.

Usage:
    python make_sheets.py <frames_dir> <scene_log> <out_dir> [--threshold 8]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw


def dhash(img: Image.Image, size: int = 8) -> int:
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    row = size + 1
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (1 if px[r * row + c] > px[r * row + c + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def parse_timestamps(log_path: Path) -> list[float]:
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    return [float(t) for t in re.findall(r"pts_time:([0-9.]+)", text)]


def fmt(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir")
    ap.add_argument("scene_log")
    ap.add_argument("out_dir")
    ap.add_argument("--threshold", type=int, default=8)
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--cell-w", type=int, default=320)
    args = ap.parse_args()

    frames_dir = Path(args.frames_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamps = parse_timestamps(Path(args.scene_log))
    files = sorted(frames_dir.glob("*.jpg"))

    if len(timestamps) < len(files):
        timestamps += [0.0] * (len(files) - len(timestamps))

    kept: list[dict] = []
    prev_hash: int | None = None

    for i, f in enumerate(files):
        with Image.open(f) as im:
            h = dhash(im)
        if prev_hash is not None and hamming(h, prev_hash) <= args.threshold:
            continue
        prev_hash = h
        kept.append({"file": f.name, "t": timestamps[i], "time": fmt(timestamps[i])})

    # contact sheets
    per_sheet = args.cols * args.rows
    cell_h = int(args.cell_w * 9 / 16)
    label_h = 18
    sheets: list[str] = []

    for s in range(0, len(kept), per_sheet):
        chunk = kept[s : s + per_sheet]
        sheet = Image.new("RGB", (args.cols * args.cell_w, args.rows * (cell_h + label_h)), (24, 24, 28))
        draw = ImageDraw.Draw(sheet)
        for k, item in enumerate(chunk):
            r, c = divmod(k, args.cols)
            x = c * args.cell_w
            y = r * (cell_h + label_h)
            with Image.open(frames_dir / item["file"]) as im:
                sheet.paste(im.convert("RGB").resize((args.cell_w, cell_h), Image.LANCZOS), (x, y + label_h))
            draw.text((x + 4, y + 3), f"#{s + k + 1} {item['time']}", fill=(255, 220, 120))
        name = f"sheet_{s // per_sheet + 1:02d}.jpg"
        sheet.save(out_dir / name, quality=88)
        sheets.append(name)

    index = {
        "total_scene_frames": len(files),
        "kept_after_dedup": len(kept),
        "threshold": args.threshold,
        "sheets": sheets,
        "frames": kept,
    }
    (out_dir / "frames_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"scene frames : {len(files)}")
    print(f"kept (dedup) : {len(kept)}")
    print(f"sheets       : {len(sheets)} -> {', '.join(sheets)}")


if __name__ == "__main__":
    main()
