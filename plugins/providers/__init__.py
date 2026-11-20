"""Local provider plugins (a conventional drop-in location).

Add a module here with a subclass of ``privyx.providers.Provider`` that sets a
class-level ``name``, and a ``setup(app)`` function that adds it with
``app.add``.  List this directory under ``plugins.paths`` in your config and
Privyx calls each module's ``setup``; select the provider with
``provider.type: <name>``.  See ../../docs/development/plugins.md.
"""
