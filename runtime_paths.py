"""Keep experimental analysis evidence separate from the original archive."""
import os
from pathlib import Path


def recordings_path(root):
    path = Path(os.getenv('KUWETA_RECORDINGS_DIR', 'data/recordings/processed'))
    return path if path.is_absolute() else Path(root) / path
