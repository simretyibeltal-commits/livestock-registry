"""Exact central chart consumer, following Farmer's verified archive contract.

No dependency resolution, local-chart fallback, extraction or environment writes.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import urllib.request

PROFILE = Path(__file__).with_name("oan-chart.json")
# The pin of the chart release a build has just produced from its own images (ci/deploy/release_images.py). It has
# the same shape and goes through the same checks; only the chart it names is newer than the committed pin.
CANDIDATE_ENV = "OAN_CHART_PIN_FILE"
CENTRAL_CACHE = Path(__file__).resolve().parents[2] / ".central-charts"


def yaml_document(data):
    try:
        import yaml
    except ModuleNotFoundError:
        # The deploy node runs the stock Python without site packages or a
        # container registry; ci/deploy/vendor carries the pure-Python PyYAML
        # (MIT, ci/deploy/vendor/yaml/LICENSE) of ci/deploy/requirements.txt.
        sys.path.append(str(Path(__file__).with_name("vendor")))
        try:
            import yaml
        except ModuleNotFoundError:
            raise ValueError('Install ci/deploy/requirements.txt before chart verification') from None
    try:
        return yaml.safe_load(data)
    except yaml.YAMLError:
        raise ValueError('Invalid packaged YAML metadata') from None

def profiles():
    override = os.environ.get(CANDIDATE_ENV)
    return json.loads((Path(override) if override else PROFILE).read_text(encoding="utf-8"))


def yaml_module():
    yaml_document(b"{}")                     # makes the vendored PyYAML importable on the stock Python
    import yaml
    return yaml


# The image sections the oan-helm chart carries for this registry (release_images.py writes their tags).
IMAGE_SECTIONS = ("staffApi", "partnerApi", "celeryWorker", "celeryBeat", "dbSeed", "staffUi", "sanity")


def strip_images(path):
    """Remove every image setting from a saved values file, so the chart's own images (tag and digest) apply.

    `helm get values --all` records the image the old release ran; passed back with -f it would override the images of
    the new chart. Everything else in the file (hostnames, wiring) is kept.
    """
    yaml = yaml_module()
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    removed = 0
    registry = document.get("registry")
    if isinstance(registry, dict):
        for section in IMAGE_SECTIONS:
            if isinstance(registry.get(section), dict) and registry[section].pop("image", None) is not None:
                removed += 1
    dashboard = document.get("dashboardApi")
    if isinstance(dashboard, dict) and dashboard.pop("image", None) is not None:
        removed += 1
    Path(path).write_text(yaml.safe_dump(document, default_flow_style=False), encoding="utf-8")
    return removed

def validate_pin(pin):
    if not re.fullmatch(r"https://raw\.githubusercontent\.com/Centre-for-Open-Societal-Systems/oan-helm/[0-9a-f]{40}/repository/",pin["repository"]):
        raise ValueError("Central chart URL must pin an immutable COSS oan-helm commit")
    if not re.fullmatch(r"[0-9a-f]{64}",pin["sha256"]):
        raise ValueError("Missing central chart SHA-256")
    if not re.fullmatch(r"[a-z0-9-]+",pin["name"]) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?",pin["version"]):
        raise ValueError("Invalid central chart identity/version")
    if pin["version"].startswith("0.0.0"):
        raise ValueError("Placeholder chart version is forbidden")
    # Values are fetched from the chart commit; a separate url that names another commit is never read.
    # No str.removesuffix here: the deploy node runs a Python older than 3.9.
    values = pin.get("values") or {}
    if values.get("url") and values["url"] != pin["repository"][:-len("repository/")] + values.get("path", ""):
        raise ValueError("Central public-values url must be the file at the pinned chart commit")
    return pin

def url(pin):
    validate_pin(pin)
    return pin["repository"] + pin["name"] + "-" + pin["version"] + ".tgz"

def verify(path, pin):
    validate_pin(pin)
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != pin["sha256"]:
        raise ValueError("Central chart checksum mismatch")
    with tarfile.open(path,"r:gz") as archive:
        def read(name):
            member = archive.getmember(pin["name"] + "/" + name)
            if not member.isfile() or member.size > 1024*1024:
                raise ValueError("Invalid packaged chart metadata")
            return yaml_document(archive.extractfile(member))
        info = read("Chart.yaml")
        if info["name"] != pin["name"] or info["version"] != pin["version"]:
            raise ValueError("Wrong central chart name/version")
        if pin.get("platformVersion"):
            dependency = {"alias":"registry","name":"openg2p-registry", "repository":"https://openg2p.github.io/openg2p-helm",
                          "version":pin["platformVersion"]}
            if info.get("dependencies") != [dependency] or info.get("appVersion") != pin["platformVersion"]:
                raise ValueError("Central chart/platform pin mismatch")
            child = read("charts/openg2p-registry/Chart.yaml")
            if child["name"] != dependency["name"] or child["version"] != pin["platformVersion"]:
                raise ValueError("Wrong packaged native platform")
            if not pin["promotionReady"]:
                member = archive.extractfile(pin["name"] + "/templates/refactor-promotion-gate.yaml")
                if b'fail "develop-refactor is incomplete:' not in member.read():
                    raise ValueError("Blocked candidate lost its native render gate")
    return pin

def fetch(pin, destination):
    target_dir = Path(destination)
    target_dir.mkdir(parents=True,exist_ok=True)
    target = target_dir / (pin["name"] + "-" + pin["version"] + ".tgz")
    if target.exists():
        verify(target,pin)
        return target.resolve()
    cached = CENTRAL_CACHE / pin['sha256'] / target.name
    # Atomic replacement only after a successful bounded download/checksum.
    partial = target.with_suffix(".partial")
    try:
        if cached.exists():
            verify(cached,pin)
            data = cached.read_bytes()
        else:
            with urllib.request.urlopen(url(pin),timeout=30) as response:
                data = response.read(32*1024*1024+1)
        if len(data)>32*1024*1024:
            raise ValueError("Central chart exceeds approved size limit")
        partial.write_bytes(data)
        verify(partial,pin)
        partial.replace(target)
    finally:
        if partial.exists():
            partial.unlink()
    return target.resolve()

def is_deployable(pin, environment):
    validate_pin(pin)
    return pin.get("environment") == environment and pin.get("promotionReady") is True


def require_deployable(pin, environment):
    if not is_deployable(pin, environment):
        raise ValueError("Deployment blocked: refactor acceptance and environment preservation evidence are required; existing environments are unchanged")


def fetch_values(pin, destination):
    """Retrieve reviewed public values from the same immutable central commit."""
    validate_pin(pin)
    values = pin['values']
    if not re.fullmatch(r'environments/dev/values/[a-z-]+\.yaml',values['path']):
        raise ValueError('Invalid central public-values path')
    if not re.fullmatch(r'[a-f0-9]{64}',values['sha256']):
        raise ValueError('Missing central public-values checksum')
    cached = CENTRAL_CACHE / values['sha256'] / Path(values['path']).name
    if cached.exists():
        data = cached.read_bytes()
    else:
        uri = pin['repository'][:-len('repository/')]+values['path']
        with urllib.request.urlopen(uri,timeout=30) as response:
            data = response.read(1024*1024+1)
    if len(data)>1024*1024 or hashlib.sha256(data).hexdigest()!=values['sha256']:
        raise ValueError('Central public-values checksum mismatch')
    if not isinstance(yaml_document(data),dict):
        raise ValueError('Invalid central public-values document')
    target=Path(destination)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(data)
    return target.resolve()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action",choices=("url","verify","fetch","values","prefetch","require-deployable","is-deployable","platform","version","strip-images"))
    parser.add_argument("role",choices=("registry","dashboard"))
    parser.add_argument("argument",nargs="?")
    args = parser.parse_args()
    pin = validate_pin(profiles()[args.role])
    if args.action=="url": print(url(pin))
    elif args.action=="verify": verify(args.argument,pin); print("PASS: central chart checksum, identity and native platform")
    elif args.action=="fetch": print(fetch(pin,args.argument))
    elif args.action=="values": print(fetch_values(pin,args.argument))
    elif args.action=="prefetch":
        fetch(pin,CENTRAL_CACHE / pin['sha256'])
        fetch_values(pin,CENTRAL_CACHE / pin['values']['sha256'] / Path(pin['values']['path']).name)
        print('PASS: central archive and public values verified for offline deploy transport')
    elif args.action=="require-deployable": require_deployable(pin,args.argument or "dev")
    elif args.action=="is-deployable": print("true" if is_deployable(pin,args.argument or "dev") else "false")
    elif args.action=="platform": print(pin["platformVersion"])
    elif args.action=="version": print(pin["version"])
    elif args.action=="strip-images": print("removed image settings from %d sections" % strip_images(args.argument))

if __name__=="__main__":
    try:
        main()
    except (ValueError,OSError,KeyError,tarfile.TarError) as error:
        raise SystemExit("FAIL: " + str(error)) from None
