"""聊天页的 PoB Build 入口契约。

前端没有 Node 构建链，关键行为因此用源码契约钉住：识别后的主按钮必须调用 `/build`，
并且不能重新出现「尚未实现」的旧提示。后端解析、核心 16 项与防御补充体检由
test_pob_build.py 覆盖。
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FrontendBuildContractTest(unittest.TestCase):
    def test_pob_paste_calls_build_endpoint(self) -> None:
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("else analyzeBuild(text);", app)
        self.assertIn("fetch(`${API}/build`", app)
        self.assertIn("解析并体检", app)
        self.assertIn("核心 16 项与防御补充体检", app)
        self.assertIn("输出分析：单体 / 清图 / Boss", app)
        self.assertIn("data.output_report", app)
        self.assertNotIn("PoB 码解析尚未实现", app)

    def test_all_backend_whitelist_sites_are_recognizable(self) -> None:
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            "pobb\\.in", "pastebin\\.com", "pastebinp\\.com", "pob\\.codes",
            "poe\\.ninja", "maxroll\\.gg", "rentry\\.co", "poedb\\.tw",
        ):
            self.assertIn(marker, app)

    def test_readme_no_longer_claims_build_is_unimplemented(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("PoB 码只能识别不能解析", readme)
        self.assertNotIn("PoB 码解析（前端能识别出来", readme)
        self.assertNotIn("属性需求与获取途径仍未采集", readme)
        self.assertNotIn("技能石 / 万神殿 / Boss 尚未入库", readme)
        self.assertIn("PoB Build 体检", readme)


if __name__ == "__main__":
    unittest.main()
