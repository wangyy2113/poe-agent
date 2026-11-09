"""Browser completion and failure follow the server event contract."""
import shutil
import subprocess
import unittest
from pathlib import Path


class FrontendChatStreamTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_stream_completion_failure_and_resource_cleanup(self):
        root = Path(__file__).resolve().parents[2]
        subprocess.run(["node", str(root / "tests/frontend/chat_stream.cjs")],
                       cwd=root, check=True, timeout=15)
