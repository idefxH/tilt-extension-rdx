#!/usr/bin/env python3
"""Pin the push-mode pack invocation: RDX_TILT_PUSH_TO_REGISTRY=1 must
publish from pack itself (--publish) and keep Tilt out of the docker
daemon. The daemon-export path breaks on containerd-image-store daemons
(current Docker Desktop default): the lifecycle fails with "no suitable
export target found" on the run image, so push mode may not round-trip
the built image through dockerd at all."""
import os
import sys


def _expect(cond, msg):
    if not cond:
        sys.stderr.write("FAIL: " + msg + "\n")
        sys.exit(1)


def _src():
    return open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', 'rdx', 'Tiltfile')).read()


def test_legacy_pack_path_publishes_in_push_mode():
    body = _src().split('def _resolve_image_strategy(',
                        1)[1].split('\ndef ', 1)[0]
    gate = body.index("RDX_TILT_PUSH_TO_REGISTRY")
    _expect("--publish" in body[gate:],
            "push mode must append --publish to the pack invocation")
    _expect("_skips_local_docker = True" in body,
            "tilt must not expect the pack image in the local daemon")
    _expect("_disable_push = True" in body,
            "tilt must not push the pack image itself")
    _expect("skips_local_docker=False" not in body,
            "no pack registration may re-enter the daemon-export path")
    cache_gate = body.index("_cache_fmt = 'volume'\n",
                            body.index("RDX_TILT_PUSH_TO_REGISTRY"))
    _expect(cache_gate < body.index("if _cache_fmt == 'bind':"),
            "publish mode must force volume caches before the flag emit "
            "(bind sources cannot be chowned by the builder user)")
    print("ok  test_legacy_pack_path_publishes_in_push_mode")


def test_declared_buildpack_publishes_in_push_mode():
    body = _src().split('def _register_declared_build(',
                        1)[1].split('\ndef ', 1)[0]
    seg = body.split('custom_build(', 1)[1]
    _expect("--publish" in seg,
            "declared buildpack must publish in push mode")
    _expect("skips_local_docker=_pub" in seg,
            "declared buildpack must skip the daemon when publishing")
    _expect("disable_push=_pub" in seg,
            "declared buildpack must not double-push when publishing")
    print("ok  test_declared_buildpack_publishes_in_push_mode")


def test_registry_from_cluster_knob():
    """RDX_REGISTRY_FROM_CLUSTER carries the node-side registry name on
    remote-daemon setups; unset must leave the single-host behaviour."""
    src = _src()
    _expect("RDX_REGISTRY_FROM_CLUSTER" in src,
            "the cluster-side registry knob must exist")
    seg = src.split("RDX_REGISTRY_FROM_CLUSTER", 1)[1]
    _expect("host_from_cluster=" in seg.split('\ndef ', 1)[0],
            "the knob must map to default_registry(host_from_cluster=...)")
    _expect("default_registry(_rdx_default_registry)" in src,
            "unset knob must keep the single-host registration")
    print("ok  test_registry_from_cluster_knob")


if __name__ == '__main__':
    test_legacy_pack_path_publishes_in_push_mode()
    test_declared_buildpack_publishes_in_push_mode()
    test_registry_from_cluster_knob()
    print("\nAll pack-publish tests passed.")
