"""把 shell 脚本 base64 后送到远端执行 —— 绕开 PowerShell 的编码/引号破坏。

用法（在 stock-advisor 目录下）：
    python r\"<本地脚本路径>\" --host root@101.43.25.101
本地脚本路径指一个 .sh 文件。
"""
import base64
import subprocess
import sys
from pathlib import Path

KEY = str(Path.home() / ".ssh" / "sa_deploy_ed25519")


def main() -> int:
    args = sys.argv[1:]
    host = "root@101.43.25.101"
    script = None
    i = 0
    while i < len(args):
        if args[i] == "--host":
            host = args[i + 1]
            i += 2
        else:
            script = Path(args[i])
            i += 1
    if script is None or not script.exists():
        print("用法: python _remote.py <script.sh> [--host user@ip]")
        return 2
    raw = script.read_bytes().replace(b"\r\n", b"\n")
    b64 = base64.b64encode(raw).decode("ascii")
    # 分块传，避免命令行长度限制
    cmd = ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
           host, "echo %s | base64 -d | bash" % b64]
    p = subprocess.run(cmd, capture_output=True)
    sys.stdout.write(p.stdout.decode("utf-8", "replace"))
    err = p.stderr.decode("utf-8", "replace")
    if err.strip():
        sys.stdout.write("\n--- stderr ---\n" + err)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
