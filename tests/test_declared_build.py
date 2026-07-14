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
    _expect('live_update=_lu' in body, "watch must map to live_update")
    _expect(body.index('if not block:') < body.index('docker_build('),
            "absent key must be a no-op before any registration")
    _expect('language' in body.split('docker_build(')[0],
            "legacy language/pack path must keep precedence")
    _expect("_register_declared_build(" in src.split('def rdx_app(', 1)[1],
            "rdx_app must invoke the consumer after projection")
    print("ok  test_declared_build_wiring")


if __name__ == '__main__':
    test_declared_build_wiring()
    print("\nAll declared-build tests passed.")
