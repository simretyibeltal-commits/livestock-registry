"""Move the pinned oan-helm chart (ci/deploy/oan-chart.json, role "registry") to a new oan-helm commit.

It reads only from the immutable oan-helm commit you name, computes the SHA-256 of the chart archive
and of the public values, cross-checks the archive against repository/SHA256SUMS at that commit, and
rewrites the pin. It never touches a cluster. The pin changes in a reviewed pull request, never inside
a deploy. The "dashboard" pin is a different chart on its own commit and is not touched here.

    python3 ci/deploy/bump_pin.py <40-hex oan-helm commit>             # show the new pin, change nothing
    python3 ci/deploy/bump_pin.py <commit> --write                     # update oan-chart.json
    python3 ci/deploy/bump_pin.py <commit> --version 1.0.0-dev.21      # default: releases/current.yaml
    python3 ci/deploy/bump_pin.py --latest [--write]                   # find the newest released chart yourself

--latest pins the head of oan-helm `main`: the newest released Livestock chart AND its current public values. The values
file changes in its own pull requests, without a chart release, so the commit that published the archive would
silently drop those changes. A proposal is made only when the chart version, the archive digest or the values
digest differs from the current pin, and never for an older chart version (no downgrade).
The last stage of the develop Jenkins build (ci/deploy/propose_pin_pr.py) runs it after every successful deploy and opens the
pull request, unless the platform version changes: that needs the Dockerfiles and the pipeline moved with it
(see NOTE below), so the stage reports it and leaves the change to a person.

A new platform version is NOT propagated automatically: the images must extend the platform bundled in the chart
(test/test_rp_pin_lockstep.py), and the platform version also appears in ci/pipelines/dev.groovy (RP_VERSION and the
openg2p/openg2p-registry-* image tags) and in ci/refactor. Run scripts/bump-rp-version.sh and update those by hand.

Standard library only; runs on the deploy node's stock Python.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import urllib.request

PROFILE = Path(__file__).with_name("oan-chart.json")
ROOT = Path(__file__).resolve().parents[2]
GIT = "https://github.com/Centre-for-Open-Societal-Systems/oan-helm.git"
RAW = "https://raw.githubusercontent.com/Centre-for-Open-Societal-Systems/oan-helm/"
ROLE = "registry"
CHART = "openg2p-livestock-registry"
VALUES_PATH = "environments/dev/values/livestock.yaml"
LIMIT = 32 * 1024 * 1024


def get(uri):
    with urllib.request.urlopen(uri, timeout=30) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("download exceeds the size limit: " + uri)
    return data


def chart_appversion(archive_bytes, name):
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        member = archive.getmember(name + "/Chart.yaml")
        if not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("unexpected Chart.yaml in the archive")
        text = archive.extractfile(member).read().decode("utf-8")
    found = re.search(r"(?m)^appVersion:\s*['\"]?([^'\"\s]+)", text)
    version = re.search(r"(?m)^version:\s*['\"]?([^'\"\s]+)", text)
    if not found or not version:
        raise ValueError("Chart.yaml has no appVersion/version")
    return version.group(1), found.group(1)


def new_pin(commit, version=None, fetch=get):
    """Return the updated profile (dict) for the oan-helm commit; `fetch` is injectable for tests."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("give the full 40-character oan-helm commit that contains the release")
    base = RAW + commit + "/"
    if version is None:
        current = fetch(base + "releases/current.yaml").decode("utf-8")
        found = re.search(r"(?m)^\s*" + re.escape(CHART) + r":\s*(\S+)\s*$", current)
        if not found:
            raise ValueError("releases/current.yaml at that commit has no " + CHART)
        version = found.group(1)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", version) or version.startswith("0.0.0"):
        raise ValueError("invalid chart version: " + version)

    archive_name = CHART + "-" + version + ".tgz"
    archive = fetch(base + "repository/" + archive_name)
    digest = hashlib.sha256(archive).hexdigest()
    sums = fetch(base + "repository/SHA256SUMS").decode("utf-8")
    listed = re.search(r"(?m)^([0-9a-f]{64})\s+\*?" + re.escape(archive_name) + r"\s*$", sums)
    if not listed:
        raise ValueError(archive_name + " is not listed in repository/SHA256SUMS at that commit")
    if listed.group(1) != digest:
        raise ValueError("archive digest does not match repository/SHA256SUMS at that commit")
    chart_version, platform = chart_appversion(archive, CHART)
    if chart_version != version:
        raise ValueError("archive contains chart " + chart_version + ", expected " + version)

    values = fetch(base + VALUES_PATH)
    profiles = json.loads(PROFILE.read_text(encoding="utf-8"))
    pin = profiles[ROLE]
    pin["repository"] = base + "repository/"
    pin["version"] = version
    pin["sha256"] = digest
    pin["platformVersion"] = platform
    pin["values"] = dict(pin.get("values") or {})
    pin["values"]["path"] = VALUES_PATH
    pin["values"]["sha256"] = hashlib.sha256(values).hexdigest()
    pin["values"]["url"] = base + VALUES_PATH
    return profiles


def version_key(version):
    """Order 1.0.0-dev.20 < 1.0.0-dev.21; a plain 1.0.0 sorts after its dev builds."""
    found = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-dev\.(\d+))?", version)
    if not found:
        raise ValueError("cannot order chart version: " + version)
    major, minor, patch, dev = found.groups()
    return (int(major), int(minor), int(patch), 1 if dev is None else 0, int(dev or 0))


