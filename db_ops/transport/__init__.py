"""Transport (ORD 15): the one client of ``common.cli`` and ``db.cli``. See docs/15_transport.md.

``lib`` builds the command (``lib.common_cli.build_command``), ``transport`` starts it
(:func:`db_ops.transport.process.execute`), ``lib`` reads the answer. It imports only ``lib``
(rules R39), and ``lib`` and ``common`` never import it.
"""
