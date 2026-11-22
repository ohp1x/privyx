"""Local detector plugins (a conventional drop-in location).

Add a module here with a subclass of ``privyx.detectors.Detector`` that sets a
class-level ``name``, and a ``setup(app)`` function that adds it with
``app.add``.  List this directory under ``plugins.paths`` in your config and
Privyx calls each module's ``setup``; turn the detector on by its name under
``detectors``.

The subfolder is only a convention: a plugin in any configured path can add
a component of any kind.  See ../../docs/development/plugins.md.
"""
