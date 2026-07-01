#!/usr/bin/env python3
"""
Walk a library chart's Chart.yaml.full (or Chart.yaml as fallback) and
ensure every HTTP/HTTPS repository: URL is registered with helm. We
read .full because filter_enabled_deps strips Chart.yaml down to the
enabled subset — but a previously-disabled chart on an HTTP repo could
get re-enabled later, and we want the index ready when it does.

OCI repos are skipped (Helm doesn't cache an index for them).
Idempotent: helm repo add fails when a name+url is already registered;
we tolerate that.

Usage: bootstrap_helm_repos.py <library-chart-dir>
"""
import hashlib
import os
import re
import subprocess
import sys


def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        sys.stderr.write(
            ("[bootstrap_helm_repos] PyYAML not installed in this python3 " +
             "({0}); install with: python3 -m pip install --break-system-packages pyyaml\n"
            ).format(sys.executable)
        )
        sys.exit(2)


def _existing_repo_urls():
    try:
        out = subprocess.check_output(
            ["helm", "repo", "list", "-o", "yaml"],
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
    yaml = _yaml()
    data = yaml.safe_load(out) or []
    return {(item.get("name"), item.get("url")) for item in data if isinstance(item, dict)}


def _safe_name_for_url(url):
    # Helm repo names are short ascii. Use a slugified host + 6-char hash
    # so reruns produce the same name and `helm repo add` is idempotent.
    host = re.sub(r"^https?://", "", url).split("/")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "repo"
    short = hashlib.sha256(url.encode()).hexdigest()[:6]
    return "rdx-{0}-{1}".format(slug, short)


def main(library_dir):
    chart_yaml = os.path.join(library_dir, "Chart.yaml.full")
    if not os.path.isfile(chart_yaml):
        chart_yaml = os.path.join(library_dir, "Chart.yaml")
    if not os.path.isfile(chart_yaml):
        return
    yaml = _yaml()
    with open(chart_yaml) as f:
        doc = yaml.safe_load(f) or {}
    deps = doc.get("dependencies") or []
    http_urls = sorted({
        d.get("repository", "").strip()
        for d in deps if isinstance(d, dict)
        and isinstance(d.get("repository"), str)
        and d["repository"].startswith(("http://", "https://"))
    })
    if not http_urls:
        return
    existing = _existing_repo_urls()
    existing_urls = {url for _, url in existing}
    added = []
    for url in http_urls:
        if url in existing_urls:
            continue
        name = _safe_name_for_url(url)
        try:
            subprocess.check_call(
                ["helm", "repo", "add", name, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            added.append((name, url))
        except subprocess.CalledProcessError:
            # Likely already registered under a different name — fine,
            # let the subsequent --skip-refresh step succeed regardless.
            pass
    if added:
        print(
            "[rdx] bootstrapped {0} HTTP helm repo(s): {1}".format(
                len(added), ", ".join(u for _, u in added)
            ),
            flush=True,
        )
        # The added repos have no cached index yet; refresh just them.
        # `helm repo update <names>` is the targeted form (3.13+).
        try:
            subprocess.check_call(
                ["helm", "repo", "update"] + [n for n, _ in added],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError:
            # Older helm: untargeted update is the fallback.
            subprocess.call(["helm", "repo", "update"], stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: bootstrap_helm_repos.py <library-chart-dir>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
