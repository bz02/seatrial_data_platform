"""Package the browser extension: copies the shared recorder into dist/seatrail-recorder/."""
import shutil
from pathlib import Path

root = Path(__file__).resolve().parent.parent
out = root / "dist" / "seatrail-recorder"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(root / "recorder" / "extension", out)
shutil.copy(root / "recorder" / "recorder.js", out / "recorder.js")
print(f"Built {out}. Load it in Chrome via chrome://extensions > Developer mode > Load unpacked.")
