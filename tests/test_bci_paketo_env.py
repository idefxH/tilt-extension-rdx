#!/usr/bin/env python3
"""
Test the Paketo (BCI builder) BP_*/BPE_* build-env knobs added to rdx_app().

The BCI builder (ghcr.io/idefxh/builder-bci-base:16.0.x) ships Paketo
buildpacks that expose build-time knobs the heroku/* buildpacks don't read.
rdx_app() surfaces the useful ones as dedicated arguments and translates
them to `pack build --env` flags in the Tiltfile. The relevant logic lives
in two PURE helpers — `_resolve_bci_build_env` and
`_bci_reload_default_process` — plus two small inline branches (the
"bci-only knob" warning gate and the default-process selection).

Unlike the other tests in this dir (which reimplement the Tiltfile logic by
hand), this test EXTRACTS the two pure helpers straight from rdx/Tiltfile via
the ast module and execs them, so the assertions run against the real code and
can't drift from it. The Tiltfile is Starlark, but these helpers are a pure
Python subset (no Tilt builtins), so they exec unchanged under CPython.

Covered:
  1. live_reload -> BP_LIVE_RELOAD_ENABLED=true, but ONLY when the language's
     buildpack contributes a watchexec `reload` process (nodejs/python/java;
     NOT go). Selecting a non-existent `reload` process would hard-fail
     `pack build`, so go must fall back to a full rebuild.
  2. node_version -> BP_NODE_VERSION (nodejs only).
  3. bp_log_level -> BP_LOG_LEVEL.
  4. image_labels -> BP_IMAGE_LABELS as sorted, space-delimited key=value.
  5. runtime_cert_binding=False -> BP_ENABLE_RUNTIME_CERT_BINDING=false; True
     / None emit nothing (Paketo defaults runtime cert binding ON).
  6. debug -> BPE_DEFAULT_BPL_DEBUG_ENABLED=true (+ _PORT when debug_port set).
  7. The "bci-only knob" warning fires (knob list non-empty) for non-bci
     builders and is empty when nothing is set.
  8. default-process selection: 'reload' for bci+live_reload+supported, else
     the Procfile-gated default.
  9. Curated knobs are applied AFTER additional_env so they win on a clash.

Usage: python3 test_bci_paketo_env.py
"""
import ast
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TILTFILE = os.path.join(_THIS_DIR, '..', 'rdx', 'Tiltfile')


def _load_helpers():
    """Exec the two pure helpers out of rdx/Tiltfile so we test the real code."""
    src = open(_TILTFILE).read()
    tree = ast.parse(src)
    wanted = ('_resolve_bci_build_env', '_bci_reload_default_process')
    ns = {}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            segment = ast.get_source_segment(src, node)
            exec(compile(segment, _TILTFILE, 'exec'), ns)
            found.add(node.name)
    missing = set(wanted) - found
    if missing:
        raise AssertionError('helpers not found in Tiltfile: %s' % missing)
    return ns['_resolve_bci_build_env'], ns['_bci_reload_default_process']


_resolve_bci_build_env, _bci_reload_default_process = _load_helpers()


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# Convenience wrapper mirroring the argument order of the real helper, with
# all knobs defaulted off so each test sets only what it exercises.
def _env(language='nodejs', live_reload=False, reload_supported=True,
         node_version=None, bp_log_level=None, image_labels=None,
         runtime_cert_binding=None, debug=False, debug_port=None):
    return _resolve_bci_build_env(
        language, live_reload, reload_supported, node_version, bp_log_level,
        image_labels, runtime_cert_binding, debug, debug_port)


def test_all_off_is_empty():
    _check(_env() == {}, 'no knobs set must yield no env: ' + repr(_env()))


def test_live_reload_supported():
    e = _env(live_reload=True, reload_supported=True)
    _check(e.get('BP_LIVE_RELOAD_ENABLED') == 'true',
           'live_reload on a supported language must set the flag: ' + repr(e))


