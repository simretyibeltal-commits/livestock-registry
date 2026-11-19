"""The ECR names of the Livestock Registry images: eoan/livestock-registry/<image>-<RP version>:<version>-<branch>.<build>.

    eoan/livestock-registry/staff-ui-1.2.2-rc.614:0.0.0-develop.300
    eoan/oan_livestock_dashboard/dashboard-ui-1.2.2-rc.614:0.0.0-develop.300   (a dashboard, under its own repository)

The RP version is appVersion of helm/openg2p-livestock-registry/Chart.yaml (the OpenG2P registry platform the images
are built on). The pipeline, the chart packaging and the deploy all take the names from here.

Standard library only: it runs on the Jenkins agent's stock Python.
"""
import re
import sys
from pathlib import Path

CHART = Path(__file__).resolve().parents[1] / "helm/openg2p-livestock-registry/Chart.yaml"
PATH = "eoan/livestock-registry"
# The dashboards are built from their own GitHub repositories and are named under those, like every eoan image:
# eoan/<repository>/<image>-<RP version>.
PATHS = {"dashboard-api": "eoan/livestock-registry-dashbaord-api", "dashboard-ui": "eoan/oan_livestock_dashboard"}


def _chart_field(name):
    match = re.search(r"""(?m)^%s:\s*["']?([^"'\s]+)""" % name, CHART.read_text(encoding="utf-8"))
    if not match:
        raise ValueError("%s has no %s" % (CHART.name, name))
    return match.group(1)


def repository(image):
    return "%s/%s-%s" % (PATHS.get(image, PATH), image, _chart_field("appVersion"))


def tag(branch, build):
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
    if not slug or not re.fullmatch(r"[1-9]\d*", str(build)):
        raise ValueError("an image tag needs a branch and a build number")
    return "%s-%s.%s" % (_chart_field("version"), slug, build)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "tag":
            print(tag(sys.argv[2], sys.argv[3]))
        elif len(sys.argv) == 3 and sys.argv[1] == "repository":
            print(repository(sys.argv[2]))
        else:
            sys.exit("usage: image_names.py tag <branch> <build> | repository <image>")
    except (ValueError, OSError) as error:
        sys.exit("FAIL: %s" % error)
