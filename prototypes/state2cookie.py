"""Playwright storage-state JSON -> Netscape cookies.txt (for yt-dlp)."""
import json
import sys
import time


def main(src: str, dst: str) -> None:
    with open(src, encoding="utf-8") as fh:
        state = json.load(fh)

    now = int(time.time())
    lines = ["# Netscape HTTP Cookie File", "# generated from playwright state", ""]
    kept = 0

    for c in state.get("cookies", []):
        name = (c.get("name") or "").strip()
        if not name:
            continue

        domain = c["domain"]
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"

        expires = c.get("expires")
        expires = int(expires) if expires and expires > 0 else now + 86400 * 365

        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        path = c.get("path") or "/"

        lines.append(
            "\t".join([prefix + domain, include_sub, path, secure, str(expires), name, c["value"]])
        )
        kept += 1

    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"wrote {dst}: {kept} cookies")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
