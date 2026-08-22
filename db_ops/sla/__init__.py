"""SLA/SLI/SLO compliance for db_ops.

The SLA app is pure post-processing: it never connects to a monitored database. Its only
input is the ``metric_results`` table produced by the metrics app (the SLI is computed as
the fraction of "good" samples in a time window), and its only output is the ``sla_runs`` /
``sla_results`` tables it writes back to the shared SQLite store.
"""
