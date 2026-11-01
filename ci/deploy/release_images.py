"""Put this build's images into the oan-helm Livestock chart and wait for the chart release that carries them.

Run by the "Release images to oan-helm" stage of every develop build, after the images are pushed and before the
deploy. The stage turns a build into a Helm release that the deploy then uses:

  1. Read the ECR digests of the build (.build/image-digests.tsv: component<TAB>sha256:...).
  2. Rewrite the image tags in charts/openg2p-livestock-registry/values.yaml of oan-helm to
     <commit12>@sha256:<digest> (staffApi, partnerApi, celeryWorker, celeryBeat, dbSeed, staffUi, sanity) and the
     tag and digest of dashboardApi. Only those lines change; the file is edited as text.
  3. Open a pull request to oan-helm main from bot/livestock-images-<commit12>, wait for its checks, merge it.
  4. oan-helm's release workflow gives the chart its next version, packages it and updates releases/current.yaml.
     Wait until the archive at the head of main carries exactly these images.
  5. Write the pin of that release to .build/oan-chart-candidate.json. The deploy reads it (OAN_CHART_PIN_FILE),
     verifies the archive by SHA-256, deploys it and, if the deploy fails, rolls Helm back to the previous revision,
     i.e. the previous release. Nothing here deploys, and the pin in this repository is not changed.

No candidate is written (the stage is UNSTABLE and the deploy stage upgrades nothing) when there is no credential, GitHub refuses, a check fails, the pull request needs a person, or the release does not
appear in time. Skipped without a candidate, and without an error, (a) for a commit that only changes the pin
(ci/deploy/oan-chart.json): such a commit comes from the pin pull request and must not start the cycle again, and
(b) for a commit whose images oan-helm main already carries: a re-run, Replay or rebuild of a commit that an earlier build
released would only produce new digests for the same tag, another chart version and another CHANGELOG entry. One chart
release per commit merged to develop. (If that earlier release failed after the oan-helm pull request merged, the next
new commit picks the release up.) In both skips the reason is written to .build/release-note.txt.

Without a candidate the deploy stage does NOT upgrade: images reach New Dev only through a chart release, so New Dev
stays on the last working release (a failed stage leaves the build UNSTABLE, a deliberate skip does not).

Needs GH_TOKEN: a GitHub App installation token for oan-helm with Contents and Pull requests write and Checks read,
and permission to merge a pull request there. DRY_RUN=1 prints the change and touches nothing.

Exit codes: 0 candidate written (or skipped on purpose), 1 unexpected failure, 3 no token, 4 GitHub refused,
6 a check on the oan-helm pull request failed, 7 the pull request could not be merged (a person must), 8 the release
did not appear in time.

Standard library only; runs on the deploy node's stock Python (3.7 or later).
"""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SLUG = "Centre-for-Open-Societal-Systems/oan-helm"
CHART = "openg2p-livestock-registry"
VALUES_PATH = "charts/%s/values.yaml" % CHART
DIGESTS = ".build/image-digests.tsv"
CANDIDATE = ".build/oan-chart-candidate.json"
NOTE = ".build/release-note.txt"
# chart values section under `registry:` -> ECR component name
REGISTRY = (("staffApi", "staff-api"), ("partnerApi", "partner-api"), ("celeryWorker", "celery"),
            ("celeryBeat", "celery"), ("dbSeed", "db-seed"), ("staffUi", "staff-ui"), ("sanity", "sanity-tests"))
DASHBOARD = "dashboard-api"
PIN_ONLY = {"ci/deploy/oan-chart.json"}


