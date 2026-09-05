# -*- coding: utf-8 -*-
"""为 release/ 写入构建信息 build_info.json（版本带构建时间戳）。

背景：安装器写 .install_manifest.json 的版本、publish_dist 写 dist manifest 的版本，
若只写 APP_VERSION（如 1.0.0），任何带构建时间戳的远端版本都比它“新”，
导致新装用户点击「检查更新」误判有更新；且无法区分同版本热修复。

本脚本在打包链路的「构建主程序之后、构建安装器之前」执行一次：
  release/build_info.json = {"app_version": "1.0.0", "version": "1.0.0.202609051215", "created": "..."}

- 安装器构建时把 build_info.json 打入 _MEIPASS，install_flow 安装时写入本地清单版本；
- publish_dist 发布时读取同一文件生成 dist manifest 版本（缺失则现场生成）。
两者共用同一时间戳，保证「新装即最新」与「同版本热修复可被检出」同时成立。

用法（项目根）：
  runtime\\venv\\Scripts\\python.exe scripts\\stamp_build.py [--force]
  --force：忽略已存在的时间戳重新生成（改动了主程序文件后必须重跑）
"""
import argparse
import json
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "release")
BUILD_INFO = os.path.join(RELEASE, "build_info.json")


def app_version():
    src = os.path.join(ROOT, "src", "installer", "install_flow.py")
    with open(src, encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        raise RuntimeError("install_flow.py 中未找到 APP_VERSION")
    return m.group(1)


def main():
    ap = argparse.ArgumentParser(description="为 release/ 生成构建版本信息")
    ap.add_argument("--force", action="store_true",
                    help="忽略已有 build_info.json 重新生成")
    args = ap.parse_args()

    if not os.path.isdir(RELEASE):
        raise SystemExit(f"缺少发布目录：{RELEASE}（先构建主程序与卸载器）")
    ver = app_version()

    if os.path.isfile(BUILD_INFO) and not args.force:
        try:
            with open(BUILD_INFO, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("app_version") == ver and old.get("version"):
                print(f"[stamp] 复用已有构建信息：{old['version']}（--force 可重新生成）")
                return
        except Exception:
            pass

    stamp = f"{ver}.{time.strftime('%Y%m%d%H%M%S')}"
    info = {
        "app_version": ver,
        "version": stamp,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(BUILD_INFO, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print(f"[stamp] 已生成构建信息：{stamp}（{BUILD_INFO}）")


if __name__ == "__main__":
    main()
