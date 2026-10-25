"""Build the static, server-less site for GitHub Pages into _site/.

    python tools/build_site.py

The dashboard detects that no ORCA server is running and executes the same
Python package in the browser via Pyodide, so the static site is fully live.
"""
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "_site")
MODULES = ["__init__", "topology", "simulator", "logparse", "observer", "predictor", "rca",
           "recovery", "scenarios", "report", "engine", "api"]


def copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def main() -> None:
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    for name in os.listdir(os.path.join(ROOT, "web")):
        copy(os.path.join(ROOT, "web", name), os.path.join(OUT, name))
    for m in MODULES:
        copy(os.path.join(ROOT, "orca", f"{m}.py"), os.path.join(OUT, "orca", f"{m}.py"))
    copy(os.path.join(ROOT, "models", "predictor.json"), os.path.join(OUT, "models", "predictor.json"))
    for name in ("evaluation.json", "evaluation.md"):
        path = os.path.join(ROOT, "reports", name)
        if os.path.exists(path):
            copy(path, os.path.join(OUT, "reports", name))
    media = os.path.join(ROOT, "docs", "media")
    if os.path.isdir(media):
        for name in os.listdir(media):
            copy(os.path.join(media, name), os.path.join(OUT, "media", name))
    open(os.path.join(OUT, ".nojekyll"), "w").close()  # serve files as-is
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(OUT) for f in fs)
    print(f"built {OUT} ({size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
