"""Rules about a restore that every component may use - today, the moment one is asked for.

:mod:`.moment` reads a point in time into the server's clock, for the restore steps, the backup
listings and the workflow alike.

A ``RestoreSpec`` (``spec``, ``plan``, ``pitr``) lived here too until 0.24.0: it described the work
``common.cli restore-database`` did, a third route for a SQL Server restore beside the SMB restore
and ``restore-by-id``. That command went (rules R43, the operator's choice) and the spec with it.
"""
