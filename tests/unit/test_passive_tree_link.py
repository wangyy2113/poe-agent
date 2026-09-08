"""官方 PoE1 天赋树链接的确定性、本地解析。"""

from __future__ import annotations

import base64
import unittest

from parsers.passive_tree_link import (
    PassiveTreeLinkError,
    looks_like_passive_tree_link,
    parse_passive_tree_link,
    passive_tree_build,
)


EXAMPLE = (
    "https://www.pathofexile.com/passive-skill-tree/3.15.0/"
    "AAAABgMADI6-HwKSwQQHLJwtH9-wTLNfKoP3ES3r5AAA"
)
OFFICIAL_V5_EXAMPLE = (
    "https://www.pathofexile.com/passive-skill-tree/3.15.0/"
    "AAAABQMADAQHES0fAiycLR9Ms18qg_eOvpLB37Dr5AA="
)


def _url(data: bytes, version: str = "3.29.0") -> str:
    code = base64.urlsafe_b64encode(data).decode().rstrip("=")
    return f"https://www.pathofexile.com/passive-skill-tree/{version}/{code}"


class PassiveTreeLinkTest(unittest.TestCase):
    def test_pob_源码里的官方样例(self) -> None:
        tree = parse_passive_tree_link(EXAMPLE)
        self.assertEqual(tree.serialization_version, 6)
        self.assertEqual(tree.tree_version, "3.15")
        self.assertEqual(tree.class_id, 3)
        self.assertEqual(tree.ascendancy_id, 0)
        self.assertEqual(len(tree.nodes), 12)
        self.assertEqual(tree.nodes[:3], ("36542", "7938", "37569"))
        self.assertEqual(tree.cluster_nodes, ())
        self.assertEqual(tree.mastery_effects, ())

    def test_ggg_开发者文档里的_v5_样例(self) -> None:
        tree = parse_passive_tree_link(OFFICIAL_V5_EXAMPLE)
        self.assertEqual(tree.serialization_version, 5)
        self.assertEqual(tree.class_id, 3)
        self.assertEqual(len(tree.nodes), 12)
        self.assertEqual(tree.nodes[:3], ("1031", "4397", "7938"))

    def test_星团节点与大师效果按_pob_字节序解析(self) -> None:
        data = (
            (6).to_bytes(4, "big")
            + bytes([2, 9, 2])  # Ranger；主升华 1、次升华 2；两个普通节点
            + (100).to_bytes(2, "big") + (200).to_bytes(2, "big")
            + bytes([1]) + (7).to_bytes(2, "big")
            + bytes([1]) + (55).to_bytes(2, "big") + (200).to_bytes(2, "big")
        )
        tree = parse_passive_tree_link(_url(data))
        self.assertEqual(tree.class_id, 2)
        self.assertEqual(tree.ascendancy_id, 1)
        self.assertEqual(tree.secondary_ascendancy_id, 2)
        self.assertEqual(tree.nodes, ("100", "200"))
        self.assertEqual(tree.cluster_nodes, ("65543",))
        self.assertEqual(tree.mastery_effects, (("200", "55"),))

    def test_v62_升华高六位完整保留_bloodline_编号(self) -> None:
        data = (
            (6).to_bytes(4, "big")
            + bytes([0, (6 << 2) | 3, 0])  # Scion；第三升华；Nameless Bloodline
            + bytes([0, 0])  # 无星团节点、无大师效果
        )
        tree = parse_passive_tree_link(_url(data, "3.27.0"))
        self.assertEqual(tree.ascendancy_id, 3)
        self.assertEqual(tree.secondary_ascendancy_id, 6)

    def test_转换成部分_build_时不伪造缺失字段(self) -> None:
        build, tree = passive_tree_build(EXAMPLE)
        self.assertEqual(build.class_name, "Witch")
        self.assertEqual(build.tree_version, "3.15")
        self.assertEqual(build.passive_nodes, tree.nodes)
        self.assertEqual(build.player_stats, {})
        self.assertEqual(build.items, [])
        self.assertTrue(any("仅包含天赋树" in warning for warning in build.warnings))

    def test_识别官方两种路径(self) -> None:
        self.assertTrue(looks_like_passive_tree_link(EXAMPLE))
        self.assertTrue(looks_like_passive_tree_link(
            EXAMPLE.replace("passive-skill-tree", "fullscreen-passive-skill-tree")
        ))
        self.assertFalse(looks_like_passive_tree_link(
            EXAMPLE.replace("pathofexile.com", "pathofexile.com.evil.test")
        ))

    def test_拒绝伪造域名与损坏载荷(self) -> None:
        with self.assertRaisesRegex(PassiveTreeLinkError, "官方"):
            parse_passive_tree_link(EXAMPLE.replace("pathofexile.com", "evil.test"))
        with self.assertRaisesRegex(PassiveTreeLinkError, "Base64"):
            parse_passive_tree_link(
                "https://pathofexile.com/passive-skill-tree/3.29.0/not+urlsafe"
            )

    def test_拒绝未知格式版本和截断计数(self) -> None:
        with self.assertRaisesRegex(PassiveTreeLinkError, "版本 7"):
            parse_passive_tree_link(_url((7).to_bytes(4, "big") + bytes([1, 0])))
        broken = (6).to_bytes(4, "big") + bytes([1, 0, 2]) + (100).to_bytes(2, "big")
        with self.assertRaisesRegex(PassiveTreeLinkError, "节点数量"):
            parse_passive_tree_link(_url(broken))


if __name__ == "__main__":
    unittest.main()
