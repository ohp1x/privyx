"""Local operator plugins (a conventional drop-in location).

Add a module here that subclasses
``privyx.privacy.operator.base.BaseOperator`` and sets a class-level ``name``.
List this directory under ``plugins.paths`` in your config and Privyx discovers
and registers it; select it with ``operator.type: <name>``.  See
../../docs/development/plugins.md.
"""
