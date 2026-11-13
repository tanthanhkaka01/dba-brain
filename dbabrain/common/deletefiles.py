"""Gone since 0.38.0 (1.329): the path-list delete of backup files.

``delete_file`` / ``delete_files`` removed a path, or a list of paths, with one ``rm`` each and
nothing on the host held to anything. A backup file is now deleted only by
:mod:`dbabrain.common.deleteverified`:

* ``delete-verified-file`` / ``delete-verified-files`` - a file dbabrain recorded, held to its name,
  path, bytes and sha256;
* ``delete-file`` with ``"force": true`` - ONE stray file nobody recorded, measured on the host in
  the script that deletes it (:func:`dbabrain.common.deleteverified.delete_forced_file`);
* ``delete-empty-folder`` / ``delete-empty-folders`` - a folder that holds nothing at all.

This module is kept as the place that says so; it holds no code that deletes.
"""
