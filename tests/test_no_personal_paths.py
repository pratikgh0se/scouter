"""No tracked file may carry the maintainer's home-directory path (public-launch pre-flight).
The needle is assembled at runtime so this file does not match itself."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEEDLE = "Users" + "/" + "pratik"


def test_no_tracked_file_contains_the_personal_home_path():
    r = subprocess.run(
        ["git", "grep", "-n", "-I", "-e", NEEDLE], cwd=ROOT, capture_output=True, text=True
    )
    assert r.returncode == 1, f"personal path found in tracked files:\n{r.stdout[:2000]}{r.stderr}"
