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
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HELM = r'''#!/usr/bin/env bash
echo "helm $*" >> "$LOG"
case "$1" in
  template) echo "kind: ConfigMap" ;;
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
DASHBOARD_STUB = '#!/usr/bin/env bash\necho "deploy-ui $*" >> "$LOG"\nexit "${DASHBOARD_UI_RC:-0}"\n'

DEPLOYED = json.dumps([{"revision": 2, "status": "superseded"},
                       {"revision": 3, "status": "deployed", "chart": "openg2p-livestock-registry-1.0.0-dev.19"},
                       {"revision": 4, "status": "failed"}])

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
        shutil.copy(ROOT / "ci/deploy/bump_pin.py", self.repo / "ci/deploy/bump_pin.py")
        shutil.copy(ROOT / "ci/image_names.py", self.repo / "ci/image_names.py")
        (self.repo / "helm/openg2p-livestock-registry").mkdir(parents=True)
        shutil.copy(ROOT / "helm/openg2p-livestock-registry/Chart.yaml", self.repo / "helm/openg2p-livestock-registry/Chart.yaml")
        (self.repo / "ci/deploy/oan_chart.py").write_text(CHART_STUB)
        (self.repo / "ci/check-new-dev-routes.py").write_text(ROUTES_STUB)
        (self.repo / "ci/refactor/deployment_guard.py").write_text(GUARD_STUB)
        (self.repo / "ci/dashboard/deploy-ui.sh").write_text(DASHBOARD_STUB)
        # What 'Package chart' leaves: the registry chart, and the dashboard chart with its values in dashboard/.
        self.built = base / "built-chart"
        (self.built / "dashboard").mkdir(parents=True)
        (self.built / "openg2p-livestock-registry-1.0.0-develop.9.tgz").write_text("")
        (self.built / "dashboard/livestock-registry-dashboard-1.0.0-develop.9.tgz").write_text("")
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
                 IMAGE_TAG="abcdef123456", BUILD_NUMBER="7", BUILT_CHART_DIR=str(self.built),
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

    def test_the_rollback_target_names_the_release_it_restores(self):
        rc, out, calls = self.run_deploy()
        self.assertIn("rollback target: revision 3 (openg2p-livestock-registry-1.0.0-dev.19, the last working release)", out)

    def test_the_build_images_are_set_on_the_pinned_chart(self):
        rc, out, calls = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertIn("registry.staffApi.image.tag=abcdef123456", calls)
        rp = re.search(r"(?m)^appVersion: (\S+)$", (ROOT / "helm/openg2p-livestock-registry/Chart.yaml").read_text()).group(1)
        self.assertIn("registry.staffApi.image.repository=123456789012.dkr.ecr.ap-south-1.amazonaws.com/"
                      "eoan/livestock-registry/staff-api-" + rp, calls)
        self.assertNotIn("helm rollback", calls)

    def test_the_dashboard_images_are_named_by_ci_image_names(self):
        rc, out, calls = self.run_deploy()
        self.assertEqual(rc, 0, out)
        rp = re.search(r"(?m)^appVersion: (\S+)$", (ROOT / "helm/openg2p-livestock-registry/Chart.yaml").read_text()).group(1)
        # Under its own GitHub repository's name, like every eoan image.
        ecr = "123456789012.dkr.ecr.ap-south-1.amazonaws.com/eoan/livestock-registry-dashbaord-api/"
        self.assertIn("dashboardApi.image.repository=" + ecr + "dashboard-api-" + rp, calls)

    def test_the_dashboard_ui_is_installed_from_the_chart_this_build_packaged(self):
        rc, out, calls = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertIn("deploy-ui livestock livestock " + str(self.built / "dashboard"), calls)

    def test_no_dashboard_chart_no_dashboard_deploy(self):
        shutil.rmtree(str(self.built / "dashboard"))
        rc, out, calls = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("deploy-ui", calls)

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
        # one dev release past whatever is pinned now, so the test does not age with the pin
        base, dev = self.pinned["version"].rsplit(".", 1)
        self.newer = "%s.%d" % (base, int(dev) + 1)
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
        self.publish(self.newer, self.pinned["platformVersion"])
        new, why = self.propose()
        self.assertIsNotNone(new)
        self.assertIn("newer chart %s" % self.newer, why)
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
        self.publish(self.newer, "1.2.2-rc.700")
        new, _ = self.propose()
        self.assertEqual(new["registry"]["platformVersion"], "1.2.2-rc.700")
        self.assertEqual(self.bump.update_companions(self.pinned, new["registry"], root=self.root), [])
        self.assertEqual(self.bump.apply_pin(self.old, new, self.root), ["ci/deploy/oan-chart.json"])
        written = json.loads((self.root / "ci/deploy/oan-chart.json").read_text())
        self.assertEqual(written["registry"]["version"], self.newer)
        self.assertEqual(written["dashboard"], self.old["dashboard"])      # the dashboard pin is never touched


class PipelineWiringTests(unittest.TestCase):
    """ci/pipelines/dev.groovy: one pipeline, images -> deploy (with rollback) -> oan-helm pull request."""

    def setUp(self):
        self.text = (ROOT / "ci/pipelines/dev.groovy").read_text()

    def stage(self, name, following):
        return self.text.split("stage('%s')" % name, 1)[1].split("stage('%s')" % following, 1)[0]

    def test_stages_run_in_the_order_images_chart_deploy_publish(self):
        order = [self.text.index("stage('%s')" % n) for n in
                 ("Build & Push Images", "Package chart", "Deploy to Development", "Publish chart", "Deploy to Live")]
        self.assertEqual(order, sorted(order))

    def test_there_is_one_pipeline_file_for_the_normal_flow(self):
        self.assertFalse((ROOT / "ci/pipelines/deploy-newdev.groovy").exists())
        self.assertFalse((ROOT / "ci/deploy/latest_release.py").exists())

    def test_the_deploy_stage_still_fails_the_build_and_uses_the_chart_this_build_packaged(self):
        stage = self.stage("Deploy to Development", "Upgrade connector (New Dev)")
        self.assertIn("bash ci/deploy/deploy-livestock-dev.sh", stage)
        self.assertIn("unstash 'built-chart'", stage)
        self.assertIn("BUILT_CHART_DIR=${env.WORKSPACE}/built-chart", stage)
        self.assertIn("IMAGE_TAG=${env.IMAGE_TAG}", stage)
        self.assertNotIn("catchError(", stage)                              # a failed deploy fails the build
        for gone in ("OAN_CHART_PIN_FILE", "candidate", "NEWDEV_SKIP_DEPLOY"):
            self.assertNotIn(gone, self.text)

    def test_images_are_named_by_ci_image_names(self):
        build = self.stage("Build & Push Images", "Stash deploy")
        self.assertIn('python3 ci/image_names.py tag "$BRANCH_NAME" "$BUILD_NUMBER"', build)
        self.assertIn('python3 ci/image_names.py repository ${name}', build)
        self.assertIn('def image = "${env.ECR_REGISTRY}/${repository}:${env.IMAGE_TAG}"', build)
        self.assertNotIn("take(12)", self.text)

    def test_the_dashboard_ui_digest_goes_to_package_chart(self):
        build = self.stage("Build & Push Images", "Stash deploy")
        self.assertIn("if (env.DASHBOARD_UI_SHA) { digested << ['dashboard-ui', repositories['dashboard-ui']] }", build)
        self.assertNotIn("DASHBOARD_UI_SHA=", self.stage("Deploy to Development", "Upgrade connector (New Dev)"))

    def test_the_dashboard_chart_is_packaged_only_when_it_may_be_deployed(self):
        package = (ROOT / "ci/deploy/package-chart.sh").read_text()
        self.assertIn("printf 'image\\t%s\\t%s@%s\\n'", package)
        self.assertIn('[ "$(python3 ci/deploy/oan_chart.py is-deployable dashboard dev)" = "true" ]', package)
        self.assertIn("--output built-chart/dashboard", package)
        self.assertIn("oan_chart.py values dashboard built-chart/dashboard/values.yaml", package)

    def test_publish_chart_publishes_the_dashboard_chart_too(self):
        publish = (ROOT / "ci/deploy/publish-chart.sh").read_text()
        self.assertIn("for archive in built-chart/*.tgz built-chart/dashboard/*.tgz; do", publish)

    def test_the_dashboard_images_are_named_by_ci_image_names(self):
        build = self.stage("Build & Push Images", "Stash deploy")
        self.assertNotIn("DASHBOARD_API_ECR", self.text)
        self.assertNotIn("DASHBOARD_UI_ECR", self.text)
        self.assertIn("components.each { name ->", build)
        self.assertIn("def dashboards = ['dashboard-ui': env.DASHBOARD_UI_SHA, 'dashboard-api': env.DASHBOARD_API_SHA]", build)
        self.assertIn('def ui = "${env.ECR_REGISTRY}/${repositories[\'dashboard-ui\']}:${env.IMAGE_TAG}"', build)
        self.assertIn('def api = "${env.ECR_REGISTRY}/${repositories[\'dashboard-api\']}:${env.IMAGE_TAG}"', build)
        self.assertIn("digested << ['dashboard-api', repositories['dashboard-api']]", build)

    def test_the_chart_takes_each_image_s_repository_from_the_digest_file(self):
        package = (ROOT / "ci/deploy/package-chart.sh").read_text()
        self.assertIn("read -r component repository digest", package)
        self.assertIn('"$path" "$ECR_REGISTRY/$repository" "$IMAGE_TAG" "$digest"', package)

    def test_the_live_values_take_the_image_names_of_ci_image_names(self):
        self.assertNotIn("ECR_PATH", self.text)
        values = self.stage("Prepare Deploy Values", "Package chart")
        self.assertIn("unstash 'deploy'", values)
        self.assertIn("repository: \\${staff_api}", values)
        self.assertNotIn("declare -A", values)                   # Jenkins sh may be dash: no bash arrays

    def test_every_build_tests_the_image_names(self):
        self.assertIn("python3 -m unittest discover -s test -p test_image_names.py -v", self.text)

    def test_the_deploy_stash_carries_what_ci_image_names_reads(self):
        # The deploy agents run from this stash, and ci/image_names.py reads the chart's Chart.yaml.
        self.assertIn("stash name: 'deploy', includes: 'ci/**,scripts/**,.central-charts/**,"
                      "helm/openg2p-livestock-registry/Chart.yaml'", self.text)

    def test_digests_are_captured_after_the_pushes_and_stashed(self):
        build = self.stage("Build & Push Images", "Stash deploy")
        self.assertIn("aws ecr describe-images", build)
        self.assertIn("stash name: 'image-digests'", build)
        self.assertLess(build.index("docker push ${api}"), build.index("describe-images"))

    def test_the_chart_is_packaged_from_the_digests_before_the_deploy(self):
        stage = self.stage("Package chart", "Deploy to Development")
        self.assertIn("unstash 'image-digests'", stage)
        self.assertIn("bash ci/deploy/package-chart.sh", stage)
        self.assertIn("stash name: 'built-chart'", stage)
        self.assertNotIn("catchError", stage)                               # no chart, no deploy

    def test_the_publish_stage_follows_the_deploy_and_cannot_fail_the_build(self):
        stage = self.stage("Publish chart", "Deploy to Live")
        self.assertIn("catchError(buildResult: 'UNSTABLE', stageResult: 'UNSTABLE')", stage)
        self.assertIn("bash ci/deploy/publish-chart.sh", stage)
        self.assertIn("unstash 'built-chart'", stage)
        self.assertIn("OAN_HELM_FORK_CRED_ID", stage)
        self.assertIn("['fresh-install', 'connector-plan', 'connector-upgrade']", stage)
        for absent in ("KUBECONFIG", "helm upgrade", "stash name"):
            self.assertNotIn(absent, stage)

    def test_the_pull_request_stages_are_gone(self):
        self.assertNotIn("Open oan-helm image PR", self.text)
        self.assertNotIn("Propose chart pin update", self.text)

    def test_the_chart_has_no_fixed_version(self):
        chart = (ROOT / "helm/openg2p-livestock-registry/Chart.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 0.0.0\n", chart)

    def test_the_pipeline_takes_no_build_parameters(self):
        # merges deploy on their own; nobody fills in Build with Parameters.
        self.assertNotIn("parameters {", self.text)

    def test_braces_balance(self):
        self.assertEqual(self.text.count("{"), self.text.count("}"))


if __name__ == "__main__":
    unittest.main()
