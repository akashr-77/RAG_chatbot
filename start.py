"""
start.py — starts the full application with one command.

    python start.py

Starts:
  1. backend/main.py       (FastAPI on :8001, spawns MCP servers internally)
  2. npm run dev           (React frontend on :5173)

Ctrl+C cleanly terminates both.
"""

import subprocess
import sys
import time
from shutil import which
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND  = ROOT / "backend" / "main.py"
FRONTEND = ROOT / "frontend"

def main():
    print("[start] Starting RAG Chatbot...")
    print("[start] Backend:  python backend/main.py")
    print("[start] Frontend: npm run dev (frontend/)")
    print("[start] Press Ctrl+C to stop everything.\n")

    backend  = subprocess.Popen([sys.executable, str(BACKEND)])
    # Small delay so FastAPI is up before Vite tries to proxy
    time.sleep(2)
    npm_cmd = which("npm.cmd") or which("npm") or "npm"
    frontend = subprocess.Popen([npm_cmd, "run", "dev"], cwd=str(FRONTEND))

    try:
        backend.wait()
    except KeyboardInterrupt:
        print("\n[start] Shutting down...")
    finally:
        backend.terminate()
        frontend.terminate()
        backend.wait()
        frontend.wait()
        print("[start] All processes stopped.")


if __name__ == "__main__":
    main()