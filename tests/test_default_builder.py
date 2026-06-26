#!/usr/bin/env python3
"""
Test runtime builder resolution from ~/.config/rdx/config.yaml.

rdx_app() no longer hardcodes builder='heroku'. The builder family is
resolved at RUNTIME (every Tiltfile evaluation) with this precedence:

  1. explicit builder= argument
  2. values.yaml `build.builder` (top-level OR nested under the lib key)
  3. machine `default_builder` from ~/.config/rdx/config.yaml
  4. 'heroku'

The selector is then mapped: 'heroku' -> DEFAULT_BUILDER,
'bci' -> BCI_BUILDER, anything else -> used as-is (raw CNB builder image).

The resolution lives in Starlark and cannot be imported, so this test
reimplements it byte-for-byte. Keep it aligned with rdx/Tiltfile's
builder-resolution block.

Usage: python3 test_default_builder.py
"""
import sys

DEFAULT_BUILDER = 'heroku/builder:24'
BCI_BUILDER = 'ghcr.io/idefxh/builder-bci-base:16.0.2'
BUILDER_IMAGES = {
    'heroku': DEFAULT_BUILDER,
    'bci': BCI_BUILDER,
}


def resolve_builder(builder=None, values=None, lib_values=None,
                    config_default='', builder_image=None):
    """Mirror of the Tiltfile builder-resolution block.

    Returns (builder_kind, builder_image_resolved) — the same
    (_builder_kind, builder) the Tiltfile computes.
    """
    values = values or {}
    lib_values = lib_values or {}

    _builder_sel = builder
    if _builder_sel == None or _builder_sel == '':
        _values_build = (values.get('build', {}) or {}).get('builder')
        if _values_build == None or _values_build == '':
            _values_build = (lib_values.get('build', {}) or {}).get('builder')
        _builder_sel = _values_build
    if _builder_sel == None or _builder_sel == '':
        _builder_sel = config_default
    if _builder_sel == None or _builder_sel == '':
        _builder_sel = 'heroku'

    if _builder_sel in BUILDER_IMAGES:
        _builder_kind = _builder_sel
        _resolved_builder = BUILDER_IMAGES[_builder_sel]
    else:
        _builder_kind = _builder_sel
        _resolved_builder = _builder_sel
    resolved = builder_image or _resolved_builder
    return _builder_kind, resolved


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_explicit_arg_wins_over_everything():
    kind, img = resolve_builder(
        builder='heroku',
        values={'build': {'builder': 'bci'}},
        config_default='bci')
    _check(kind == 'heroku', 'explicit builder= must win: ' + repr(kind))
    _check(img == DEFAULT_BUILDER, 'wrong image: ' + img)


def test_values_build_builder_used_when_no_arg():
    kind, img = resolve_builder(
        values={'build': {'builder': 'bci'}},
        config_default='heroku')
    _check(kind == 'bci', 'values build.builder should be used: ' + repr(kind))
    _check(img == BCI_BUILDER, 'wrong image: ' + img)


def test_values_build_builder_nested_under_lib_key():
    kind, img = resolve_builder(
        lib_values={'build': {'builder': 'bci'}},
        config_default='heroku')
    _check(kind == 'bci', 'lib-nested build.builder should be used: ' + repr(kind))
    _check(img == BCI_BUILDER, 'wrong image: ' + img)


def test_config_default_used_when_no_arg_no_values():
    kind, img = resolve_builder(config_default='bci')
    _check(kind == 'bci', 'config default_builder should apply: ' + repr(kind))
    _check(img == BCI_BUILDER, 'wrong image: ' + img)


def test_config_default_heroku_shorthand():
    kind, img = resolve_builder(config_default='heroku')
    _check(kind == 'heroku', repr(kind))
    _check(img == DEFAULT_BUILDER, img)


def test_config_default_raw_image_used_as_is():
    raw = 'registry.suse.com/bci/builder:42'
    kind, img = resolve_builder(config_default=raw)
    _check(kind == raw, 'raw image kept as kind: ' + repr(kind))
    _check(img == raw, 'raw image used as-is: ' + img)


def test_falls_back_to_heroku_when_nothing_set():
    kind, img = resolve_builder()
    _check(kind == 'heroku', 'default fallback must be heroku: ' + repr(kind))
    _check(img == DEFAULT_BUILDER, img)


def test_empty_string_config_falls_back_to_heroku():
    kind, img = resolve_builder(config_default='')
    _check(kind == 'heroku', repr(kind))
    _check(img == DEFAULT_BUILDER, img)


def test_builder_image_overrides_resolved_image_keeps_kind():
    # A raw builder_image override layers on top but the kind (for the
    # bci-only branches) still comes from the selector.
    kind, img = resolve_builder(
        builder='bci', builder_image='ghcr.io/idefxh/builder-bci-base:9.9.9')
    _check(kind == 'bci', 'kind keeps the bci selector: ' + repr(kind))
    _check(img == 'ghcr.io/idefxh/builder-bci-base:9.9.9',
           'builder_image override should win on the image: ' + img)


def test_precedence_values_beats_config():
    kind, _ = resolve_builder(
        values={'build': {'builder': 'heroku'}}, config_default='bci')
    _check(kind == 'heroku', 'values build.builder beats config default: '
           + repr(kind))


if __name__ == '__main__':
    failures = 0
    for fn in (test_explicit_arg_wins_over_everything,
               test_values_build_builder_used_when_no_arg,
               test_values_build_builder_nested_under_lib_key,
               test_config_default_used_when_no_arg_no_values,
               test_config_default_heroku_shorthand,
               test_config_default_raw_image_used_as_is,
               test_falls_back_to_heroku_when_nothing_set,
               test_empty_string_config_falls_back_to_heroku,
               test_builder_image_overrides_resolved_image_keeps_kind,
               test_precedence_values_beats_config):
        try:
            fn()
            sys.stdout.write('PASS %s\n' % fn.__name__)
        except AssertionError as e:
            failures += 1
            sys.stdout.write('FAIL %s: %s\n' % (fn.__name__, e))
    sys.exit(1 if failures else 0)
