from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
if SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))

from centaur.engine import run_workflow


if __name__ == "__main__":
    run_workflow(ROOT, allow_repo_root=True)
