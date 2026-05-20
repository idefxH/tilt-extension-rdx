#!/usr/bin/env python3
"""
Align vendored sub-chart Chart.yaml versions with the parent's dep
declarations.

AppCo (and possibly other) Helm charts publish OCI artifact tags that
don't match the chart's internal Chart.yaml version (e.g. tag
"0.4.4-29.1", chart version "0.4.4"). Helm's dep-matching at template
time uses the chart's INTERNAL version, so the alias the parent
declared silently fails to resolve and parent values never reach the
sub-chart. The pulled tgz is correct; we just need to rewrite its
Chart.yaml so the version matches what the parent says.

Usage: align_subchart_versions.py <parent-chart-dir>

Walks <parent>/charts/*.tgz; for each, peeks at Chart.yaml, if its
version differs from what <parent>/Chart.yaml's `dependencies` say for
the same chart name, rewrites the tgz with the aligned version.

Idempotent: skips if already aligned.
"""
import glob
import os
import shutil
import sys
import tarfile

def _safe_extract(tf, dest):
    # py3.12+ added the `filter` kwarg + the data_filter helper.
    # py<3.12 doesn't accept `filter=` — fall back to the legacy
    # extractall() (no member sanitisation). Acceptable here because
    # the input is a chart .tgz we just downloaded from our own
    # registry and helm dep update validated.
    if hasattr(tarfile, 'data_filter'):
        tf.extractall(dest, filter='data')
    else:
        tf.extractall(dest)



# We import yaml lazily so a missing PyYAML doesn't crash on charts that
# need no patching.
def _yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        sys.stderr.write(
            "[align_subchart_versions] PyYAML not installed; "
            "pip3 install pyyaml or run with python3 -m pip install pyyaml\n"
        )
        sys.exit(0)


def main(parent_dir):
    parent_chart_yaml = os.path.join(parent_dir, "Chart.yaml")
    if not os.path.isfile(parent_chart_yaml):
        return
    yaml = _yaml()
    with open(parent_chart_yaml) as f:
        parent = yaml.safe_load(f) or {}
    deps = parent.get("dependencies") or []
    wanted = {d["name"]: d["version"] for d in deps if "name" in d and "version" in d}
    if not wanted:
        return
    subdir = os.path.join(parent_dir, "charts")
    if not os.path.isdir(subdir):
        return
    fixed = 0
    for tgz in glob.glob(os.path.join(subdir, "*.tgz")):
        work = tgz + ".unpack"
        if os.path.isdir(work):
            shutil.rmtree(work)
        os.makedirs(work)
        try:
            with tarfile.open(tgz, "r:gz") as tf:
                _safe_extract(tf, work)  # tarfile filter handling — py3.12+ supports `filter='data'`, older python doesn't
            inner = next(
                (d for d in os.listdir(work) if os.path.isdir(os.path.join(work, d))),
                None,
            )
            if not inner:
                continue
            cy_path = os.path.join(work, inner, "Chart.yaml")
            with open(cy_path) as f:
                cy = yaml.safe_load(f) or {}
            name = cy.get("name")
            want = wanted.get(name)
            if want and cy.get("version") != want:
                cy["version"] = want
                with open(cy_path, "w") as f:
                    yaml.safe_dump(cy, f, default_flow_style=False, sort_keys=False)
                new_tgz = tgz + ".new"
                with tarfile.open(new_tgz, "w:gz") as tf:
                    tf.add(os.path.join(work, inner), arcname=inner)
                os.replace(new_tgz, tgz)
                fixed += 1
                print(
                    "[rdx] aligned {0} {1} -> {2}".format(name, cy.get("version"), want),
                    flush=True,
                )
        finally:
            if os.path.isdir(work):
                shutil.rmtree(work)
    if fixed:
        print(
            "[rdx] patched {0} sub-chart Chart.yaml version(s) under {1}".format(
                fixed, parent_dir
            ),
            flush=True,
        )


if __name__ == "__main__":
    main(sys.argv[1])