def head_commit(run=None):
    """The 40-character commit at the head of oan-helm main (git ls-remote: no API token, no rate limit)."""
    run = run or (lambda cmd: subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                                             universal_newlines=True).stdout)
    out = run(["git", "ls-remote", GIT, "refs/heads/main"])
    found = re.match(r"([0-9a-f]{40})\s+refs/heads/main\s*$", out.strip())
    if not found:
        raise ValueError("could not read the head of oan-helm main")
    return found.group(1)


def proposal(old, fetch=get, head=head_commit):
    """(new profile, reason) when oan-helm main differs from the pin, else (None, 'up to date ...')."""
    commit = head()
    new = new_pin(commit, None, fetch)
    a, b = old[ROLE], new[ROLE]
    if version_key(b["version"]) < version_key(a["version"]):
        raise ValueError("oan-helm main has chart %s, older than the pin %s: refusing to downgrade" % (b["version"], a["version"]))
    if b["version"] == a["version"] and b["sha256"] != a["sha256"]:
        raise ValueError("the archive for %s changed (%s -> %s): published archives are immutable, so stop and ask oan-helm"
                         % (a["version"], a["sha256"][:12], b["sha256"]))
    if b["version"] == a["version"] and b["values"]["sha256"] == a["values"]["sha256"]:
        return None, "up to date: chart %s and its values are unchanged on oan-helm main" % a["version"]
    if b["version"] == a["version"]:
        return new, "public values changed on oan-helm main (chart stays %s)" % a["version"]
    return new, "newer chart %s released (pinned %s)" % (b["version"], a["version"])


def from_candidate(old, path):
    """(new profiles, reason) for the chart release a develop build made from its own images, as the pin file the
    deploy used (ci/deploy/release_images.py). Same refusals as `proposal`; nothing to do when it is the pinned chart."""
    new = json.loads(Path(path).read_text(encoding="utf-8"))
    a, b = old[ROLE], new[ROLE]
    if version_key(b["version"]) < version_key(a["version"]):
        raise ValueError("the deployed chart %s is older than the pin %s: refusing to downgrade" % (b["version"], a["version"]))
    if b["version"] == a["version"] and b["sha256"] != a["sha256"]:
        raise ValueError("the archive for %s changed (%s -> %s): published archives are immutable, so stop and ask oan-helm"
                         % (a["version"], a["sha256"][:12], b["sha256"]))
    if b["version"] == a["version"] and b["values"]["sha256"] == a["values"]["sha256"]:
        return None, "up to date: the chart deployed by this build (%s) is the pinned chart" % a["version"]
    if b["version"] == a["version"]:
        return new, "public values changed on oan-helm main (chart stays %s)" % a["version"]
    return new, "chart %s, released from this build's images and deployed, replaces the pinned %s" % (b["version"], a["version"])


def update_companions(old, new, root=None):
    """Nothing besides the pin is rewritten for the Livestock profile; the platform version is moved by hand."""
    return []


def describe(old, new):
    """Old -> new lines for the console and the pull request."""
    a, b = old[ROLE], new[ROLE]
    rows = [("version", a.get("version"), b["version"]), ("sha256", a.get("sha256"), b["sha256"]),
            ("platformVersion", a.get("platformVersion"), b["platformVersion"]),
            ("values.sha256", (a.get("values") or {}).get("sha256"), b["values"]["sha256"]),
            ("commit", a["repository"].split("/oan-helm/")[1].split("/")[0], b["repository"].split("/oan-helm/")[1].split("/")[0])]
    return ["%-16s %s -> %s" % row for row in rows]


def apply_pin(old, new, root=None):
    """Write the pin and the files that must agree with it; returns the repository-relative paths changed."""
    PROFILE.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    root = Path(root) if root else ROOT
    return [str(PROFILE.relative_to(root))] + update_companions(old[ROLE], new[ROLE], root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("commit", nargs="?", help="40-character oan-helm commit that contains the chart release")
    parser.add_argument("--latest", action="store_true", help="find the newest released chart and its commit; stop if not newer")
    parser.add_argument("--version", help="chart version (default: releases/current.yaml at that commit)")
    parser.add_argument("--write", action="store_true", help="update ci/deploy/oan-chart.json")
    args = parser.parse_args(argv)
    old = json.loads(PROFILE.read_text(encoding="utf-8"))
    if args.latest:
        if args.commit or args.version:
            parser.error("--latest takes no commit or --version")
        new, reason = proposal(old)
        print(reason)
        if new is None:
            return 0
    else:
        if not args.commit:
            parser.error("give an oan-helm commit, or use --latest")
        new = new_pin(args.commit, args.version)
    print("\n".join(describe(old, new)))
    if not args.write:
        if old[ROLE].get("platformVersion") != new[ROLE]["platformVersion"]:
            print("NOTE: platform version changes; after --write run scripts/bump-rp-version.sh and move RP_VERSION and the "
                  "openg2p/openg2p-registry-* tags in ci/pipelines/dev.groovy and ci/refactor by hand.")
        print("\n(dry run: nothing written; add --write to update " + str(PROFILE.name) + ")")
        return 0
    for path in apply_pin(old, new):
        print("updated " + path)
    if old[ROLE].get("platformVersion") != new[ROLE]["platformVersion"]:
        print("NOTE: platform version changed: run scripts/bump-rp-version.sh and move RP_VERSION and the "
              "openg2p/openg2p-registry-* tags in ci/pipelines/dev.groovy and ci/refactor by hand.")
    print("next: python3 -m unittest discover -s test -p test_oan_chart.py && python3 ci/deploy/oan_chart.py prefetch registry")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        raise SystemExit("FAIL: " + str(error))
