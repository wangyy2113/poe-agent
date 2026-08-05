"""Path of Building 数据源基础设施：定位仓库、读版本、生成可复现引用。

PoB 是 **P2 来源**（见 config.yaml `sources.priority`）：数值与结构上更接近
一手（自 GGG 客户端 `.dat` 机器导出），但无中文名与页面说明，故不越过 POEDB。

引用的难点：PoB 数据是本地文件，没有 URL，而 `Entity.indexable` 要求非空 URL
（需求 2.5）。解法是把**当前 commit SHA 钉进 GitHub blob 地址**：

    https://github.com/PathOfBuildingCommunity/PathOfBuilding/blob/9e6ea223e/src/Data/ModExplicit.lua

这样引用可点击、可复现，且从 URL 就能看出数据是哪一版。用 `main` 之类的可变
引用是不行的——上游一改，引用就指向了和入库内容不同的数据，等于伪造溯源。

commit SHA 直接读 `.git` 目录，**不调用 `git` 命令**：跨平台规则禁止 shell 调用
平台工具，而且不该假设运行环境装了 git。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from core.config import REPO_ROOT, Config, get_config
from entities.taxonomy import Source

logger = logging.getLogger(__name__)

#: PoB 相对仓库根的数据目录
DATA_DIR = Path("src") / "Data"
TREE_DIR = Path("src") / "TreeData"

#: 常量文件的引用路径（相对 `src/`）。集中在此，免得各处各写一遍。
MISC_URL_FILE = "Data/Misc.lua"


class PoBNotFound(RuntimeError):
    """找不到 PoB 仓库。消息里列出所有尝试过的位置，便于用户直接处理。"""


@dataclass(frozen=True)
class PoBRepo:
    """一个定位好、读出版本的 PoB 仓库快照。"""

    root: Path
    origin: str
    repo_url: str
    #: 完整 commit SHA。取不到时为空串——**不伪造**，宁可让引用退化为分支地址
    #: 并在日志里明说，也不编一个假 SHA 让引用看起来可复现。
    commit: str = ""
    manifest_version: str = ""
    tree_version: str = ""

    # ── 版本标签 ──

    @property
    def short_commit(self) -> str:
        return self.commit[:9] if self.commit else ""

    @property
    def version_label(self) -> str:
        """写进实体 `version` 字段的值，如 `PoB 2.66.2 / tree 3.29`。"""
        parts = []
        if self.manifest_version:
            parts.append(f"PoB {self.manifest_version}")
        if self.tree_version:
            parts.append(f"tree {self.tree_version.replace('_', '.')}")
        return " / ".join(parts) or "PoB 未知版本"

    @property
    def game_version(self) -> str:
        """游戏版本号，如 `3.29`。由天赋树版本推出——PoB 的树版本跟随游戏大版本。"""
        m = re.match(r"(\d+)_(\d+)", self.tree_version or "")
        return f"{m.group(1)}.{m.group(2)}" if m else ""

    @property
    def reproducible(self) -> bool:
        """引用是否真的可复现（钉到了 commit）。"""
        return bool(self.commit)

    # ── 路径与引用 ──

    def data_file(self, relative: str) -> Path:
        """`Data/ModExplicit.lua` → 绝对路径。用 / 分隔的相对路径，跨平台安全。"""
        return self.root / "src" / Path(relative)

    def blob_url(self, relative: str, line: int | None = None) -> str:
        """生成钉住 commit 的 GitHub blob URL。

        `relative` 相对 `src/`，如 `Data/ModExplicit.lua`。
        取不到 commit 时退化为默认分支地址，并**在日志里记警告**——
        此时引用不可复现，不能声称它可复现。
        """
        ref = self.commit or "master"
        if not self.commit:
            logger.warning(
                "PoB commit 未知，引用退化为分支地址 %s，不可复现：%s", ref, relative
            )
        posix = Path(relative).as_posix()
        url = f"{self.repo_url.rstrip('/')}/blob/{ref}/src/{posix}"
        return f"{url}#L{line}" if line else url

    def source_name(self) -> str:
        return Source.POB.value

    def priority(self, cfg: Config | None = None) -> int:
        return (cfg or get_config()).priority_of(Source.POB.value)


# ── 定位 ───────────────────────────────────────────────────

def resolve_pob_root(explicit: str | None = None, cfg: Config | None = None) -> tuple[Path, str]:
    """按优先级定位 PoB 仓库，返回 (路径, 来源说明)。

    顺序：显式参数 → `POB_PATH` 环境变量 → `config.yaml` 的 `sources.pob.path`
    → 仓库同级 `../PathOfBuilding`。**不硬编码本机绝对路径**——那既违反跨平台
    规则，也会把本地目录结构写进版本库。
    """
    cfg = cfg or get_config()
    candidates: list[tuple[str, Path]] = []
    if explicit:
        candidates.append(("显式参数", Path(explicit).expanduser()))
    if os.environ.get("POB_PATH"):
        candidates.append(("env:POB_PATH", Path(os.environ["POB_PATH"]).expanduser()))
    configured = cfg.get("sources.pob.path")
    if configured:
        p = Path(str(configured)).expanduser()
        candidates.append(("config.yaml", p if p.is_absolute() else REPO_ROOT / p))
    candidates.append(("仓库同级目录", REPO_ROOT.parent / "PathOfBuilding"))

    for origin, path in candidates:
        if (path / DATA_DIR).is_dir():
            return path, origin

    tried = "\n".join(f"    {o}: {p}" for o, p in candidates)
    raise PoBNotFound(
        "找不到 Path of Building 仓库（需存在 src/Data/）。已尝试：\n" + tried
        + "\n  设置 POB_PATH 环境变量或 config.yaml 的 sources.pob.path 指向它。"
    )


def load_repo(explicit: str | None = None, cfg: Config | None = None) -> PoBRepo:
    """定位并读出版本信息。"""
    cfg = cfg or get_config()
    root, origin = resolve_pob_root(explicit, cfg)
    repo = PoBRepo(
        root=root,
        origin=origin,
        repo_url=str(
            cfg.get("sources.pob.repo_url",
                    "https://github.com/PathOfBuildingCommunity/PathOfBuilding")
        ),
        commit=read_git_commit(root),
        manifest_version=read_manifest_version(root),
        tree_version=read_latest_tree_version(root),
    )
    logger.info(
        "PoB 仓库：%s（来自 %s）%s commit=%s",
        root, origin, repo.version_label, repo.short_commit or "未知",
    )
    return repo


# ── 版本读取 ───────────────────────────────────────────────

def read_git_commit(root: Path) -> str:
    """读 `.git` 得到 HEAD 的完整 SHA。不调用 `git` 命令。

    覆盖三种形态：detached HEAD（HEAD 里直接是 SHA）、普通分支（HEAD 指向 ref
    文件）、以及 ref 已被 `git gc` 打包进 `packed-refs` 的情况。
    worktree / submodule 时 `.git` 是文件而非目录，也一并处理。
    """
    git = root / ".git"
    if git.is_file():  # worktree / submodule：内容形如 "gitdir: <路径>"
        try:
            text = git.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            git = target if target.is_absolute() else (root / target)
    if not git.is_dir():
        return ""

    head = git / "HEAD"
    if not head.is_file():
        return ""
    try:
        content = head.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

    if re.fullmatch(r"[0-9a-f]{40}", content):
        return content
    if not content.startswith("ref:"):
        return ""

    ref = content.split(":", 1)[1].strip()
    loose = git / Path(ref)
    if loose.is_file():
        try:
            sha = loose.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
        except OSError:
            pass

    packed = git / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith(("#", "^")):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
        except OSError:
            pass
    return ""


def read_manifest_version(root: Path) -> str:
    mf = root / "manifest.xml"
    if not mf.is_file():
        return ""
    try:
        text = mf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r'<Version\b[^>]*\bnumber="([^"]+)"', text) or \
        re.search(r'\bversion="([^"]+)"', text)
    return m.group(1) if m else ""


def read_latest_tree_version(root: Path) -> str:
    """从 `GameVersions.lua` 的 `treeVersionList` 取最后一项。

    不去数 `TreeData/` 的目录名：那里同时有 `3_29` 和 `3_29_ruthless`，
    按字符串排序会挑错，而 `latestTreeVersion` 是上游明确指定的。
    """
    gv = root / "src" / "GameVersions.lua"
    if not gv.is_file():
        return ""
    try:
        text = gv.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"treeVersionList\s*=\s*\{(.*?)\}", text, re.S)
    if not m:
        return ""
    items = re.findall(r'"([^"]+)"', m.group(1))
    return items[-1] if items else ""


def tree_file(repo: PoBRepo) -> Path:
    return repo.root / TREE_DIR / repo.tree_version / "tree.lua"
