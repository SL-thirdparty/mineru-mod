# -*- coding: utf-8 -*-
"""构建 dist 发布分支（P4.1）。

把 release/ 下最终产物（除全量安装包）连同 manifest.json 构建为 orphan dist
分支的单个 commit，供已安装用户的「检查更新」拉取对比（修复/升级二合一）。

manifest.json（dist 分支根）：
  version   应用版本（源自 src/installer/install_flow.py 的 APP_VERSION）
  files     {相对路径: sha256}（dist 分支内全部产物文件）
  installer 全量安装包信息（Release 附件，不走 dist 分支对比）

用法（项目根）：
  runtime\\venv\\Scripts\\python.exe scripts\\publish_dist.py            # 仅构建本地 dist 分支
  runtime\\venv\\Scripts\\python.exe scripts\\publish_dist.py --push     # 构建并推送（先打印内容清单）
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "release")
STAGE = os.path.join(ROOT, ".tmp", "dist_stage")
INDEX = os.path.join(ROOT, ".tmp", "dist_index")
DIST_BRANCH = "dist"
REMOTE = "origin"

APP_DIR = "MinerU文档解析"
EXTRA_FILES = ["使用说明.html", "卸载MinerU.exe"]
INSTALLER = "MinerU安装.exe"
MANIFEST = "manifest.json"


def _git(args, env_extra=None, check=True):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    # -c core.autocrlf=false：禁用行尾转换，blob 与磁盘字节一致，
    # 保证 manifest 哈希 == raw.githubusercontent 实际服务内容
    r = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=ROOT,
                       env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def app_version():
    src = os.path.join(ROOT, "src", "installer", "install_flow.py")
    with open(src, encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        raise RuntimeError("install_flow.py 中未找到 APP_VERSION")
    return m.group(1)


def collect_files():
    """收集发布文件 {相对路径: 绝对路径}，并校验产物齐全。"""
    files = {}
    app = os.path.join(RELEASE, APP_DIR)
    if not os.path.isdir(app):
        raise RuntimeError(f"缺少应用产物目录：{app}（先完成 mineru_app.spec 打包）")
    for dp, _dns, fns in os.walk(app):
        for fn in fns:
            fp = os.path.join(dp, fn)
            files[os.path.relpath(fp, RELEASE).replace(os.sep, "/")] = fp
    for rel in EXTRA_FILES:
        fp = os.path.join(RELEASE, rel)
        if not os.path.isfile(fp):
            raise RuntimeError(f"缺少发布文件：{fp}")
        files[rel] = fp
    return files


def build_stage(files, installer_info):
    """把产物 + manifest.json 复制到暂存目录，返回暂存根。"""
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    os.makedirs(STAGE)
    manifest = {
        "version": app_version(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": {rel: _sha256(fp) for rel, fp in sorted(files.items())},
    }
    if installer_info:
        manifest["installer"] = installer_info
    for rel, fp in files.items():
        dst = os.path.join(STAGE, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(fp, dst)
    with open(os.path.join(STAGE, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def commit_dist_branch():
    """暂存目录 → orphan 单 commit → 覆盖 refs/heads/dist（不影响当前工作区）。
    用独立索引 + --work-tree 指向暂存目录，仓库源码与主索引完全不受影响。"""
    env = {"GIT_INDEX_FILE": INDEX}
    if os.path.exists(INDEX):
        os.remove(INDEX)
    _git(["read-tree", "--empty"], env)
    _git(["-C", STAGE, "--git-dir", os.path.join(ROOT, ".git"),
          "--work-tree", STAGE, "add", "-A"], env)
    tree = _git(["write-tree"], env)
    ver = app_version()
    commit = _git(["commit-tree", tree, "-m",
                   f"MinerU 发布产物 {ver}（{time.strftime('%Y-%m-%d %H:%M')}）"], env)
    _git(["update-ref", f"refs/heads/{DIST_BRANCH}", commit])
    return commit


def summarize(manifest):
    total = len(manifest["files"])
    size = sum(os.path.getsize(os.path.join(STAGE, *r.split("/")))
               for r in manifest["files"])
    return f"版本 {manifest['version']} · {total} 个文件 · {size / 1048576:.1f} MB"


def main():
    ap = argparse.ArgumentParser(description="构建 dist 发布分支")
    ap.add_argument("--push", action="store_true", help="构建后推送到远程（默认不推）")
    args = ap.parse_args()

    installer_path = os.path.join(RELEASE, INSTALLER)
    installer_info = None
    if os.path.isfile(installer_path):
        installer_info = {
            "asset": INSTALLER,
            "sha256": _sha256(installer_path),
            "size": os.path.getsize(installer_path),
            "release_page": "https://github.com/SL-thirdparty/mineru-mod/releases/latest",
        }
    else:
        print(f"[dist] 警告：缺少全量安装包 {installer_path}，manifest 将不含 installer 信息")

    files = collect_files()
    manifest = build_stage(files, installer_info)
    commit = commit_dist_branch()
    print(f"[dist] 已构建分支 {DIST_BRANCH} @ {commit[:12]}")
    print(f"[dist] {summarize(manifest)}")
    if installer_info:
        print(f"[dist] 安装包 {INSTALLER} {installer_info['size'] / 1048576:.1f} MB（Release 附件，不入 dist 分支）")

    if args.push:
        print(f"\n即将推送到远程仓库：SL-thirdparty/mineru-mod 分支 {DIST_BRANCH}")
        print(f"内容：{summarize(manifest)}")
        ans = input("确认推送？(y/N) ").strip().lower()
        if ans != "y":
            print("已取消推送（本地 dist 分支保留）")
            return
        _git(["push", REMOTE, f"{DIST_BRANCH}:{DIST_BRANCH}", "--force-with-lease"])
        print("[dist] 推送完成")


if __name__ == "__main__":
    main()
