"""Open a pull request that moves the pinned oan-helm chart, when oan-helm main differs from the pin.

Run by the last stage of the develop Jenkins build, after the deploy succeeded. It never deploys and
never changes `develop`: it pushes a branch and opens a pull request, and a person merges it.

Two modes. When the build made its own chart release (OAN_CHART_PIN_FILE, ci/deploy/release_images.py) and deployed it,
the pull request records exactly that chart as the pin, on one rolling branch (bot/oan-chart-pin) whose open pull request
is updated by each build, so builds do not pile up pull requests. Otherwise the pin is compared with the head of oan-helm
main (the original behaviour). A commit that only changes the pin never proposes anything: it is that pull request.

  1. ci/deploy/bump_pin.py decides: a newer chart, or changed public values, on oan-helm main (never a
     downgrade; a changed archive for the same version is an error). Nothing to do -> exit 0.
  2. Otherwise it writes the new pin on a new branch, runs the pin tests and a verified prefetch of the
     chart and values, commits, pushes, opens the PR. A change of the platform version is not proposed
     (the images and the pipeline have to move with it): exit 5, reported, nothing pushed.
  3. The workspace is put back to the commit the build checked out.

Needs GH_TOKEN (a GitHub App installation token or a token) with Contents and Pull requests write on this
repository, only when a proposal is actually needed. Exit codes: 0 nothing to do or proposed (or already
proposed), 1 verification or git failed, 3 a proposal is needed but there is no token, 4 GitHub refused,
5 the new chart needs another platform version (a person moves it).

Standard library only; runs on the deploy node's stock Python.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE = "develop"
AUTHOR = ["-c", "user.name=oan-ci", "-c", "user.email=oan-ci@users.noreply.github.com"]


class Refused(Exception):
    pass


def load_bump():
    spec = importlib.util.spec_from_file_location("bump_pin", HERE / "bump_pin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cmd, label=None, cwd=None, env=None, check=True):
    """Run a command; the failure message names `label`, never the arguments (one carries the token)."""
    done = subprocess.run(cmd, cwd=str(cwd or ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True)
    if check and done.returncode != 0:
        raise RuntimeError("%s failed (exit %d):\n%s" % (label or cmd[0], done.returncode, done.stdout[-1500:]))
    return done.stdout


def api(method, path, token, payload=None):
    request = urllib.request.Request("https://api.github.com" + path, method=method,
                                     data=json.dumps(payload).encode() if payload is not None else None)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except ValueError:
            body = {}
        return error.code, body


def slug_of(url):
    found = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url.strip())
    if not found:
        raise ValueError("origin is not a github.com repository: " + url.strip())
    return found.group(1)


def auth(token):
    basic = base64.b64encode(("x-access-token:" + token).encode()).decode()
    return ["-c", "http.extraheader=AUTHORIZATION: basic " + basic]


def pr_body(reason, lines, candidate=False, build_url=""):
    if candidate:
        origin = ["This is the chart release the develop build made from its own images (%s) and deployed to New Dev "
                  "successfully; merging records it as the pin, so a fresh checkout and any manual deploy use the last "
                  "release that worked." % (build_url or "Jenkins build"),
                  "", "One rolling pull request: each build that deploys a newer release updates this one."]
    else:
        origin = ["Pinned to the head of oan-helm `main` at proposal time; the pin names that exact commit and is verified "
                  "by SHA-256."]
    return "\n".join([
        "Moves the pinned oan-helm Livestock chart and public values for the New Dev deploy (opened by the develop Jenkins build).",
        "",
        "```", reason] + lines + ["```", ""] + origin + ["",
        "**Merging this deploys to New Dev.** The develop build runs `ci/deploy/deploy-livestock-dev.sh`: render, route and "
        "Farmer-reference guards, `helm upgrade --install`, rollout checks, and a rollback to the last deployed revision on failure. "
        "A commit that only changes the pin does not release images again.",
        "",
        "Before merging:",
        "- Read the oan-helm CHANGELOG entry and release notes for the new chart or values.",
        "- `promotionReady` and the other profile fields are unchanged by this PR: it moves only the chart commit, version, "
        "SHA-256 values and public values.",
        "- If the chart changes hooks, seed data or migrations, back up the Livestock registry and ID-generator databases "
        "first. A rollback restores the chart and values, not data.",
        "- The platform version is the same as today's, so the images are not affected.",
        ""])


def propose(token, bump=None, runner=run, call=api, root=ROOT, candidate=None):
    bump = bump or load_bump()
    old = json.loads(bump.PROFILE.read_text(encoding="utf-8"))
    if candidate is None:
        found = os.environ.get("OAN_CHART_PIN_FILE", "")
        candidate = found if found and Path(found).exists() else ""
    rolling = bool(candidate)
    if rolling:
        new, reason = bump.from_candidate(old, candidate)
    else:
        new, reason = bump.proposal(old)
    print(reason)
    if new is None:
        return 0
    lines = bump.describe(old, new)
    print("\n".join(lines))
    if new[bump.ROLE]["platformVersion"] != old[bump.ROLE]["platformVersion"]:
        print("The new chart needs platform %s (pinned: %s). Not proposed automatically: run scripts/bump-rp-version.sh and "
              "move RP_VERSION and the openg2p/openg2p-registry-* tags in ci/pipelines/dev.groovy and ci/refactor, then "
              "python3 ci/deploy/bump_pin.py <commit> --write." % (new[bump.ROLE]["platformVersion"], old[bump.ROLE]["platformVersion"]))
        return 5
    if not token:
        print("A pin update is available but no GitHub credential was given (GH_TOKEN); nothing was proposed.")
        return 3

    pin = new[bump.ROLE]
    branch = "bot/oan-chart-pin" if rolling else "bot/oan-chart-pin-%s-%s" % (pin["version"], pin["values"]["sha256"][:8])
    slug = slug_of(runner(["git", "remote", "get-url", "origin"], "git remote get-url", root))
    state = "open" if rolling else "all"
    status, pulls = call("GET", "/repos/%s/pulls?state=%s&head=%s:%s" % (slug, state, slug.split("/")[0], branch), token)
    if status != 200:
        print("GitHub refused to list pull requests (HTTP %s): the credential needs Contents and Pull requests "
              "write on %s." % (status, slug))
        return 4
    if pulls and not rolling:
        print("Already proposed: %s (%s)" % (pulls[0].get("html_url"), pulls[0].get("state")))
        return 0

    title = "Pin OAN chart: " + reason
    body = pr_body(reason, lines, rolling, os.environ.get("BUILD_URL", ""))
    exists = runner(["git"] + auth(token) + ["ls-remote", "--heads", "origin", branch], "git ls-remote", root).strip()
    if rolling or not exists:
        original = runner(["git", "rev-parse", "HEAD"], "git rev-parse", root).strip()
        try:
            changed = bump.apply_pin(old, new, root)
            env = dict(os.environ, PYTHONPATH=str(HERE / "vendor"))      # the vendored PyYAML for the pin tests
            env.pop("OAN_CHART_PIN_FILE", None)                           # these checks are about the pin being written
            runner(["python3", "-m", "unittest", "discover", "-s", "test", "-p", "test_oan_chart.py"],
                   "the pin tests", root, env)
            runner(["python3", "ci/deploy/oan_chart.py", "prefetch", "registry"], "verified prefetch of the new pin", root, env)
            runner(["git", "checkout", "-q", "-b", branch], "git checkout -b", root)
            runner(["git", "add", "--"] + changed, "git add", root)
            runner(["git"] + AUTHOR + ["commit", "-q", "-m", title], "git commit", root)
            push = ["push", "-q"] + (["--force"] if rolling else []) + ["origin", "HEAD:refs/heads/" + branch]
            runner(["git"] + auth(token) + push, "git push", root)
            print("pushed " + branch)
        finally:
            # leave the workspace as the build found it
            runner(["git", "checkout", "-q", "-f", original], "git checkout", root, check=False)
            runner(["git", "branch", "-D", branch], "git branch -D", root, check=False)
    if pulls and rolling:
        status, updated = call("PATCH", "/repos/%s/pulls/%d" % (slug, pulls[0]["number"]), token, {"title": title, "body": body})
        if status != 200:
            print("GitHub refused to update the pull request (HTTP %s): %s. The branch %s is pushed." % (
                status, (updated or {}).get("message"), branch))
            return 4
        print("updated " + pulls[0].get("html_url", "(no url)"))
        return 0
    status, created = call("POST", "/repos/%s/pulls" % slug, token,
                           {"title": title, "head": branch, "base": BASE, "body": body})
    if status not in (200, 201):
        print("GitHub refused to create the pull request (HTTP %s): %s. The branch %s is pushed; open the pull request "
              "from it by hand, or give the credential Pull requests write." % (status, (created or {}).get("message"), branch))
        return 4
    print("opened " + created.get("html_url", "(no url)"))
    return 0


def pin_only_commit():
    spec = importlib.util.spec_from_file_location("release_images", HERE / "release_images.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pin_only_commit(ROOT)


def main():
    try:
        if pin_only_commit():
            print("This commit only changes the chart pin: nothing to propose (it is the pin pull request itself).")
            return 0
        return propose(os.environ.get("GH_TOKEN", ""))
    except (ValueError, KeyError, OSError) as error:
        print("FAIL: %s" % error)
        return 1
    except RuntimeError as error:
        print("FAIL: %s" % error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
