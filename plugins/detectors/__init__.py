"""Local detector plugins (a conventional drop-in location).

Add a module here that subclasses
``privyx.privacy.detector.base.BaseDetector`` and sets a class-level ``name``.
List this directory under ``plugins.paths`` in your config and Privyx discovers
and registers it; select it with ``detector.type: <name>``.

The subfolder is only a convention — discovery is by subclass, so a plugin in
any configured path can define any component family.  See
../../docs/development/plugins.md.
"""
