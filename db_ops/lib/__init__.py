"""Pure helpers every component may import in-process.

Split out of ``common`` on 2026-08-15, when the rule became "an app does not import ``common``;
it calls the ``common`` CLI". That rule is right for the modules that *do* something — reach a
host, run SQL, move a file — because those are operations and an operation can be a process. It
cannot apply to these, and not for reasons of taste:

* **A class does not come back from a subprocess.** ``metrics`` builds ``MetricResult`` objects,
  ``NotifyConfig`` and ``ParsedTimeWindow`` are parsed once per config load and passed around.
* **Some of it runs per row.** ``policy_engine`` classifies every metric row — roughly 29,000 for
  one database's index inventory — and ``time_window`` is consulted on every daemon tick. A
  process per call is not a slower design, it is a broken one.

So the layer split by what a thing *is*, not by who calls it: an operation goes through the CLI,
a value or a rule about values is imported. Everything here is a pure function of its arguments
and imports nothing from ``db_ops`` — ``tests/test_lib_is_pure.py`` holds that, because the
property is the whole reason this package exists and it would erode one convenient import at a
time.

The one exception is spelled out where it lives: ``notify`` reads the configured notify-level
vocabulary from ``db_ops.config``, lazily and failing open, because the vocabulary is data an
operator adds by registering a Telegram group. ``db_ops.config`` is a root module, not a
component, so this does not point the layer at anything above it.
"""
