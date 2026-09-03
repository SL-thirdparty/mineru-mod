# -*- coding: utf-8 -*-
"""目录分层结构与关键路径 smoke 测试。

在迁移后的分层布局下运行，验证：
  1) 顶层分层目录（src/scripts/tests/release/runtime/docs）齐备；
  2) 源码模块可被 py_compile 编译通过（无语法错误）；
  3) mineru.json 的模型路径指向 runtime/models_cache；
  4) .gitignore 已将 runtime/ 忽略（运行态不入库）。

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe -m pytest tests\\smoke_structure_test.py -q
    # 或直接运行：
    runtime\\venv\\Scripts\\python.exe tests\\smoke_structure_test.py
"""
import json
import os
import py_compile
import sys
from pathlib import Path

ROOT = Path(os.environ.get("MINERU_ROOT") or Path(__file__).resolve().parent.parent)

# 顶层分层目录（不含 .tmp 等临时/运行时目录）
TOP_LEVEL = ["src", "scripts", "tests", "release", "runtime", "docs"]

# 每个顶层目录应存在的关键文件
KEY_FILES = {
    "src/webui": ["app.py", "static"],
    "src/tray": ["mineru_tray.py"],
    "src/installer": ["installer_gui.py", "install_flow.py"],
    "scripts": ["install_mineru_uv.py", "download_torch_wheels.py",
                "mineru_tray.spec", "installer_spec.pyinstaller.spec"],
}

# 需 py_compile 的 Python 模块
PY_MODULES = [
    "src/webui/app.py",
    "src/tray/mineru_tray.py",
    "src/installer/installer_gui.py",
    "src/installer/install_flow.py",
    "scripts/install_mineru_uv.py",
    "scripts/download_torch_wheels.py",
]


def test_top_level_dirs():
    missing = [d for d in TOP_LEVEL if not (ROOT / d).is_dir()]
    assert not missing, f"缺少顶层目录: {missing}"


def test_key_files_exist():
    missing = []
    for dir_, names in KEY_FILES.items():
        for n in names:
            if not (ROOT / dir_ / n).exists():
                missing.append(f"{dir_}/{n}")
    assert not missing, f"缺少关键文件: {missing}"


def test_py_compile():
    failed = []
    for rel in PY_MODULES:
        p = ROOT / rel
        if not p.exists():
            failed.append(f"{rel}（缺失）")
            continue
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as e:
            failed.append(f"{rel}（语法错误: {e}）")
    assert not failed, "编译失败:\n" + "\n".join(failed)


def test_models_dir_runtime():
    cfg_path = ROOT / "mineru.json"
    assert cfg_path.is_file(), f"缺少 mineru.json: {cfg_path}"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    md = cfg.get("models-dir") or {}
    for key, val in md.items():
        assert "runtime" in val.replace("\\", "/"), \
            f"models-dir.{key} 未指向 runtime/: {val}"
        # 路径存在性（本机未下载 VLM 时只强制 pipeline）
        p = Path(val)
        if key == "vlm" and not p.exists():
            continue
        assert p.is_dir(), f"models-dir.{key} 目录不存在: {val}"


def test_gitignore_runtime():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "runtime/" in gi, ".gitignore 未忽略 runtime/"


if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{'通过' if failed == 0 else '失败 ' + str(failed) + ' 项'}")
    sys.exit(1 if failed else 0)