def test_live_reload_unsupported_language_emits_nothing():
    # go: reload_supported=False. Emitting BP_LIVE_RELOAD_ENABLED here would
    # bake a watchexec layer nothing runs (and pairs with a 'reload' process
    # that doesn't exist).
    e = _env(language='go', live_reload=True, reload_supported=False)
    _check('BP_LIVE_RELOAD_ENABLED' not in e,
           'live_reload must NOT set the flag when unsupported: ' + repr(e))


def test_node_version_nodejs_only():
    e = _env(language='nodejs', node_version='22')
    _check(e.get('BP_NODE_VERSION') == '22', repr(e))
    # ints accepted and stringified
    _check(_env(language='nodejs', node_version=20).get('BP_NODE_VERSION') == '20',
           'node_version int must stringify')
    e2 = _env(language='python', node_version='22')
    _check('BP_NODE_VERSION' not in e2,
           'BP_NODE_VERSION must not leak onto non-node languages: ' + repr(e2))


def test_bp_log_level():
    _check(_env(bp_log_level='DEBUG').get('BP_LOG_LEVEL') == 'DEBUG', 'log level')


def test_image_labels_sorted_space_delimited():
    e = _env(image_labels={'b.label': '2', 'a.label': '1'})
    _check(e.get('BP_IMAGE_LABELS') == 'a.label=1 b.label=2',
           'image labels must be sorted, space-delimited key=value: ' + repr(e))
    _check('BP_IMAGE_LABELS' not in _env(image_labels={}),
           'empty image_labels emits nothing')


def test_runtime_cert_binding_opt_out_only():
    _check(_env(runtime_cert_binding=False).get('BP_ENABLE_RUNTIME_CERT_BINDING')
           == 'false', 'False must disable runtime cert binding')
    # Paketo defaults runtime cert binding ON, so True/None must emit nothing.
    _check('BP_ENABLE_RUNTIME_CERT_BINDING' not in _env(runtime_cert_binding=True),
           'True must not emit (already the Paketo default)')
    _check('BP_ENABLE_RUNTIME_CERT_BINDING' not in _env(runtime_cert_binding=None),
           'None must not emit')


def test_debug_bakes_launch_default():
    e = _env(debug=True)
    _check(e.get('BPE_DEFAULT_BPL_DEBUG_ENABLED') == 'true', repr(e))
    _check('BPE_DEFAULT_BPL_DEBUG_PORT' not in e,
           'no port without debug_port: ' + repr(e))
    e2 = _env(debug=True, debug_port=5005)
    _check(e2.get('BPE_DEFAULT_BPL_DEBUG_PORT') == '5005', repr(e2))
    _check('BPE_DEFAULT_BPL_DEBUG_ENABLED' not in _env(debug=False),
           'debug=False bakes nothing')


def test_reload_default_process():
    _check(_bci_reload_default_process(True, True) == 'reload',
           'live_reload + supported -> reload')
    _check(_bci_reload_default_process(True, False) == None,
           'live_reload + unsupported -> None (fall back)')
    _check(_bci_reload_default_process(False, True) == None,
           'no live_reload -> None')
    _check(_bci_reload_default_process(False, False) == None, 'both off -> None')


# ── Inline-branch mirrors (kept byte-aligned with the build block) ──────────

def _bci_only_knobs(builder_kind, live_reload=False, node_version=None,
                    bp_log_level=None, image_labels=None,
                    runtime_cert_binding=None, debug=False):
    """Mirror of the build block's bci-only-knob detection."""
    if builder_kind == 'bci':
        return []
    knobs = []
    if live_reload:
        knobs.append('live_reload')
    if node_version != None:
        knobs.append('node_version')
    if bp_log_level != None:
        knobs.append('bp_log_level')
    if image_labels != None:
        knobs.append('image_labels')
    if runtime_cert_binding != None:
        knobs.append('runtime_cert_binding')
    if debug:
        knobs.append('debug')
    return knobs


def _select_default_process(builder_kind, live_reload, reload_supported,
                            pack_default_process, procfile_has_default):
    """Mirror of the build block's default-process selection."""
    reload_proc = None
    if builder_kind == 'bci':
        reload_proc = _bci_reload_default_process(live_reload, reload_supported)
    if reload_proc:
        return reload_proc
    if pack_default_process and procfile_has_default:
        return pack_default_process
    return None


