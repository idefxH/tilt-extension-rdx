#!/usr/bin/env python3
"""Pin the DO-0016 P1 wiring: rdx_app consumes workloads_resolved[].build
from the projected overlay and registers the declared build."""
import os
import sys


def _expect(cond, msg):
    if not cond:
        sys.stderr.write("FAIL: " + msg + "\n")
        sys.exit(1)


def test_declared_build_wiring():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'rdx', 'Tiltfile')).read()
    body = src.split('def _register_declared_build(', 1)[1].split('\ndef ', 1)[0]
    _expect("workloads_resolved" in body,
            "consumer must read the projected contract key")
    _expect('docker_build(' in body and 'custom_build(' in body,
            "both strategies must register a Tilt image build")
    _expect("'live_update': _lu" in body or 'live_update=_lu' in body,
            "watch must map to live_update")
    _expect(body.index('if not block:') < body.index('docker_build('),
            "absent key must be a no-op before any registration")
    _expect('language' in body.split('docker_build(')[0],
            "legacy language/pack path must keep precedence")
    _expect("_register_declared_build(" in src.split('def rdx_app(', 1)[1],
            "rdx_app must invoke the consumer after projection")
    print("ok  test_declared_build_wiring")


def test_declared_dockerfile_overrides_language_marker():
    """A non-buildpack declared block drives the build even when the
    scaffolded rdx_app(language=...) argument is still in place: the
    image phase yields before the pack path, and the consumer falls
    through instead of deferring. Otherwise `rdx new` followed by
    `rdx project build --strategy dockerfile` pack-builds the template
    source and the declaration is silently ignored."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'rdx', 'Tiltfile')).read()
    img = src.split('def _resolve_image_strategy(', 1)[1].split('\ndef ', 1)[0]
    _expect("_declared_strategy != 'buildpack'" in img,
            "image phase must gate the legacy pack path on the declared strategy")
    _expect(img.index("_declared_strategy != 'buildpack'")
            < img.index('LANGUAGE_DEFAULTS[language]'),
            "the declared-build gate must run before the pack path engages")
    body = src.split('def _register_declared_build(', 1)[1].split('\ndef ', 1)[0]
    pre = body.split('docker_build(')[0]
    _expect("== 'buildpack'" in pre,
            "consumer's early return must be scoped to buildpack agreement")
    _expect('return' in pre[pre.index("== 'buildpack'"):],
            "buildpack agreement must still defer to the legacy pack path")
    print("ok  test_declared_dockerfile_overrides_language_marker")


if __name__ == '__main__':
    test_declared_build_wiring()
    test_declared_dockerfile_overrides_language_marker()
    print("\nAll declared-build tests passed.")