class Stop(Exception):
    """A reason to stop with an exit code and a message for the build log."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def load_bump():
    spec = importlib.util.spec_from_file_location("bump_pin", HERE / "bump_pin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- the values file ---------------------------------------------------------------------------------------------

def read_digests(text):
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not re.fullmatch(r"sha256:[0-9a-f]{64}", parts[1].strip()):
            raise ValueError("bad digest line: " + line[:80])
        result[parts[0].strip()] = parts[1].strip()
    return result


def wanted(image_tag, digests):
    """{values section: 'commit@sha256:..'} for the registry components, plus the dashboard (tag, digest)."""
    if not re.fullmatch(r"[0-9a-f]{12}", image_tag):
        raise ValueError("IMAGE_TAG must be the 12-character commit tag")
    missing = sorted((set(c for _, c in REGISTRY) | {DASHBOARD}) - set(digests))
    if missing:
        raise ValueError("no ECR digest for: " + ", ".join(missing))
    tags = dict((section, "%s@%s" % (image_tag, digests[component])) for section, component in REGISTRY)
    return tags, (image_tag, digests[DASHBOARD])


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _block_end(lines, start, indent):
    """Index after the block that starts at `start`: the next non-blank line indented `indent` or less."""
    index = start + 1
    # Blank lines and comment lines (whatever their column) belong to the block they are in.
    while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("#")
                                  or _indent(lines[index]) > indent):
        index += 1
    return index


def _find(lines, pattern, start, end):
    for index in range(start, end):
        if re.fullmatch(pattern, lines[index]):
            return index
    raise ValueError("values.yaml has no line matching %r" % pattern)


def _set_value(lines, start, end, key, value, indent):
    """Replace `<indent>key: ...` inside lines[start:end]; return True when the text changed."""
    found = _find(lines, r" {%d}%s: \S.*" % (indent, key), start, end)
    new = "%s%s: %s" % (" " * indent, key, value)
    changed = lines[found] != new
    lines[found] = new
    return changed


def rewrite_values(text, registry_tags, dashboard, repositories=None):
    """The values text with the new image tags; everything else is byte for byte as it was.

    `repositories` ({section: expected repository, 'dashboardApi': ...}) makes the rewrite refuse an image block whose
    repository is not the expected ECR repository.
    """
    lines = text.split("\n")
    top = _find(lines, r"registry:\s*", 0, len(lines))
    top_end = _block_end(lines, top, 0)
    for section, tag in registry_tags.items():
        header = _find(lines, r" {2}%s:\s*" % section, top + 1, top_end)
        end = _block_end(lines, header, 2)
        image = _find(lines, r" {4}image:\s*", header + 1, end)
        image_end = _block_end(lines, image, 4)
        if repositories and section in repositories:
            repo = lines[_find(lines, r" {6}repository: \S+", image + 1, image_end)].split(": ", 1)[1].strip()
            if repo != repositories[section]:
                raise ValueError("%s image repository is %s, expected %s" % (section, repo, repositories[section]))
        _set_value(lines, image + 1, image_end, "tag", tag, 6)
    header = _find(lines, r"dashboardApi:\s*", 0, len(lines))
    end = _block_end(lines, header, 0)
    image = _find(lines, r" {2}image:\s*", header + 1, end)
    image_end = _block_end(lines, image, 2)
    if repositories and "dashboardApi" in repositories:
        repo = lines[_find(lines, r" {4}repository: \S+", image + 1, image_end)].split(": ", 1)[1].strip()
        if repo != repositories["dashboardApi"]:
            raise ValueError("dashboardApi image repository is %s, expected %s" % (repo, repositories["dashboardApi"]))
    # Quoted: a tag of only digits (or 1234e5678901) would be read as a number, and the chart would render %!s(int64=...).
    _set_value(lines, image + 1, image_end, "tag", "'%s'" % dashboard[0], 4)
    _set_value(lines, image + 1, image_end, "digest", dashboard[1], 4)
    return "\n".join(lines)


def released_before(text, image_tag):
    """True when the values already carry this commit's images (an earlier build of the same commit released them)."""
    return re.search(r"(?m)^\s+tag: %s@sha256:[0-9a-f]{64}\s*$" % re.escape(image_tag), text) is not None


