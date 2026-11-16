"""The plugin API: the public module a component is written with."""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_the_public_module_names_what_a_plugin_needs() -> None:
    import privyx.plugins as api
    from privyx.core.result import Detection
    from privyx.privacy.operator.base import restore
    from privyx.vault.base import BaseVault

    assert api.Detection is Detection
    assert api.restore is restore
    assert api.BaseVault is BaseVault
    assert all(hasattr(api, name) for name in api.__all__)
    with pytest.raises(AttributeError):
        _ = api.Nothing  # type: ignore[attr-defined]


def test_the_base_classes_load_on_first_use() -> None:
    # Every build_* function reads PLUGINS, so importing the module stays light.
    code = (
        "import sys, privyx.plugins as api\n"
        "before = 'privyx.providers.base' in sys.modules\n"
        "api.BaseProvider\n"
        "print(before, 'privyx.providers.base' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["False", "True"]
