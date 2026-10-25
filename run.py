"""Start the ORCA live dashboard.

    python run.py [--port 8000] [--host 127.0.0.1] [--seed 7] [--interval 1.0]
"""
import argparse
import os
import subprocess
import sys

from orca.predictor import MODEL_PATH
from orca.server import serve

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ORCA - cascading failure prediction & recovery")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--interval", type=float, default=1.0, help="seconds per simulation tick at 1x speed")
    args = ap.parse_args()
    if not os.path.exists(MODEL_PATH):
        print("No trained model found - training one now (about a minute)...")
        subprocess.check_call([sys.executable, "-m", "orca.train", "--episodes", "80"])
    serve(args.host, args.port, args.seed, args.interval)
