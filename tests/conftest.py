"""Pytest config for the worktree.

Anchors the project root to THIS worktree (not the outer
``localaiproject`` repo): without this, pytest walks up to the parent
project and resolves ``app.services`` against the older outer tree.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Prepend the worktree root so `app.services.*` resolves to this tree
# rather than the outer ``localaiproject`` checkout.
_WORKTREE_ROOT = Path(__file__).resolve().parent.parent
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))


_TEST_DATA_HOME = tempfile.mkdtemp(prefix="localaiproject-worktree-tests-")
os.environ.setdefault("LOCAL_AI_CONTROL_LAYER_HOME", _TEST_DATA_HOME)
os.environ.setdefault(
    "LOCAL_AI_CONTROL_LAYER_MEMORY_FILE",
    os.path.join(_TEST_DATA_HOME, "local_memory.json"),
)


@atexit.register
def _cleanup_test_data_home():
    shutil.rmtree(_TEST_DATA_HOME, ignore_errors=True)
