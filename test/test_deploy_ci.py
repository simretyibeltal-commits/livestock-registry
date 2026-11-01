"""Rollback of a failed New Dev deploy and the chart-pin updater, with stand-in helm/kubectl/GitHub.

No network, no cluster. Standard library only (runs on the Jenkins agent's stock Python).
    python3 -m unittest discover -s test -p test_deploy_ci.py -v
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HELM = r'''#!/usr/bin/env bash
echo "helm $*" >> "$LOG"
case "$1" in
  template) if [ -n "${TEMPLATE_OUT:-}" ]; then printf '%s\n' "${TEMPLATE_OUT}"; else echo "kind: ConfigMap"; fi ;;
  get)      if [ -n "${NO_RELEASE:-}" ]; then echo "Error: release: not found" >&2; exit 1; fi; echo "registry: {}" ;;
  history)  case " $* " in *" json "*) echo "${HISTORY_JSON}" ;; *) echo "REVISION STATUS" ;; esac ;;
  upgrade)  exit "${UPGRADE_RC:-0}" ;;
  rollback) exit "${ROLLBACK_RC:-0}" ;;
esac
exit 0
'''

KUBECTL = r'''#!/usr/bin/env bash
echo "kubectl $*" >> "$LOG"
case "$1" in
  create) echo "{}" ;;
  rollout) case "$*" in *"${ROLLOUT_FAIL_ON:-NONE}"*) exit 1 ;; esac ;;
esac
exit 0
'''

# Stands in for ci/deploy/oan_chart.py (which would download the pinned chart).
CHART_STUB = r'''import os, sys
a = sys.argv[1:]
if a[0] == "fetch":
    os.makedirs(a[2], exist_ok=True)
    p = os.path.join(a[2], "chart.tgz"); open(p, "w").write("x"); print(p)
elif a[0] == "values":
    open(a[2], "w").write("registry: {}\n"); print(a[2])
elif a[0] == "version":
    print("1.0.0-dev.20")
elif a[0] == "strip-images":
    open("strip.called", "w").write(a[2])
'''

# The route guard (exit code GUARD_ROUTES_RC) and the Farmer-reference guard: it runs twice, before the
# upgrade on the render and after the rollouts on the live Deployments; GUARD_FAIL_ON=<n> fails the nth run.
ROUTES_STUB = "import os, sys\nsys.stdin.read()\nsys.exit(int(os.environ.get('GUARD_ROUTES_RC', '0')))\n"
GUARD_STUB = r'''import os, sys
sys.stdin.read()
n = 1
if os.path.exists("guard.count"):
    n = int(open("guard.count").read()) + 1
open("guard.count", "w").write(str(n))
sys.exit(1 if str(n) == os.environ.get("GUARD_FAIL_ON") else 0)
'''
DASHBOARD_STUB = '#!/usr/bin/env bash\nexit "${DASHBOARD_UI_RC:-0}"\n'

DEPLOYED = json.dumps([{"revision": 2, "status": "superseded"},
                       {"revision": 3, "status": "deployed", "chart": "openg2p-livestock-registry-1.0.0-dev.19"},
                       {"revision": 4, "status": "failed"}])

ECR = "123456789012.dkr.ecr.ap-south-1.amazonaws.com"
DIGEST = dict((c, "sha256:" + ("%x" % (i + 1)) * 64) for i, c in enumerate(
    ["staff-api", "partner-api", "celery", "db-seed", "sanity-tests", "staff-ui", "dashboard-api"]))


def rendered(tag="abcdef123456", skip=None):
    """What `helm template` prints when the chart carries this build's images."""
    base = ECR + "/openg2p/livestock-registry/"
    refs = ["%s%s:%s@%s" % (base, c, tag, DIGEST[c]) for c in
            ("staff-api", "partner-api", "celery", "db-seed", "staff-ui", "sanity-tests")]
    refs.append("%sdashboard-api@%s" % (base, DIGEST["dashboard-api"]))
    return "\n".join("          image: \"%s\"" % r for r in refs if r != skip)