def test_bci_only_knob_warning_gate():
    _check(_bci_only_knobs('bci', live_reload=True, debug=True) == [],
           'bci builder never warns about its own knobs')
    knobs = _bci_only_knobs('heroku', live_reload=True, node_version='22',
                            debug=True, runtime_cert_binding=False)
    _check('live_reload' in knobs and 'node_version' in knobs and
           'debug' in knobs and 'runtime_cert_binding' in knobs,
           'heroku must surface every set bci-only knob: ' + repr(knobs))
    _check(_bci_only_knobs('heroku') == [],
           'no knobs set -> no warning even on heroku')


def test_default_process_selection():
    # bci + live_reload + supported -> watchexec reload process
    _check(_select_default_process('bci', True, True, 'dev', True) == 'reload',
           'bci live_reload picks reload')
    # bci + live_reload + unsupported (go) -> Procfile default if present
    _check(_select_default_process('bci', True, False, None, False) == None,
           'go w/ no procfile default -> None')
    # bci WITHOUT live_reload -> nodemon dev (Procfile-gated)
    _check(_select_default_process('bci', False, True, 'dev', True) == 'dev',
           'bci default keeps the nodemon dev process')
    _check(_select_default_process('bci', False, True, 'dev', False) == None,
           'no dev: in Procfile -> no override (brownfield web-only)')
    # heroku never selects reload regardless of live_reload
    _check(_select_default_process('heroku', True, True, 'dev', True) == 'dev',
           'heroku ignores reload, uses Procfile dev')


def _emit_env_flag(k, v):
    """Mirror of the build block's shell-safe --env emission for BCI knobs."""
    if ' ' in str(v):
        return '--env %s="%s"' % (k, v)
    return '--env %s=%s' % (k, v)


def test_image_labels_shell_quoted():
    # BP_IMAGE_LABELS holds space-delimited pairs; custom_build runs the pack
    # command through a shell, so the value MUST be quoted or `team=platform`
    # splits off into a stray positional arg to pack.
    env = _env(image_labels={'a': '1', 'team': 'platform'})
    flag = _emit_env_flag('BP_IMAGE_LABELS', env['BP_IMAGE_LABELS'])
    _check(flag == '--env BP_IMAGE_LABELS="a=1 team=platform"',
           'space-bearing value must be double-quoted: ' + flag)
    # No-space values stay unquoted (unchanged from prior behaviour).
    _check(_emit_env_flag('BP_NODE_VERSION', '22') == '--env BP_NODE_VERSION=22',
           'no-space value must not be quoted')


def test_curated_knobs_win_over_additional_env():
    # The build block applies env in this order; pack honours last-wins on a
    # duplicate key, so the curated knob must come AFTER additional_env.
    additional_env = {'BP_NODE_VERSION': '18'}
    flags = []
    for k, v in additional_env.items():
        flags.append('%s=%s' % (k, v))
    for k, v in _env(language='nodejs', node_version='22').items():
        flags.append('%s=%s' % (k, v))
    # Last occurrence of BP_NODE_VERSION must be the curated 22.
    last = [f for f in flags if f.startswith('BP_NODE_VERSION=')][-1]
    _check(last == 'BP_NODE_VERSION=22',
           'curated node_version must win over additional_env: ' + repr(flags))


if __name__ == '__main__':
    failures = 0
    tests = [
        test_all_off_is_empty,
        test_live_reload_supported,
        test_live_reload_unsupported_language_emits_nothing,
        test_node_version_nodejs_only,
        test_bp_log_level,
        test_image_labels_sorted_space_delimited,
        test_runtime_cert_binding_opt_out_only,
        test_debug_bakes_launch_default,
        test_reload_default_process,
        test_bci_only_knob_warning_gate,
        test_default_process_selection,
        test_image_labels_shell_quoted,
        test_curated_knobs_win_over_additional_env,
    ]
    for fn in tests:
        try:
            fn()
            sys.stdout.write('PASS %s\n' % fn.__name__)
        except AssertionError as e:
            failures += 1
            sys.stdout.write('FAIL %s: %s\n' % (fn.__name__, e))
    sys.exit(1 if failures else 0)
