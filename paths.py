import sys
from pathlib import Path

_FROZEN = getattr(sys, "frozen", False)
ASSET_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_ROOT = Path(sys.executable).resolve().parent if _FROZEN else Path(__file__).resolve().parent

ROOT = DATA_ROOT
PROFILES_DIR = DATA_ROOT / "profiles"
COOKIE_FILE = DATA_ROOT / "cookie.json"
STATIC_DIR = ASSET_ROOT / "web" / "static"
MUSIC_DIR = ASSET_ROOT / "music"