class DeployRollbackTests(unittest.TestCase):
    """ci/deploy/deploy-livestock-dev.sh: a failure from the upgrade onwards puts the release back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        for rel in ("ci/deploy", "ci/refactor", "ci/dashboard"):
            (self.repo / rel).mkdir(parents=True)
        for name in ("deploy-livestock-dev.sh", "release-rollback.sh"):
            shutil.copy(ROOT / "ci/deploy" / name, self.repo / "ci/deploy" / name)
        (self.repo / "ci/deploy/oan_chart.py").write_text(CHART_STUB)
        (self.repo / "ci/check-new-dev-routes.py").write_text(ROUTES_STUB)
        (self.repo / "ci/refactor/deployment_guard.py").write_text(GUARD_STUB)
        (self.repo / "ci/dashboard/deploy-ui.sh").write_text(DASHBOARD_STUB)
        self.bin = base / "bin"
        self.bin.mkdir()
        for name, body in (("helm", HELM), ("kubectl", KUBECTL)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.log = base / "calls.log"
        self.log.write_text("")

    def run_deploy(self, **env):
        e = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], LOG=str(self.log),
                 KUBECONFIG="/nonexistent", ECR_REGISTRY="123456789012.dkr.ecr.ap-south-1.amazonaws.com",
                 IMAGE_TAG="abcdef123456", BUILD_NUMBER="7", DASHBOARD_UI_ECR="oan/x", DASHBOARD_UI_SHA="deadbeefcafe",
                 HISTORY_JSON=DEPLOYED)
        e.update(env)
        out = base_out = Path(self.tmp.name) / "out.txt"
        with open(str(out), "w") as sink:       # a file, not a pipe: the log watcher's last `sleep` must not block us
            done = subprocess.run(["bash", str(self.repo / "ci/deploy/deploy-livestock-dev.sh")], cwd=str(self.repo),
                                  env=e, stdout=sink, stderr=subprocess.STDOUT, universal_newlines=True)
        return done.returncode, base_out.read_text(), self.log.read_text()

    def report(self):
        return (self.repo / "deploy-failure-7.txt").read_text()

    def test_success_does_not_roll_back(self):
        rc, out, calls = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertIn("rollback target: revision 3", out)
        self.assertNotIn("helm rollback", calls)
        self.assertIn("helm upgrade --install livestock-registry", calls)
        self.assertNotIn("--atomic", calls)
        self.assertFalse((self.repo / "deploy-failure-7.txt").exists())

    def test_failed_upgrade_rolls_back_to_last_deployed_revision(self):
        rc, out, calls = self.run_deploy(UPGRADE_RC="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("helm rollback livestock-registry 3 -n livestock", calls)   # 3 = last `deployed`, not the failed 4
        self.assertIn("== failure: deploy failed", self.report())

    def test_failed_rollout_rolls_back(self):
        rc, out, calls = self.run_deploy(ROLLOUT_FAIL_ON="staff-portal-ui")
        self.assertNotEqual(rc, 0)
        self.assertIn("helm rollback livestock-registry 3 -n livestock", calls)

    def test_failed_dashboard_api_rollout_rolls_back(self):
        rc, out, calls = self.run_deploy(ROLLOUT_FAIL_ON="dashboard-api")
        self.assertNotEqual(rc, 0)
        self.assertIn("helm rollback livestock-registry 3 -n livestock", calls)

    def test_failed_dashboard_ui_deploy_rolls_back(self):
        rc, out, calls = self.run_deploy(DASHBOARD_UI_RC="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("helm rollback livestock-registry 3 -n livestock", calls)

    def test_failed_guard_on_the_live_deployments_rolls_back(self):
        rc, out, calls = self.run_deploy(GUARD_FAIL_ON="2")
        self.assertNotEqual(rc, 0)
        self.assertIn("helm upgrade --install", calls)
        self.assertIn("helm rollback livestock-registry 3 -n livestock", calls)

    def test_diagnostics_are_saved_before_the_rollback(self):
        rc, out, calls = self.run_deploy(UPGRADE_RC="1")
        lines = calls.splitlines()
        rollback = next(i for i, l in enumerate(lines) if l.startswith("helm rollback"))
        events = max(i for i, l in enumerate(lines[:rollback]) if l.startswith("kubectl get events"))
        self.assertLess(events, rollback)

    def test_first_install_failure_has_nothing_to_roll_back_to(self):
        rc, out, calls = self.run_deploy(UPGRADE_RC="1", NO_RELEASE="1", HISTORY_JSON="[]")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("helm rollback", calls)
        self.assertIn("no earlier deployed revision", out)

    def test_failure_without_a_deployed_revision_does_not_roll_back(self):
        rc, out, calls = self.run_deploy(UPGRADE_RC="1", HISTORY_JSON=json.dumps([{"revision": 1, "status": "failed"}]))
        self.assertNotEqual(rc, 0)
        self.assertNotIn("helm rollback", calls)

    def test_a_failed_rollback_still_fails_the_build(self):
        rc, out, calls = self.run_deploy(UPGRADE_RC="1", ROLLBACK_RC="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("ROLLBACK FAILED", out)

    def test_failure_before_the_upgrade_changes_nothing_and_rolls_nothing_back(self):
        rc, out, calls = self.run_deploy(GUARD_ROUTES_RC="1")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("helm upgrade", calls)
        self.assertNotIn("helm rollback", calls)

    def test_interrupted_release_is_refused_before_any_change(self):
        history = json.dumps([{"revision": 3, "status": "deployed"}, {"revision": 4, "status": "pending-upgrade"}])
        rc, out, calls = self.run_deploy(HISTORY_JSON=history)
        self.assertNotEqual(rc, 0)
        self.assertIn("pending Helm operation", out)
        self.assertNotIn("helm upgrade", calls)
        self.assertNotIn("helm rollback", calls)

    # ---- chart release mode: the develop build released its images in oan-helm and deploys that release

    def chart_release(self, **env):
        (self.repo / ".build").mkdir(exist_ok=True)
        (self.repo / ".build/oan-chart-candidate.json").write_text("{}")
        (self.repo / ".build/image-digests.tsv").write_text("".join("%s\t%s\n" % kv for kv in sorted(DIGEST.items())))
        values = dict(OAN_CHART_PIN_FILE=".build/oan-chart-candidate.json", IMAGE_DIGESTS_FILE=".build/image-digests.tsv",
                      TEMPLATE_OUT=rendered())
        values.update(env)
        return self.run_deploy(**values)

    def test_the_rollback_target_names_the_release_it_restores(self):
        rc, out, calls = self.run_deploy()
        self.assertIn("rollback target: revision 3 (openg2p-livestock-registry-1.0.0-dev.19, the last working release)", out)

    def test_without_a_chart_release_the_build_images_are_set_on_the_pinned_chart(self):
        rc, out, calls = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertIn("registry.staffApi.image.tag=abcdef123456", calls)
        self.assertFalse((self.repo / "strip.called").exists())

    def test_chart_release_mode_deploys_the_chart_images_not_flags(self):
        rc, out, calls = self.chart_release()
        self.assertEqual(rc, 0, out)
        self.assertIn("chart release mode: deploying 1.0.0-dev.20", out)
        self.assertIn("chart release carries exactly this build's 7 images", out)
        upgrade = next(l for l in calls.splitlines() if l.startswith("helm upgrade"))
        self.assertNotIn(".image.", upgrade)                      # no image flag overrides the chart's tag@digest
        self.assertIn("dashboardApi.enabled=true", upgrade)
        self.assertTrue((self.repo / "strip.called").exists())    # the saved release values lose their old images
        self.assertNotIn("helm rollback", calls)

    def test_chart_release_that_does_not_run_this_builds_images_stops_before_any_change(self):
        for template in (rendered(tag="000000000000"), rendered(skip="%s/openg2p/livestock-registry/dashboard-api@%s" % (
                ECR, DIGEST["dashboard-api"])), "kind: ConfigMap"):
            rc, out, calls = self.chart_release(TEMPLATE_OUT=template)
            self.assertNotEqual(rc, 0)
            self.assertIn("does not run this build's images", out)
            self.assertNotIn("helm upgrade", calls)
            self.assertNotIn("helm rollback", calls)

    def test_failed_chart_release_deploy_rolls_back_to_the_previous_release(self):
        rc, out, calls = self.chart_release(UPGRADE_RC="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("helm rollback livestock-registry 3 -n livestock", calls)

    def test_chart_release_mode_needs_the_digests(self):
        rc, out, calls = self.chart_release(IMAGE_DIGESTS_FILE="")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("helm upgrade", calls)

    def test_the_upgrade_is_marked_started_only_just_before_helm_upgrade(self):
        text = (ROOT / "ci/deploy/deploy-livestock-dev.sh").read_text()
        self.assertLess(text.index("\nUPGRADE_STARTED=1"), text.index("if ! helm upgrade --install"))
        self.assertLess(text.index("helm template"), text.index("\nUPGRADE_STARTED=1"))
        self.assertNotIn("--atomic", text)


def load_bump():
    spec = importlib.util.spec_from_file_location("bump_pin_under_test", ROOT / "ci/deploy/bump_pin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_archive(version="1.0.0-dev.21", app="1.2.2-rc.700"):
    body = "apiVersion: v2\nname: openg2p-livestock-registry\nversion: %s\nappVersion: %s\n" % (version, app)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        data = body.encode()
        info = tarfile.TarInfo("openg2p-livestock-registry/Chart.yaml")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class BumpPinTests(unittest.TestCase):
    COMMIT = "b" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bump = load_bump()
        profile = Path(self.tmp.name) / "oan-chart.json"
        shutil.copy(ROOT / "ci/deploy/oan-chart.json", profile)
        self.bump.PROFILE = profile
        self.archive = fake_archive()
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.files = {
            "releases/current.yaml": b"charts:\n  openg2p-livestock-registry: 1.0.0-dev.21\n  openg2p-farmer-registry: 1.0.0-dev.20\n",
            "repository/openg2p-livestock-registry-1.0.0-dev.21.tgz": self.archive,
            "repository/SHA256SUMS": ("%s  openg2p-livestock-registry-1.0.0-dev.21.tgz\n" % self.digest).encode(),
            "environments/dev/values/livestock.yaml": b"registry: {}\n",
        }

    def fetch(self, uri):
        path = uri.split("/oan-helm/" + self.COMMIT + "/")[1]
        return self.files[path]

    def test_pin_is_rebuilt_from_the_commit(self):
        profiles = self.bump.new_pin(self.COMMIT, fetch=self.fetch)
        pin = profiles["registry"]
        self.assertEqual(pin["version"], "1.0.0-dev.21")          # taken from releases/current.yaml
        self.assertEqual(pin["sha256"], self.digest)
        self.assertEqual(pin["platformVersion"], "1.2.2-rc.700")  # read from the archive
        self.assertTrue(pin["repository"].endswith("/oan-helm/" + self.COMMIT + "/repository/"))
        self.assertEqual(pin["values"]["url"], pin["repository"][:-len("repository/")] + pin["values"]["path"])
        self.assertEqual(pin["values"]["sha256"], hashlib.sha256(b"registry: {}\n").hexdigest())
        self.assertEqual(pin["namespace"], "livestock")           # untouched fields stay
        self.assertIs(pin["promotionReady"], True)
        self.assertEqual(profiles["dashboard"], json.loads((ROOT / "ci/deploy/oan-chart.json").read_text())["dashboard"])

    def test_archive_not_matching_sha256sums_is_rejected(self):
        self.files["repository/SHA256SUMS"] = ("%s  openg2p-livestock-registry-1.0.0-dev.21.tgz\n" % ("0" * 64)).encode()
        with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
            self.bump.new_pin(self.COMMIT, fetch=self.fetch)

    def test_version_missing_from_sha256sums_is_rejected(self):
        self.files["repository/SHA256SUMS"] = b""
        with self.assertRaisesRegex(ValueError, "not listed"):
            self.bump.new_pin(self.COMMIT, fetch=self.fetch)

    def test_archive_with_another_chart_version_is_rejected(self):
        self.files["repository/openg2p-livestock-registry-1.0.0-dev.21.tgz"] = fake_archive(version="1.0.0-dev.20")
        self.files["repository/SHA256SUMS"] = ("%s  openg2p-livestock-registry-1.0.0-dev.21.tgz\n" % hashlib.sha256(
            self.files["repository/openg2p-livestock-registry-1.0.0-dev.21.tgz"]).hexdigest()).encode()
        with self.assertRaisesRegex(ValueError, "expected 1.0.0-dev.21"):
            self.bump.new_pin(self.COMMIT, fetch=self.fetch)

    def test_branch_or_short_commit_is_rejected(self):
        for bad in ("main", "abc1234", "B" * 40):
            with self.assertRaises(ValueError):
                self.bump.new_pin(bad, fetch=self.fetch)

    def test_placeholder_version_is_rejected(self):
        with self.assertRaises(ValueError):
            self.bump.new_pin(self.COMMIT, version="0.0.0-develop.1", fetch=self.fetch)


class ProposePinTests(unittest.TestCase):
    """--latest: what the scheduled workflow uses to decide whether to open a pin pull request."""
    HEAD = "c" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bump = load_bump()
        self.root = Path(self.tmp.name)
        (self.root / "ci/deploy").mkdir(parents=True)
        shutil.copy(ROOT / "ci/deploy/oan-chart.json", self.root / "ci/deploy/oan-chart.json")
        self.bump.PROFILE = self.root / "ci/deploy/oan-chart.json"
        self.bump.ROOT = self.root
        self.old = json.loads(self.bump.PROFILE.read_text(encoding="utf-8"))
        self.pinned = self.old["registry"]
        self.files = {}

    def publish(self, version, app, values=b"registry: {}\n"):
        archive = fake_archive(version=version, app=app)
        name = "openg2p-livestock-registry-%s.tgz" % version
        self.files = {
            "releases/current.yaml": ("charts:\n  openg2p-livestock-registry: %s\n" % version).encode(),
            "repository/" + name: archive,
            "repository/SHA256SUMS": ("%s  %s\n" % (hashlib.sha256(archive).hexdigest(), name)).encode(),
            "environments/dev/values/livestock.yaml": values,
        }
        return hashlib.sha256(archive).hexdigest()

    def fetch(self, uri):
        return self.files[uri.split("/oan-helm/" + self.HEAD + "/")[1]]

    def propose(self):
        return self.bump.proposal(self.old, fetch=self.fetch, head=lambda: self.HEAD)

    def test_version_order(self):
        key = self.bump.version_key
        self.assertLess(key("1.0.0-dev.9"), key("1.0.0-dev.10"))     # numeric, not text order
        self.assertLess(key("1.0.0-dev.20"), key("1.0.0-dev.21"))
        self.assertLess(key("1.0.0-dev.99"), key("1.0.0"))
        with self.assertRaises(ValueError):
            key("latest")

    def test_newer_chart_is_proposed(self):
        self.publish("1.0.0-dev.21", self.pinned["platformVersion"])
        new, why = self.propose()
        self.assertIsNotNone(new)
        self.assertIn("newer chart 1.0.0-dev.21", why)
        self.assertTrue(new["registry"]["repository"].endswith("/oan-helm/" + self.HEAD + "/repository/"))

    def test_same_chart_and_values_is_up_to_date(self):
        digest = self.publish(self.pinned["version"], self.pinned["platformVersion"])
        self.pinned["sha256"] = digest                      # as if the archive were the pinned one
        self.pinned["values"]["sha256"] = hashlib.sha256(self.files["environments/dev/values/livestock.yaml"]).hexdigest()
        new, why = self.propose()
        self.assertIsNone(new)
        self.assertIn("up to date", why)

    def test_values_only_change_is_proposed_with_the_same_chart(self):
        digest = self.publish(self.pinned["version"], self.pinned["platformVersion"], values=b"registry: {changed: true}\n")
        self.pinned["sha256"] = digest
        new, why = self.propose()
        self.assertIsNotNone(new)
        self.assertIn("public values changed", why)
        self.assertEqual(new["registry"]["version"], self.pinned["version"])

    def test_older_chart_is_never_proposed(self):
        self.publish("1.0.0-dev.18", "1.2.2-rc.600")
        with self.assertRaisesRegex(ValueError, "downgrade"):
            self.propose()

    def test_changed_archive_for_the_same_version_is_refused(self):
        self.publish(self.pinned["version"], "1.2.2-rc.999")  # different bytes, same version
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.propose()

    def test_head_commit_is_read_with_git_ls_remote(self):
        out = "%s\trefs/heads/main\n" % self.HEAD
        self.assertEqual(self.bump.head_commit(run=lambda cmd: out), self.HEAD)
        with self.assertRaises(ValueError):
            self.bump.head_commit(run=lambda cmd: "")

    def test_only_the_pin_is_rewritten_even_for_a_new_platform(self):
        self.publish("1.0.0-dev.21", "1.2.2-rc.700")
        new, _ = self.propose()
        self.assertEqual(new["registry"]["platformVersion"], "1.2.2-rc.700")
        self.assertEqual(self.bump.update_companions(self.pinned, new["registry"], root=self.root), [])
        self.assertEqual(self.bump.apply_pin(self.old, new, self.root), ["ci/deploy/oan-chart.json"])
        written = json.loads((self.root / "ci/deploy/oan-chart.json").read_text())
        self.assertEqual(written["registry"]["version"], "1.0.0-dev.21")
        self.assertEqual(written["dashboard"], self.old["dashboard"])      # the dashboard pin is never touched


def load_propose():
    spec = importlib.util.spec_from_file_location("propose_under_test", ROOT / "ci/deploy/propose_pin_pr.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProposePullRequestTests(unittest.TestCase):
    """The last Jenkins stage: opens the pin pull request, with a stand-in git and GitHub."""
    TOKEN = "tok-SECRET-123"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "ci/deploy").mkdir(parents=True)
        shutil.copy(ROOT / "ci/deploy/oan-chart.json", self.root / "ci/deploy/oan-chart.json")
        self.mod = load_propose()
        self.bump = load_bump()
        self.bump.PROFILE = self.root / "ci/deploy/oan-chart.json"
        self.bump.ROOT = self.root
        old = json.loads(self.bump.PROFILE.read_text(encoding="utf-8"))
        new = json.loads(json.dumps(old))
        pin = new["registry"]
        pin["version"] = "1.0.0-dev.21"
        pin["sha256"] = "a" * 64
        pin["values"]["sha256"] = "b" * 64
        pin["repository"] = "https://raw.githubusercontent.com/Centre-for-Open-Societal-Systems/oan-helm/" + "e" * 40 + "/repository/"
        pin["values"]["url"] = pin["repository"][:-len("repository/")] + pin["values"]["path"]
        self.new = new
        self.proposal_result = (new, "newer chart 1.0.0-dev.21 released (pinned 1.0.0-dev.19)")
        self.bump.proposal = lambda old_profile: self.proposal_result
        self.cmds = []
        self.calls = []
        self.exists = ""          # what `git ls-remote` prints
        self.pulls = (200, [])    # answer to the pull request list
        self.create = (201, {"html_url": "https://github.com/o/r/pull/7"})
        self.fail_on = None       # a label whose command should fail

    def runner(self, cmd, label=None, cwd=None, env=None, check=True):
        self.cmds.append((label or cmd[0], cmd))
        if label == self.fail_on:
            raise RuntimeError("%s failed (exit 1):\nboom" % label)
        if label == "git remote get-url":
            return "https://github.com/o/r.git\n"
        if label == "git rev-parse":
            return "f" * 40 + "\n"
        if label == "git ls-remote":
            return self.exists
        return ""

    def call(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        return self.pulls if method == "GET" else self.create

    def go(self, token=None):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            code = self.mod.propose(self.TOKEN if token is None else token, bump=self.bump, runner=self.runner,
                                    call=self.call, root=self.root)
        return code, out.getvalue()

    def labels(self):
        return [label for label, _ in self.cmds]

    def test_nothing_to_do_touches_neither_git_nor_github(self):
        self.proposal_result = (None, "up to date: chart 1.0.0-dev.19 and its values are unchanged on oan-helm main")
        code, out = self.go()
        self.assertEqual((code, self.cmds, self.calls), (0, [], []))
        self.assertIn("up to date", out)

    def test_a_needed_update_without_a_credential_proposes_nothing(self):
        code, out = self.go(token="")
        self.assertEqual(code, 3)
        self.assertEqual((self.cmds, self.calls), ([], []))
        self.assertIn("no GitHub credential", out)

    def test_a_new_platform_version_is_reported_and_not_proposed(self):
        self.new["registry"]["platformVersion"] = "1.2.2-rc.700"
        code, out = self.go()
        self.assertEqual(code, 5)
        self.assertEqual((self.cmds, self.calls), ([], []))
        self.assertIn("scripts/bump-rp-version.sh", out)

    def test_full_proposal_flow(self):
        code, out = self.go()
        self.assertEqual(code, 0, out)
        labels = self.labels()
        order = ["git rev-parse", "the pin tests", "verified prefetch of the new pin", "git checkout -b", "git add",
                 "git commit", "git push", "git checkout", "git branch -D"]
        positions = [labels.index(name) for name in order]
        self.assertEqual(positions, sorted(positions))
        # the pin and the files that must agree with it were rewritten on disk
        self.assertIn("1.0.0-dev.21", (self.root / "ci/deploy/oan-chart.json").read_text())
        add = next(cmd for label, cmd in self.cmds if label == "git add")
        self.assertEqual(add[-1], "ci/deploy/oan-chart.json")
        self.assertEqual(add[-2:], ["--", "ci/deploy/oan-chart.json"])
        # the pull request goes to develop from a branch named for the version and values
        method, path, payload = self.calls[-1]
        self.assertEqual((method, path), ("POST", "/repos/o/r/pulls"))
        self.assertEqual(payload["base"], "develop")
        self.assertEqual(payload["head"], "bot/oan-chart-pin-1.0.0-dev.21-" + "b" * 8)
        self.assertIn("Merging this deploys to New Dev", payload["body"])
        self.assertIn("opened https://github.com/o/r/pull/7", out)
        # the workspace is restored to the commit the build checked out
        restore = next(cmd for label, cmd in self.cmds if label == "git checkout")
        self.assertEqual(restore[-1], "f" * 40)

    def test_the_token_is_never_printed_or_used_as_a_label(self):
        code, out = self.go()
        self.assertNotIn(self.TOKEN, out)
        self.assertNotIn(self.TOKEN, " ".join(self.labels()))
        push = next(cmd for label, cmd in self.cmds if label == "git push")
        self.assertNotIn(self.TOKEN, " ".join(push))                  # sent only as a base64 basic-auth header
        self.assertTrue(any(a.startswith("http.extraheader=AUTHORIZATION: basic ") for a in push))

    def test_an_existing_pull_request_is_not_proposed_again(self):
        self.pulls = (200, [{"html_url": "https://github.com/o/r/pull/3", "state": "closed"}])
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertIn("Already proposed", out)
        self.assertNotIn("git push", self.labels())
        self.assertEqual([c[0] for c in self.calls], ["GET"])

    def test_an_existing_branch_without_a_pull_request_only_opens_the_pull_request(self):
        self.exists = "abc\trefs/heads/bot/x\n"
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertNotIn("git push", self.labels())
        self.assertEqual(self.calls[-1][0], "POST")

    def test_failed_verification_pushes_nothing_and_restores_the_workspace(self):
        self.fail_on = "the pin tests"
        with self.assertRaisesRegex(RuntimeError, "the pin tests failed"):
            self.go()
        self.assertNotIn("git push", self.labels())
        self.assertIn("git checkout", self.labels())
        self.assertEqual([c[0] for c in self.calls], ["GET"])

    def test_github_refusing_to_list_pull_requests_is_reported(self):
        self.pulls = (403, {"message": "Resource not accessible by integration"})
        code, out = self.go()
        self.assertEqual(code, 4)
        self.assertIn("Pull requests write", out)
        self.assertNotIn("git push", self.labels())

    def test_github_refusing_to_create_the_pull_request_is_reported(self):
        self.create = (403, {"message": "Resource not accessible by integration"})
        code, out = self.go()
        self.assertEqual(code, 4)
        self.assertIn("is pushed; open the pull request", out)

    def test_repository_slug(self):
        slug = self.mod.slug_of
        self.assertEqual(slug("https://github.com/Centre-for-Open-Societal-Systems/livestock-registry.git\n"),
                         "Centre-for-Open-Societal-Systems/livestock-registry")
        self.assertEqual(slug("git@github.com:o/r.git"), "o/r")
        with self.assertRaises(ValueError):
            slug("https://gitlab.com/o/r.git")



# ---- the images go into the oan-helm chart ----------------------------------------------------------------------

def load_release():
    spec = importlib.util.spec_from_file_location("release_images_under_test", ROOT / "ci/deploy/release_images.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REG = "123456789012.dkr.ecr.ap-south-1.amazonaws.com"
PATH_ = "openg2p/livestock-registry"
OLD_TAG = "bf78e69a5fd6"


def sample_values(tag=OLD_TAG, digests=None):
    """The shape of charts/openg2p-livestock-registry/values.yaml in oan-helm, reduced."""
    digests = digests or {}

    def img(component, section_digest):
        return ("    image:\n      repository: %s/%s/%s\n      tag: %s@sha256:%s\n" % (REG, PATH_, component, tag, section_digest))
    old = "e" * 64
    return (
        "global:\n  baseDomain: development.oanstaging.com\n"
        "registry:\n"
        "  keycloak-init:\n    image:\n      repository: docker.io/x/y\n      tag: 1.0.0\n"
        "  staffApi:\n" + img("staff-api", old) + "    envVars:\n      A: 'b'\n"
        "# a comment at column 0 inside the registry block\n"
        "  partnerApi:\n" + img("partner-api", old) +
        "  celeryWorker:\n" + img("celery", old) +
        "  celeryBeat:\n" + img("celery", old) +
        "  dbSeed:\n" + img("db-seed", old) + "    loadGeoData: false\n"
        "  staffUi:\n" + img("staff-ui", old) +
        "  sanity:\n    enabled: true\n" + img("sanity-tests", old) + "    regType: Livestock\n"
        "reporting:\n  image:\n    repository: ''\n    tag: ''\n"
        "dashboardApi:\n  enabled: false\n  image:\n    digest: sha256:%s\n    repository: %s/%s/dashboard-api\n    tag: %s\n  replicas: 1\n"
        "dashboardUi:\n  image:\n    repository: ''\n    tag: ''\n" % (old, REG, PATH_, tag))


NEW_TAG = "0123456789ab"


def release_digests():
    return dict((c, "sha256:" + ("%x" % (i + 1)) * 64) for i, c in enumerate(
        ["staff-api", "partner-api", "celery", "db-seed", "sanity-tests", "staff-ui", "dashboard-api"]))


class ImageValuesTests(unittest.TestCase):
    def setUp(self):
        self.r = load_release()
        self.tags, self.dash = self.r.wanted(NEW_TAG, release_digests())
        self.repos = self.r.expected_repositories(REG, PATH_)

    def test_only_the_image_lines_change(self):
        old = sample_values()
        new = self.r.rewrite_values(old, self.tags, self.dash, self.repos)
        changed = [(a, b) for a, b in zip(old.split("\n"), new.split("\n")) if a != b]
        self.assertEqual(len(old.split("\n")), len(new.split("\n")))
        self.assertEqual(len(changed), 9)                          # seven registry tags + dashboard digest and tag
        self.assertTrue(all(b.lstrip().startswith(("tag:", "digest:")) for _, b in changed))
        self.assertIn("      tag: %s@%s" % (NEW_TAG, release_digests()["staff-api"]), new)
        self.assertIn("    tag: '%s'\n" % NEW_TAG, new)
        self.assertIn("    digest: %s\n" % release_digests()["dashboard-api"], new)
        # untouched: the comment, other images, the empty reporting and dashboardUi images
        for kept in ("# a comment at column 0 inside the registry block", "tag: 1.0.0", "    tag: ''", "      A: 'b'"):
            self.assertIn(kept, new)

    def test_an_all_digit_commit_tag_stays_a_string_in_the_values(self):
        # Build 875427410733 made --set render image:%!s(int64=...); the same trap exists in the values file.
        yaml = load_chart_module().yaml_module()
        for tag in ("875427410733", "123456e78901", "000000000001"):
            tags, dash = self.r.wanted(tag, release_digests())
            new = self.r.rewrite_values(sample_values(), tags, dash, self.repos)
            document = yaml.safe_load(new)
            self.assertEqual(document["dashboardApi"]["image"]["tag"], tag)
            self.assertIsInstance(document["dashboardApi"]["image"]["tag"], str)
            self.assertIsInstance(document["registry"]["staffApi"]["image"]["tag"], str)
            self.assertTrue(self.r.carries(new, tags, dash))

    def test_both_celery_workloads_get_the_celery_digest(self):
        new = self.r.rewrite_values(sample_values(), self.tags, self.dash, self.repos)
        self.assertEqual(new.count("%s@%s" % (NEW_TAG, release_digests()["celery"])), 2)

    def test_rewriting_twice_changes_nothing_and_carries_says_so(self):
        once = self.r.rewrite_values(sample_values(), self.tags, self.dash, self.repos)
        self.assertEqual(self.r.rewrite_values(once, self.tags, self.dash, self.repos), once)
        self.assertTrue(self.r.carries(once, self.tags, self.dash))
        self.assertFalse(self.r.carries(sample_values(), self.tags, self.dash))

    def test_another_ecr_repository_is_refused(self):
        bad = sample_values().replace("/staff-api", "/someone-elses-api")
        with self.assertRaisesRegex(ValueError, "staffApi image repository"):
            self.r.rewrite_values(bad, self.tags, self.dash, self.repos)

    def test_a_missing_section_is_an_error_not_a_silent_skip(self):
        with self.assertRaises(ValueError):
            self.r.rewrite_values(sample_values().replace("  dbSeed:", "  dbSeedX:"), self.tags, self.dash, self.repos)

    def test_digests_are_read_and_validated(self):
        text = "".join("%s\t%s\n" % kv for kv in release_digests().items())
        self.assertEqual(self.r.read_digests(text), release_digests())
        for bad in ("staff-api\tlatest\n", "staff-api sha256:" + "a" * 64 + "\n"):
            with self.assertRaises(ValueError):
                self.r.read_digests(bad)

    def test_every_component_needs_a_digest_and_the_tag_a_commit(self):
        partial = dict(release_digests())
        del partial["sanity-tests"]
        with self.assertRaisesRegex(ValueError, "sanity-tests"):
            self.r.wanted(NEW_TAG, partial)
        with self.assertRaises(ValueError):
            self.r.wanted("latest", release_digests())

    def test_the_archive_values_are_read_for_the_release_check(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            data = sample_values().encode()
            info = tarfile.TarInfo("openg2p-livestock-registry/values.yaml")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        self.assertEqual(self.r.archive_values(buf.getvalue()), sample_values())


class StripImagesTests(unittest.TestCase):
    def test_saved_release_values_lose_only_their_image_settings(self):
        chart = load_chart_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "values.yaml"
            path.write_text("global:\n  baseDomain: a.b\nregistry:\n  staffApi:\n    image: {tag: old, repository: r}\n    hostname: h\n"
                            "  sanity:\n    image: {tag: old}\n  keycloak-init:\n    image: {tag: keep}\n"
                            "dashboardApi:\n  enabled: true\n  image: {tag: old, digest: d}\n")
            self.assertEqual(chart.strip_images(path), 3)
            text = path.read_text()
            self.assertNotIn("old", text)
            for kept in ("baseDomain: a.b", "hostname: h", "keep", "enabled: true"):
                self.assertIn(kept, text)

    def test_the_candidate_pin_replaces_the_committed_pin_through_the_environment(self):
        chart = load_chart_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pin.json"
            path.write_text(json.dumps({"registry": {"version": "9.9.9-dev.1"}}))
            old = os.environ.get("OAN_CHART_PIN_FILE")
            os.environ["OAN_CHART_PIN_FILE"] = str(path)
            try:
                self.assertEqual(chart.profiles()["registry"]["version"], "9.9.9-dev.1")
            finally:
                if old is None:
                    del os.environ["OAN_CHART_PIN_FILE"]
                else:
                    os.environ["OAN_CHART_PIN_FILE"] = old
        self.assertEqual(chart.profiles()["registry"]["version"], json.loads((ROOT / "ci/deploy/oan-chart.json").read_text())["registry"]["version"])


def load_chart_module():
    spec = importlib.util.spec_from_file_location("oan_chart_under_test", ROOT / "ci/deploy/oan_chart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGitHub(object):
    """oan-helm as the release stage sees it: main, a branch and a pull request, checks, merge, and the release."""

    def __init__(self, release):
        self.r = release
        self.main_sha = "a" * 40
        self.text = sample_values()                 # values.yaml on main
        self.branches = {}                          # name -> text
        self.calls = []
        self.check_script = [[{"name": "validate", "status": "completed", "conclusion": "success"}]]
        self.merge_status = 200
        self.checks_polled = 0
        self.refuse = {}                            # (method, path-prefix) -> status

    def __call__(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        slug = "/repos/%s" % self.r.SLUG
        assert path.startswith(slug), path
        rest = path[len(slug):]
        for (m, prefix), status in self.refuse.items():
            if method == m and rest.startswith(prefix):
                return status, {"message": "refused"}
        if method == "GET" and rest == "/git/ref/heads/main":
            return 200, {"object": {"sha": self.main_sha}}
        if method == "GET" and rest.startswith("/git/ref/heads/bot/"):
            return 200, {"object": {"sha": "b" * 40}}
        if method == "GET" and rest.startswith("/contents/"):
            ref = rest.split("?ref=")[1]
            text = self.text if ref == "main" else self.branches[ref]
            return 200, {"content": base64.b64encode(text.encode()).decode(), "sha": "blob1"}
        if method == "POST" and rest == "/git/refs":
            self.branches[payload["ref"][len("refs/heads/"):]] = self.text
            return 201, {}
        if method == "PUT" and rest.startswith("/contents/"):
            self.branches[payload["branch"]] = base64.b64decode(payload["content"]).decode()
            return 200, {}
        if method == "GET" and rest.startswith("/pulls?"):
            return 200, []
        if method == "POST" and rest == "/pulls":
            return 201, {"number": 11, "html_url": "https://github.com/o/oan-helm/pull/11"}
        if method == "GET" and "/check-runs" in rest:
            runs = self.check_script[min(self.checks_polled, len(self.check_script) - 1)]
            self.checks_polled += 1
            return 200, {"check_runs": runs}
        if method == "PUT" and rest.endswith("/merge"):
            if self.merge_status == 200:
                branch = [b for b in self.branches][0]
                self.text = self.branches[branch]
            return self.merge_status, {"message": "Pull request is not mergeable"}
        if method == "DELETE":
            return 204, None
        raise AssertionError("unexpected call %s %s" % (method, rest))


def tgz(values_text, version, app="1.2.2-rc.614"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, body in (("Chart.yaml", "apiVersion: v2\nname: openg2p-livestock-registry\nversion: %s\nappVersion: %s\n" % (version, app)),
                           ("values.yaml", values_text)):
            data = body.encode()
            info = tarfile.TarInfo("openg2p-livestock-registry/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class ReleaseStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "ci/deploy").mkdir(parents=True)
        (self.root / ".build").mkdir()
        shutil.copy(ROOT / "ci/deploy/oan-chart.json", self.root / "ci/deploy/oan-chart.json")
        (self.root / ".build/image-digests.tsv").write_text("".join("%s\t%s\n" % kv for kv in release_digests().items()))
        self.r = load_release()
        self.bump = load_bump()
        self.bump.PROFILE = self.root / "ci/deploy/oan-chart.json"
        self.bump.ROOT = self.root
        self.gh = FakeGitHub(self.r)
        self.out = []
        self.now = [0.0]
        self.version = "1.0.0-dev.20"
        self.release_after = 2                       # polls of main before the release commit exists
        self.polls = 0
        self.env = {"IMAGE_TAG": NEW_TAG, "ECR_REGISTRY": REG, "ECR_PATH": PATH_, "COMMIT_SUBJECT": "Add a field",
                    "SOURCE_COMMIT": NEW_TAG + "f" * 28, "BUILD_URL": "https://jenkins/job/7/"}
        self.tags, self.dash = self.r.wanted(NEW_TAG, release_digests())

    def sleep(self, seconds):
        self.now[0] += seconds

    def fetch(self, uri):
        path = uri.split("/oan-helm/")[1].split("/", 1)[1]
        if path == "releases/current.yaml":
            self.polls += 1                              # one round of waiting for the release per look at current.yaml
        # until `release_after` rounds have passed, main still has the chart without this build's images
        released = self.gh.text if self.polls > self.release_after else sample_values()
        name = "openg2p-livestock-registry-%s.tgz" % self.version
        archive = tgz(released, self.version)
        files = {"releases/current.yaml": ("charts:\n  openg2p-livestock-registry: %s\n" % self.version).encode(),
                 "repository/" + name: archive,
                 "repository/SHA256SUMS": ("%s  %s\n" % (hashlib.sha256(archive).hexdigest(), name)).encode(),
                 "environments/dev/values/livestock.yaml": b"registry: {}\n"}
        return files[path]

    def release(self):
        release = self.r.Release("tok", call=self.gh, fetch=self.fetch, sleep=self.sleep, clock=lambda: self.now[0],
                                 bump=self.bump, out=self.out.append)
        self.release_obj = release
        return release

    def run_stage(self, **overrides):
        env = dict(self.env, **overrides)
        return self.release().run(env, self.root, (self.root / ".build/image-digests.tsv").read_text())

    def methods(self):
        return [(m, p.split("?")[0].replace("/repos/%s" % self.r.SLUG, "")) for m, p, _ in self.gh.calls]

    def test_full_flow_opens_the_pull_request_waits_merges_and_returns_the_release_pin(self):
        profiles = self.run_stage()
        pin = profiles["registry"]
        self.assertEqual(pin["version"], "1.0.0-dev.20")
        self.assertEqual(pin["repository"].split("/oan-helm/")[1].split("/")[0], self.gh.main_sha)
        calls = self.methods()
        order = [("POST", "/git/refs"), ("PUT", "/contents/charts/openg2p-livestock-registry/values.yaml"),
                 ("POST", "/pulls"), ("GET", "/commits/%s/check-runs" % ("b" * 40)), ("PUT", "/pulls/11/merge"),
                 ("DELETE", "/git/refs/heads/bot/livestock-images-" + NEW_TAG)]
        positions = [calls.index(c) for c in order]
        self.assertEqual(positions, sorted(positions))
        branch_text = self.gh.branches["bot/livestock-images-" + NEW_TAG]
        self.assertIn("%s@%s" % (NEW_TAG, release_digests()["staff-api"]), branch_text)
        pr = next(p for m, p, body in self.gh.calls if m == "POST" and p.endswith("/pulls"))
        body = next(b for m, p, b in self.gh.calls if m == "POST" and p.endswith("/pulls"))
        self.assertEqual((body["base"], body["head"]), ("main", "bot/livestock-images-" + NEW_TAG))
        self.assertIn("Livestock registry images %s: Add a field" % NEW_TAG, body["title"])
        merge = next(b for m, p, b in self.gh.calls if m == "PUT" and p.endswith("/merge"))
        self.assertEqual(merge["merge_method"], "squash")
        self.assertEqual(merge["sha"], "b" * 40)

    def test_a_rerun_of_an_already_released_commit_does_nothing(self):
        self.gh.text = self.r.rewrite_values(sample_values(), self.tags, self.dash)
        self.release_after = 0
        profiles = self.run_stage()
        self.assertIsNone(profiles)
        calls = self.methods()
        self.assertNotIn(("POST", "/pulls"), calls)
        self.assertNotIn(("POST", "/git/refs"), calls)
        self.assertIn("already released", "\n".join(self.out))
        self.assertIn("stays on the last working release", self.release_obj.note)

    def test_a_rerun_with_new_digests_for_the_same_commit_is_still_skipped(self):
        other = dict(release_digests(), **{"staff-api": "sha256:" + "9" * 64})
        self.gh.text = self.r.rewrite_values(sample_values(), *self.r.wanted(NEW_TAG, other))
        self.assertIsNone(self.run_stage())
        self.assertNotIn(("POST", "/pulls"), self.methods())

    def test_released_before_looks_at_the_tag_lines_only(self):
        released = self.r.rewrite_values(sample_values(), self.tags, self.dash)
        self.assertTrue(self.r.released_before(released, NEW_TAG))
        self.assertFalse(self.r.released_before(sample_values(), NEW_TAG))
        self.assertFalse(self.r.released_before(released, "0" * 12))

    def test_a_failed_check_stops_before_the_merge(self):
        self.gh.check_script = [[{"name": "validate", "status": "completed", "conclusion": "failure"}]]
        with self.assertRaises(self.r.Stop) as caught:
            self.run_stage()
        self.assertEqual(caught.exception.code, 6)
        self.assertNotIn(("PUT", "/pulls/11/merge"), self.methods())

    def test_checks_are_waited_for_until_they_finish(self):
        running = [{"name": "validate", "status": "in_progress", "conclusion": None}]
        done = [{"name": "validate", "status": "completed", "conclusion": "success"}]
        self.gh.check_script = [running, running, done]
        self.run_stage()
        self.assertEqual(self.gh.checks_polled, 3)

    def test_a_merge_the_app_may_not_make_needs_a_person(self):
        self.gh.merge_status = 405
        with self.assertRaises(self.r.Stop) as caught:
            self.run_stage()
        self.assertEqual(caught.exception.code, 7)
        self.assertIn("A person must merge", str(caught.exception))

    def test_a_credential_without_access_is_reported_as_such(self):
        self.gh.refuse[("POST", "/git/refs")] = 403
        with self.assertRaises(self.r.Stop) as caught:
            self.run_stage()
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("Contents and Pull requests write", str(caught.exception))

    def test_a_release_that_never_appears_times_out_without_a_candidate(self):
        self.release_after = 10 ** 6
        with self.assertRaises(self.r.Stop) as caught:
            self.run_stage()
        self.assertEqual(caught.exception.code, 8)

    def test_a_chart_older_than_the_pin_is_refused(self):
        self.version = "1.0.0-dev.18"
        self.release_after = 0
        with self.assertRaises(self.r.Stop) as caught:
            self.run_stage()
        self.assertIn("older than the pinned", str(caught.exception))

    def test_dry_run_changes_nothing(self):
        self.assertIsNone(self.run_stage(DRY_RUN="1"))
        self.assertEqual([m for m, _ in self.methods() if m != "GET"], [])

    def test_main_writes_the_candidate_pin_and_nothing_else_in_the_repository(self):
        pin = self.bump.new_pin
        out = io.StringIO()
        import contextlib
        fake = {"registry": json.loads((self.root / "ci/deploy/oan-chart.json").read_text())["registry"]}
        fake["registry"]["version"] = "1.0.0-dev.20"
        original = self.r.Release.run
        self.r.Release.run = lambda me, env, root, digests: fake
        original_pin_only = self.r.pin_only_commit
        self.r.pin_only_commit = lambda root, runner=None: False
        try:
            with contextlib.redirect_stdout(out):
                code = self.r.main(env=dict(self.env, GH_TOKEN="tok"), root=self.root)
        finally:
            self.r.Release.run = original
            self.r.pin_only_commit = original_pin_only
        self.assertEqual(code, 0, out.getvalue())
        written = json.loads((self.root / ".build/oan-chart-candidate.json").read_text())
        self.assertEqual(written["registry"]["version"], "1.0.0-dev.20")

    def test_main_without_a_token_is_exit_3_and_writes_no_candidate(self):
        out = io.StringIO()
        import contextlib
        original = self.r.pin_only_commit
        self.r.pin_only_commit = lambda root, runner=None: False
        try:
            with contextlib.redirect_stdout(out):
                code = self.r.main(env=dict(self.env), root=self.root)
        finally:
            self.r.pin_only_commit = original
        self.assertEqual(code, 3)
        self.assertFalse((self.root / ".build/oan-chart-candidate.json").exists())

    def test_main_leaves_a_note_and_no_candidate_for_a_commit_that_was_already_released(self):
        import contextlib
        self.gh.text = self.r.rewrite_values(sample_values(), self.tags, self.dash)
        original = self.r.pin_only_commit
        self.r.pin_only_commit = lambda root, runner=None: False
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = self.r.main(env=dict(self.env, GH_TOKEN="tok"), root=self.root, call=self.gh)
        finally:
            self.r.pin_only_commit = original
        self.assertEqual(code, 0, out.getvalue())
        self.assertFalse((self.root / ".build/oan-chart-candidate.json").exists())
        note = (self.root / ".build/release-note.txt").read_text()
        self.assertIn("stays on the last working release", note)
        self.assertNotIn("POST", [m for m, _ in self.methods()])

    def test_main_leaves_a_note_for_a_pin_only_commit(self):
        import contextlib
        original = self.r.pin_only_commit
        self.r.pin_only_commit = lambda root, runner=None: True
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = self.r.main(env=dict(self.env), root=self.root)
        finally:
            self.r.pin_only_commit = original
        self.assertEqual(code, 0)
        self.assertIn("only changes the chart pin", (self.root / ".build/release-note.txt").read_text())

    def test_a_commit_that_only_moves_the_pin_is_not_released_again(self):
        class Done(object):
            returncode = 0
            stdout = "ci/deploy/oan-chart.json\n"
        self.assertTrue(self.r.pin_only_commit(self.root, runner=lambda cmd: Done()))
        Done.stdout = "ci/deploy/oan-chart.json\ndocker/staff-api/Dockerfile\n"
        self.assertFalse(self.r.pin_only_commit(self.root, runner=lambda cmd: Done()))
        Done.stdout = ""
        self.assertFalse(self.r.pin_only_commit(self.root, runner=lambda cmd: Done()))
        Done.returncode = 128
        self.assertFalse(self.r.pin_only_commit(self.root, runner=lambda cmd: Done()))


class CandidatePinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bump = load_bump()
        self.old = json.loads((ROOT / "ci/deploy/oan-chart.json").read_text())
        self.path = Path(self.tmp.name) / "candidate.json"

    def candidate(self, **changes):
        new = json.loads(json.dumps(self.old))
        new["registry"].update(changes)
        self.path.write_text(json.dumps(new))
        return new

    def test_a_newer_deployed_chart_replaces_the_pin(self):
        new = self.candidate(version="1.0.0-dev.20", sha256="a" * 64)
        got, why = self.bump.from_candidate(self.old, self.path)
        self.assertEqual(got, new)
        self.assertIn("released from this build's images", why)

    def test_the_pinned_chart_again_is_up_to_date(self):
        self.candidate()
        got, why = self.bump.from_candidate(self.old, self.path)
        self.assertIsNone(got)
        self.assertIn("up to date", why)

    def test_an_older_chart_or_a_changed_archive_is_refused(self):
        self.candidate(version="1.0.0-dev.18", sha256="a" * 64)
        with self.assertRaisesRegex(ValueError, "downgrade"):
            self.bump.from_candidate(self.old, self.path)
        self.candidate(sha256="a" * 64)
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.bump.from_candidate(self.old, self.path)


class RollingPinPullRequestTests(unittest.TestCase):
    """With the build's own chart release the pin pull request is one rolling branch, updated by every build."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "ci/deploy").mkdir(parents=True)
        shutil.copy(ROOT / "ci/deploy/oan-chart.json", self.root / "ci/deploy/oan-chart.json")
        self.mod = load_propose()
        self.bump = load_bump()
        self.bump.PROFILE = self.root / "ci/deploy/oan-chart.json"
        self.bump.ROOT = self.root
        new = json.loads(self.bump.PROFILE.read_text())
        new["registry"].update(version="1.0.0-dev.20", sha256="a" * 64)
        new["registry"]["repository"] = "https://raw.githubusercontent.com/Centre-for-Open-Societal-Systems/oan-helm/" + "e" * 40 + "/repository/"
        self.new = new
        self.candidate = self.root / "candidate.json"
        self.candidate.write_text(json.dumps(new))
        self.bump.from_candidate = lambda old, path: (self.new, "chart 1.0.0-dev.20, released from this build's images and deployed, replaces the pinned 1.0.0-dev.19")
        self.cmds, self.calls = [], []
        self.pulls = (200, [])

    def runner(self, cmd, label=None, cwd=None, env=None, check=True):
        self.cmds.append((label or cmd[0], cmd, env))
        if label == "git remote get-url":
            return "https://github.com/o/r.git\n"
        if label == "git rev-parse":
            return "f" * 40 + "\n"
        return ""

    def call(self, method, path, token, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            return self.pulls
        if method == "PATCH":
            return 200, {}
        return 201, {"html_url": "https://github.com/o/r/pull/9"}

    def go(self):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            code = self.mod.propose("tok", bump=self.bump, runner=self.runner, call=self.call, root=self.root,
                                    candidate=str(self.candidate))
        return code, out.getvalue()

    def test_first_build_opens_the_rolling_pull_request(self):
        code, out = self.go()
        self.assertEqual(code, 0, out)
        push = next(c for l, c, e in self.cmds if l == "git push")
        self.assertIn("--force", push)
        self.assertEqual(push[-1], "HEAD:refs/heads/bot/oan-chart-pin")
        self.assertIn("state=open", self.calls[0][1])
        method, path, payload = self.calls[-1]
        self.assertEqual((method, payload["head"], payload["base"]), ("POST", "bot/oan-chart-pin", "develop"))
        self.assertIn("deployed to New Dev successfully", payload["body"])
        self.assertIn("One rolling pull request", payload["body"])

    def test_the_next_build_updates_the_open_pull_request_instead_of_adding_one(self):
        self.pulls = (200, [{"number": 9, "html_url": "https://github.com/o/r/pull/9", "state": "open"}])
        code, out = self.go()
        self.assertEqual(code, 0, out)
        self.assertEqual([c[0] for c in self.calls], ["GET", "PATCH"])
        self.assertEqual(self.calls[-1][1], "/repos/o/r/pulls/9")
        self.assertIn("git push", [l for l, c, e in self.cmds])

    def test_the_pin_checks_do_not_see_the_candidate_variable(self):
        os.environ["OAN_CHART_PIN_FILE"] = str(self.candidate)
        try:
            self.go()
        finally:
            del os.environ["OAN_CHART_PIN_FILE"]
        env = next(e for l, c, e in self.cmds if l == "the pin tests")
        self.assertNotIn("OAN_CHART_PIN_FILE", env)


class PipelineWiringTests(unittest.TestCase):
    """ci/pipelines/dev.groovy: the stages that make the chart release and use it, in order."""

    def setUp(self):
        self.text = (ROOT / "ci/pipelines/dev.groovy").read_text()

    def stage(self, name, following):
        return self.text.split("stage('%s')" % name, 1)[1].split("stage('%s')" % following, 1)[0]

    def test_stages_run_in_the_order_images_release_deploy_pin(self):
        order = [self.text.index("stage('%s')" % n) for n in
                 ("Build & Push Images", "Release images to oan-helm", "Deploy to Development", "Propose chart pin update")]
        self.assertEqual(order, sorted(order))

    def test_digests_are_captured_after_the_pushes_and_stashed(self):
        build = self.stage("Build & Push Images", "Stash deploy")
        self.assertIn("aws ecr describe-images", build)
        self.assertIn("stash name: 'image-digests'", build)
        self.assertLess(build.index("docker push ${api}"), build.index("describe-images"))

    def test_the_release_stage_cannot_fail_the_build_and_hands_over_the_candidate(self):
        stage = self.stage("Release images to oan-helm", "Prepare Deploy Values")
        self.assertIn("catchError(buildResult: 'UNSTABLE', stageResult: 'UNSTABLE')", stage)
        self.assertIn("python3 ci/deploy/release_images.py", stage)
        self.assertIn("agent { label 'vpn-agent2' }", stage)
        self.assertIn("branch 'develop'", stage)
        self.assertIn("stash name: 'candidate'", stage)
        self.assertIn("prefetch registry", stage)
        self.assertIn("rm -f .build/oan-chart-candidate.json", stage)       # never a previous build's candidate

    def test_the_deploy_uses_the_candidate_only_when_this_build_has_one(self):
        stage = self.stage("Deploy to Development", "Upgrade connector (New Dev)")
        self.assertIn("rm -f .build/oan-chart-candidate.json", stage)
        self.assertIn("try { unstash 'candidate' }", stage)
        self.assertIn("OAN_CHART_PIN_FILE=.build/oan-chart-candidate.json", stage)
        self.assertIn("IMAGE_DIGESTS_FILE=.build/image-digests.tsv", stage)
        self.assertNotIn("catchError(", stage)                              # a failed deploy fails the build

    def test_without_a_candidate_nothing_is_upgraded(self):
        stage = self.stage("Deploy to Development", "Upgrade connector (New Dev)")
        self.assertIn("NEWDEV_SKIP_DEPLOY", stage)
        self.assertIn("unstable(", stage)                                   # a failed release stage is visible
        self.assertIn(".build/release-note.txt", stage)                     # a deliberate skip is not UNSTABLE
        self.assertLess(stage.index("NEWDEV_SKIP_DEPLOY == 'yes'"), stage.index("sh 'bash ci/deploy/deploy-livestock-dev.sh'"))
        self.assertIn("Skipping the upgrade", stage)

    def test_the_pin_stage_does_nothing_without_a_deployed_candidate(self):
        stage = self.text.split("stage('Propose chart pin update')", 1)[1]
        self.assertIn("the pin is not changed", stage)
        self.assertLess(stage.index("the pin is not changed"), stage.index("propose_pin_pr.py"))

    def test_the_pin_stage_records_the_deployed_candidate(self):
        stage = self.text.split("stage('Propose chart pin update')", 1)[1]
        self.assertIn("try { unstash 'candidate' }", stage)
        self.assertIn("OAN_CHART_PIN_FILE=.build/oan-chart-candidate.json", stage)
        self.assertIn("python3 ci/deploy/propose_pin_pr.py", stage)

    def test_braces_balance(self):
        self.assertEqual(self.text.count("{"), self.text.count("}"))


if __name__ == "__main__":
    unittest.main()
