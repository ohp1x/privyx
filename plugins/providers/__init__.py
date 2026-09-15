"""Local provider plugins (a conventional drop-in location).

Add a module here that subclasses ``privyx.providers.base.BaseProvider`` and
sets a class-level ``name``.  List this directory under ``plugins.paths`` in
your config and Privyx discovers and registers it; select it with
``provider.type: <name>``.  See ../../docs/development/plugins.md.
"""
