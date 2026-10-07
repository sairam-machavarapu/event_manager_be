"""Apply migrations, then start the single-instance demo API."""

import os
import subprocess
import sys


def main():
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            os.environ.get("PORT", "8000"),
            "--no-proxy-headers",
        ],
    )


if __name__ == "__main__":
    main()