def expected_repositories(registry, path):
    result = dict((section, "%s/%s/%s" % (registry, path, component)) for section, component in REGISTRY)
    result["dashboardApi"] = "%s/%s/%s" % (registry, path, DASHBOARD)
    return result


def archive_values(archive_bytes):
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        member = archive.getmember("%s/values.yaml" % CHART)
        if not member.isfile() or member.size > 2 * 1024 * 1024:
            raise ValueError("unexpected values.yaml in the chart archive")
        return archive.extractfile(member).read().decode("utf-8")


def carries(values_text, registry_tags, dashboard):
    """True when the values already hold exactly these images (rewriting them changes nothing)."""
    return rewrite_values(values_text, registry_tags, dashboard) == values_text


# ---- GitHub ------------------------------------------------------------------------------------------------------

def api(method, path, token, payload=None):
    request = urllib.request.Request("https://api.github.com" + path, method=method,
                                     data=json.dumps(payload).encode() if payload is not None else None)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except ValueError:
            body = {}
        return error.code, body


def refused(status, body, doing):
    message = (body or {}).get("message", "")
    if status in (401, 403, 404):
        raise Stop(4, "GitHub refused to %s on %s (HTTP %s: %s). The credential needs Contents and Pull requests write and "
                      "Checks read on that repository." % (doing, SLUG, status, message))
    raise Stop(1, "GitHub could not %s (HTTP %s: %s)" % (doing, status, message))


def settled(runs):
    """(done, failed names) for a list of check runs."""
    failed = [r.get("name", "?") for r in runs if r.get("status") == "completed"
              and r.get("conclusion") not in ("success", "neutral", "skipped")]
    done = all(r.get("status") == "completed" for r in runs)
    return done, failed


