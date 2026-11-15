"""djrs: a deterministic Django-to-Rust transpiler.

Three parts:

* ``djrs.extract``  runs inside a Django project and writes a JSON description
  of it (the IR): versions, settings that affect request handling, models,
  the URL tree in Django's matching order, views, serializers and signals.
* ``djrs.generate`` turns that JSON into a Cargo crate built on the
  ``djrs-runtime`` crate. Routes it cannot translate are forwarded to Django.
* ``djrs.testing``  a Django test runner that sends the project's own tests
  through the generated binary instead of calling Django in-process.
"""

__version__ = "0.1.0"

# Bump when the IR's shape changes in a way the generator must know about.
IR_SCHEMA_VERSION = 1
