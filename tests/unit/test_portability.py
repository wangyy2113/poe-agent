"""平台中立性静态规则与 HTTP 缓存文件名契约。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kb.fetcher import cache_path_for
from scripts.check_portability import ROOT, scan, scan_file


class PortabilityScannerTest(unittest.TestCase):
    def _scan_text(self, text: str) -> set[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(text, encoding="utf-8")
            return {issue.code for issue in scan_file(path)}

    def test_rejects_platform_modules_shell_tools_and_hardcoded_python(self) -> None:
        codes = self._scan_text(
            "import fcntl\n"
            "import os\n"
            "import os.path as osp\n"
            "import subprocess\n"
            "PYTHON = '/Applications/Python/bin/python2.7'\n"
            "os.fork()\n"
            "os.path.join('data', 'x')\n"
            "osp.join('data', 'y')\n"
            "subprocess.run(['grep', 'x'], shell=True)\n"
        )
        self.assertTrue({"PORT001", "PORT002", "PORT003", "PORT004", "PORT005", "PORT006"} <= codes)

    def test_rejects_implicit_text_encoding_but_allows_binary_io(self) -> None:
        codes = self._scan_text(
            "from pathlib import Path\n"
            "Path('a').read_text()\n"
            "Path('b').write_text('x')\n"
            "Path('chained.txt').open('r')\n"
            "open('c.txt', 'r')\n"
            "open('d.bin', 'rb')\n"
            "Path('e.bin').open('rb')\n"
        )
        self.assertEqual(codes, {"PORT007"})

    def test_repository_python_sources_pass(self) -> None:
        issues = scan([ROOT / "main.py", *(
            path for dirname in (
                "agents", "api", "calc", "core", "craft", "entities", "kb",
                "orchestrator", "parsers", "retrieval", "scripts", "tools",
            ) for path in (ROOT / dirname).rglob("*.py")
        )])
        self.assertEqual([], issues, "\n".join(issue.render() for issue in issues))


class CacheFilenameTest(unittest.TestCase):
    def test_url_cache_name_is_short_stable_hash_without_query_text(self) -> None:
        url = "https://poedb.tw/cn/" + "非常长的路径" * 100 + "?bad=<>:\\|?*&x=1"
        first = cache_path_for(Path("cache"), url)
        second = cache_path_for(Path("cache"), url)
        other = cache_path_for(Path("cache"), url + "2")
        self.assertEqual(first, second)
        self.assertNotEqual(first.name, other.name)
        self.assertLessEqual(len(first.name), 64)
        self.assertFalse(set('<>:"/\\|?*') & set(first.name))
        self.assertNotIn("非常长的路径", first.name)


if __name__ == "__main__":
    unittest.main()
