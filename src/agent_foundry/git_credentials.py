"""Git credential helper. Output goes exclusively to Git's private credential pipe."""

import os
import sys
from urllib.parse import urlsplit


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "get":
        return
    values = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
    remote = urlsplit(os.environ.get("FOUNDRY_TOOL_REPOSITORY", "https://github.com/qmdch1/agent-tools.git"))
    if values.get("protocol") != "https" or values.get("host") != remote.netloc:
        return
    if values.get("path", "").removesuffix(".git") != remote.path.lstrip("/").removesuffix(".git"):
        return
    token = os.environ.get("FOUNDRY_GIT_TOKEN", "")
    if token:
        print("username=x-access-token")
        print("password=" + token)


if __name__ == "__main__":
    main()
