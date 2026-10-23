"""Build a bundle with a signed application origin, independently of its runtime origin."""
import argparse
import subprocess

REPO_URL = "https://repo.tuneconsole.com/"
RUNTIME_REPO = "https://flathub.org/repo/flathub.flatpakrepo"
APP_ID = "com.tuneconsole.TuneConsole"


def build(repo, output, key, *, repo_url=REPO_URL):
    subprocess.run(["flatpak", "build-bundle", str(repo), str(output), APP_ID,
                    f"--repo-url={repo_url}", f"--gpg-keys={key}",
                    f"--runtime-repo={RUNTIME_REPO}"], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("output")
    parser.add_argument("key")
    args = parser.parse_args()
    build(args.repo, args.output, args.key)