class Release(object):
    def __init__(self, token, call=api, fetch=None, sleep=time.sleep, clock=time.time, bump=None, out=print):
        self.token, self.call, self.sleep, self.clock, self.out = token, call, sleep, clock, out
        self.bump = bump or load_bump()
        self.fetch = fetch or self.bump.get
        self.poll = 20
        self.checks_timeout = 25 * 60
        self.release_timeout = 30 * 60
        self.note = None

    def gh(self, method, path, payload=None):
        return self.call(method, "/repos/%s%s" % (SLUG, path), self.token, payload)

    # -- steps
    def main_head(self):
        status, body = self.gh("GET", "/git/ref/heads/main")
        if status != 200:
            refused(status, body, "read oan-helm main")
        return body["object"]["sha"]

    def contents(self, ref):
        status, body = self.gh("GET", "/contents/%s?ref=%s" % (VALUES_PATH, ref))
        if status != 200:
            refused(status, body, "read " + VALUES_PATH)
        return base64.b64decode(body["content"]).decode("utf-8"), body["sha"]

    def open_pull_request(self, branch, title, body_text, new_text, main_sha, message):
        status, body = self.gh("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": main_sha})
        if status not in (200, 201) and not (status == 422 and "exists" in str((body or {}).get("message", ""))):
            refused(status, body, "create the branch " + branch)
        text, blob = self.contents(branch)
        if text != new_text:
            status, body = self.gh("PUT", "/contents/" + VALUES_PATH, {
                "message": message, "branch": branch, "sha": blob,
                "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii")})
            if status not in (200, 201):
                refused(status, body, "commit the new image tags")
        status, ref = self.gh("GET", "/git/ref/heads/" + branch)
        if status != 200:
            refused(status, ref, "read the branch " + branch)
        head_sha = ref["object"]["sha"]
        owner = SLUG.split("/")[0]
        status, pulls = self.gh("GET", "/pulls?state=open&head=%s:%s" % (owner, branch))
        if status != 200:
            refused(status, pulls, "list pull requests")
        if pulls:
            return pulls[0]["number"], pulls[0]["html_url"], head_sha
        status, created = self.gh("POST", "/pulls", {"title": title, "head": branch, "base": "main", "body": body_text})
        if status not in (200, 201):
            refused(status, created, "open the pull request")
        return created["number"], created["html_url"], head_sha

    def wait_for_checks(self, head_sha, url):
        deadline = self.clock() + self.checks_timeout
        seen = 0
        while True:
            status, body = self.gh("GET", "/commits/%s/check-runs" % head_sha)
            if status != 200:
                refused(status, body, "read the checks of the pull request")
            runs = (body or {}).get("check_runs", [])
            done, failed = settled(runs)
            if failed:
                raise Stop(6, "These checks failed on %s: %s. The images were not released; fix oan-helm or the images and "
                              "rerun the build." % (url, ", ".join(failed)))
            if runs and done:
                return
            if not runs:
                seen += 1
                if seen >= 9:        # no workflow reported in three minutes: let the merge decide
                    self.out("No checks reported for %s; trying the merge." % url)
                    return
            if self.clock() > deadline:
                raise Stop(8, "The checks on %s did not finish in time." % url)
            self.sleep(self.poll if runs else 20)

    def merge(self, number, url, head_sha, title, message):
        status, body = self.gh("PUT", "/pulls/%d/merge" % number, {
            "merge_method": "squash", "sha": head_sha, "commit_title": title, "commit_message": message})
        if status == 200:
            return
        text = (body or {}).get("message", "")
        if status in (403, 405, 409, 422):
            raise Stop(7, "%s could not be merged automatically (HTTP %s: %s). A person must merge it (a review may be "
                          "required, or the App may lack permission to merge there); the release then carries these images "
                          "and the next build deploys it." % (url, status, text))
        refused(status, body, "merge the pull request")

    def drop_branch(self, branch):
        self.gh("DELETE", "/git/refs/heads/" + branch)       # best effort; the squash merge made it redundant

    def release_for(self, tags, dashboard, old_version):
        """Wait for the chart at the head of main that carries these images; return its pin profiles."""
        deadline = self.clock() + self.release_timeout
        last = "no release yet"
        key = self.bump.version_key
        while True:
            try:
                head = self.main_head()
                captured = {}

                def fetch(uri):
                    data = self.fetch(uri)
                    if uri.endswith(".tgz"):
                        captured["archive"] = data
                    return data

                profiles = self.bump.new_pin(head, None, fetch)
                pin = profiles["registry"]
                if key(pin["version"]) < key(old_version):
                    raise Stop(1, "oan-helm main has chart %s, older than the pinned %s" % (pin["version"], old_version))
                if carries(archive_values(captured["archive"]), tags, dashboard):
                    return profiles
                last = "chart %s on oan-helm main does not carry these images yet" % pin["version"]
            except Stop:
                raise
            except (ValueError, KeyError, OSError, tarfile.TarError) as error:
                last = "%s: %s" % (error.__class__.__name__, error)
            if self.clock() > deadline:
                raise Stop(8, "The chart release with these images did not appear in time (%s). Check the Release Helm "
                              "charts workflow in oan-helm; the next build picks the release up." % last)
            self.sleep(self.poll)

    # -- the whole stage
    def run(self, env, root, digests_text):
        image_tag = env["IMAGE_TAG"]
        tags, dashboard = wanted(image_tag, read_digests(digests_text))
        repositories = expected_repositories(env["ECR_REGISTRY"], env.get("ECR_PATH", "openg2p/livestock-registry"))
        old = json.loads((root / "ci/deploy/oan-chart.json").read_text(encoding="utf-8"))["registry"]
        main_sha = self.main_head()
        text, _ = self.contents("main")
        new_text = rewrite_values(text, tags, dashboard, repositories)
        subject = env.get("COMMIT_SUBJECT", "").strip()[:100]
        self.out("images of build %s:" % image_tag)
        for section, tag in sorted(tags.items()):
            self.out("  %-13s %s" % (section, tag))
        self.out("  %-13s %s %s" % ("dashboardApi", dashboard[0], dashboard[1]))
        if released_before(text, image_tag):
            self.note = ("Commit %s was already released to oan-helm by an earlier build: a re-run or rebuild of the same "
                         "commit does not create another chart release, so nothing is deployed and New Dev stays on the "
                         "last working release." % image_tag)
            self.out(self.note)
            return None
        if env.get("DRY_RUN"):
            self.out("DRY_RUN: %s would change." % VALUES_PATH)
            return None
        branch = "bot/livestock-images-" + image_tag
        title = "Livestock registry images %s%s" % (image_tag, (": " + subject) if subject else "")
        body = "\n".join([
            "Image tags and digests of Livestock Registry develop build `%s` (source commit `%s`)." % (
                image_tag, env.get("SOURCE_COMMIT", image_tag)),
            "", "Jenkins build: %s" % env.get("BUILD_URL", "(unknown)"), "",
            "Opened by the develop Jenkins build of livestock-registry. Only the image `tag` (and the dashboard `digest`) "
            "lines of `%s` change. Merging releases the next `%s` chart, which the same build then deploys to New Dev; if "
            "that fails the build rolls Helm back to the previous release." % (VALUES_PATH, CHART), ""])
        number, url, head_sha = self.open_pull_request(branch, title, body, new_text, main_sha, title)
        self.out("pull request: " + url)
        self.wait_for_checks(head_sha, url)
        self.merge(number, url, head_sha, title, "Livestock develop commit %s." % env.get("SOURCE_COMMIT", image_tag))
        self.out("merged " + url)
        self.drop_branch(branch)
        profiles = self.release_for(tags, dashboard, old["version"])
        pin = profiles["registry"]
        self.out("released chart %s (oan-helm %s)" % (pin["version"], pin["repository"].split("/oan-helm/")[1][:12]))
        return profiles


def pin_only_commit(root, runner=None):
    """True when the checked-out commit changes nothing but the chart pin (it is the pin pull request merged)."""
    runner = runner or (lambda cmd: subprocess.run(cmd, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   universal_newlines=True))
    done = runner(["git", "diff", "--name-only", "HEAD~1", "HEAD"])
    if done.returncode != 0:
        return False
    names = set(n for n in done.stdout.split("\n") if n.strip())
    return bool(names) and names <= PIN_ONLY


def write_note(root, message):
    """Why there is no candidate on purpose; the deploy stage prints it and does not mark the build UNSTABLE."""
    target = Path(root) / NOTE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(message + "\n", encoding="utf-8")


def main(env=None, root=ROOT, call=api):
    env = dict(os.environ if env is None else env)
    root = Path(root)
    try:
        if pin_only_commit(root):
            note = ("This commit only changes the chart pin: its images are not released again (that would start the "
                    "cycle over), so nothing is deployed and New Dev stays on the last working release.")
            print(note)
            write_note(root, note)
            return 0
        for name in ("IMAGE_TAG", "ECR_REGISTRY"):
            if not env.get(name):
                raise Stop(1, name + " is not set")
        if not env.get("GH_TOKEN") and not env.get("DRY_RUN"):
            raise Stop(3, "No GitHub credential (GH_TOKEN): the images were not released to oan-helm.")
        digests = (root / DIGESTS).read_text(encoding="utf-8")
        release = Release(env.get("GH_TOKEN", ""), call=call)
        profiles = release.run(env, root, digests)
        if profiles is None:
            if release.note:
                write_note(root, release.note)
            return 0
        target = root / CANDIDATE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")
        print("candidate pin written to " + CANDIDATE)
        return 0
    except Stop as stop:
        print("FAIL: %s" % stop)
        return stop.code
    except (ValueError, KeyError, OSError) as error:
        print("FAIL: %s: %s" % (error.__class__.__name__, error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
