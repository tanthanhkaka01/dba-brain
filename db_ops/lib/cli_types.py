"""Every ``common.cli`` request and answer as a ``TypedDict`` - GENERATED, do not edit.

Rendered from ``shared_config_objects.json`` by ``db_ops.lib.request_types.render`` and held to it by
``tests/test_cli_types_match_the_reference.py``. Change the reference, then write this file again
with ``python -m db_ops.control.cli request-types --write``.

``<Command>Request`` is a command's request (an ``input_*`` entry), ``<Command>Answer`` what it
answers under ``data`` (an ``output_*`` entry). Every key is optional unless the reference says the
field is required; a field the reference says may be null is ``... | None``.
"""

from __future__ import annotations

from typing import Any, Required, TypedDict



#: A gated operation's report: every gate, what blocked it, and the facts it decided from. (authorize, host-facts, host-service, host-restart, shrink-log, kill-spid, start-job, disable-job, sqlserver-precheck, sqlserver-apply-cu, sqlserver-verify-build, sqlserver-export-instance, sqlserver-replay-instance, sqlserver-verify-instance)
GateReportAnswer = TypedDict('GateReportAnswer', {
    'ok': bool | None,
    'operation': str | None,
    'target': str | None,
    'run_id': str | None,
    'started_at': str | None,
    'status': str | None,
    'counts': dict[str, Any] | None,
    'overrides': list[Any] | None,
    'blockers': list[Any] | None,
    'gates': list[Any] | None,
    'facts': dict[str, Any] | None,
    'evidence_file': str | None,
    'local_elapsed_minutes': float | None,
}, total=False)


#: What the terminal answered, or that there was nobody to ask. (ask)
AskAnswer = TypedDict('AskAnswer', {
    'interactive': bool | None,
    'answer': str | None,
    'source': str | None,
}, total=False)


#: One gate of a gated operation.
GateAnswer = TypedDict('GateAnswer', {
    'name': str | None,
    'status': str | None,
    'detail': str | None,
    'blocking': bool | None,
    'override': str | None,
    'data': dict[str, Any] | None,
}, total=False)


#: One file fetched from or sent to a host. (fetch-file, send-file)
FileCopyAnswer = TypedDict('FileCopyAnswer', {
    'ok': bool | None,
    'direction': str | None,
    'status': str | None,
    'server_id': str | None,
    'host': str | None,
    'remote_path': str | None,
    'local_path': str | None,
    'size_bytes': int | None,
    'duration_ms': int | None,
    'replace_mode': str | None,
}, total=False)


#: An archive packed on a host (or here) from named files. (pack-files)
FilePackAnswer = TypedDict('FilePackAnswer', {
    'ok': bool | None,
    'packed_on': str | None,
    'server_id': str | None,
    'archive_path': str | None,
    'format': str | None,
    'size_bytes': int | None,
    'sha256': str | None,
    'file_count': int | None,
    'files': list[Any] | None,
    'duration_ms': int | None,
}, total=False)


#: One file moved from one host to another through this node. (relay-file)
FileRelayAnswer = TypedDict('FileRelayAnswer', {
    'ok': bool | None,
    'direction': str | None,
    'status': str | None,
    'source': dict[str, Any] | None,
    'destination': dict[str, Any] | None,
    'size_bytes': int | None,
    'sha256': str | None,
    'verified': bool | None,
    'duration_ms': int | None,
    'replace_mode': str | None,
}, total=False)


#: A backup folder packed into one archive on its host. (pack-backup)
BackupPackAnswer = TypedDict('BackupPackAnswer', {
    'archive_path': str | None,
    'format': str | None,
    'sha256': str | None,
    'size_bytes': int | None,
    'file_count': int | None,
    'folder': str | None,
}, total=False)


#: One file pulled from or pushed to a host, with its checksum. (pull-file, push-file)
FilePullPushAnswer = TypedDict('FilePullPushAnswer', {
    'remote_path': str | None,
    'local_path': str | None,
    'sha256': str | None,
    'size_bytes': int | None,
    'verified': bool | None,
}, total=False)


#: The backup files a directory holds, one row each. (list-backup-files)
BackupFileListAnswer = TypedDict('BackupFileListAnswer', {
    'files': list[Any] | None,
    'counts': dict[str, Any] | None,
    'newest_finished_at': str | None,
    'unreadable': list[Any] | None,
}, total=False)


#: One backup file.
BackupFileAnswer = TypedDict('BackupFileAnswer', {
    'path': str | None,
    'kind': str | None,
    'database_name': str | None,
    'size_bytes': int | None,
    'finished_at': str | None,
    'finished_at_utc': str | None,
    'age_seconds': int | None,
}, total=False)


#: Which backup files a retention rule keeps and which it would delete. (prune-backup-files)
BackupPruneAnswer = TypedDict('BackupPruneAnswer', {
    'mode': str | None,
    'retention_days': int | None,
    'retention_seconds': int | None,
    'window': str | None,
    'cutoff': str | None,
    'keep': list[Any] | None,
    'obsolete': list[Any] | None,
    'obsolete_paths': list[Any] | None,
    'counts': dict[str, Any] | None,
    'total': int | None,
    'reclaimable_bytes': int | None,
    'sizes_known': bool | None,
    'deleted': dict[str, Any] | None,
}, total=False)


#: Files deleted on a host, one row each, and what it freed. (delete-file, delete-files)
FileDeleteAnswer = TypedDict('FileDeleteAnswer', {
    'file': dict[str, Any] | None,
    'files': list[Any] | None,
    'counts': dict[str, Any] | None,
    'freed_bytes': int | None,
    'failed': list[Any] | None,
    'dry_run': bool | None,
    'stopped_early': bool | None,
}, total=False)


#: The databases on one instance. (list-databases)
DatabaseListAnswer = TypedDict('DatabaseListAnswer', {
    'server_id': str | None,
    'db_type': str | None,
    'ip': str | None,
    'port': int | None,
    'credential_name': str | None,
    'username': str | None,
    'databases': list[Any] | None,
    'count': int | None,
    'system_hidden': int | None,
    'container_type': str | None,
    'note': str | None,
}, total=False)


#: The schemas in one database. (list-schemas)
SchemaListAnswer = TypedDict('SchemaListAnswer', {
    'server_id': str | None,
    'db_type': str | None,
    'database_name': str | None,
    'credential_name': str | None,
    'username': str | None,
    'schemas': list[Any] | None,
    'count': int | None,
    'system_hidden': int | None,
    'note': str | None,
}, total=False)


#: The scheduled jobs on one instance. (list-jobs)
JobListAnswer = TypedDict('JobListAnswer', {
    'server_id': str | None,
    'db_type': str | None,
    'ip': str | None,
    'credential_name': str | None,
    'jobs': list[Any] | None,
    'count': int | None,
    'enabled_count': int | None,
    'disabled_hidden': int | None,
    'note': str | None,
}, total=False)


#: A spreadsheet loaded into a new table. (create-table-from-xlsx)
TableLoadAnswer = TypedDict('TableLoadAnswer', {
    'server_id': str | None,
    'db_type': str | None,
    'database_name': str | None,
    'schema': str | None,
    'table_name': str | None,
    'qualified_name': str | None,
    'credential_name': str | None,
    'username': str | None,
    'columns': list[Any] | None,
    'column_count': int | None,
    'column_type': str | None,
    'created': bool | None,
    'dropped_existing': bool | None,
    'rows_in_sheet': int | None,
    'rows_inserted': int | None,
    'sheet_truncated': bool | None,
    'source_format': str | None,
    'source_delimiter': str | None,
    'source_encoding': str | None,
    'ddl_transactional': bool | None,
    'duration_ms': int | None,
}, total=False)


#: What a config object's fields mean. (describe-object)
DescribeObjectAnswer = TypedDict('DescribeObjectAnswer', {
    'objects': list[Any] | None,
    'reference': str | None,
    'object': dict[str, Any] | None,
    'field': dict[str, Any] | None,
}, total=False)


#: Whether a schedule would run now, and why not. (due-check)
DueCheckAnswer = TypedDict('DueCheckAnswer', {
    'due': bool | None,
    'reason': str | None,
    'last_run': str | None,
    'next_due_at': str | None,
}, total=False)


#: This node's data/ held to the reference. (check-objects)
CheckObjectsAnswer = TypedDict('CheckObjectsAnswer', {
    'data_dir': str | None,
    'records_walked': int | None,
    'objects_checked': int | None,
    'violations': list[Any] | None,
    'notices': list[Any] | None,
    'deprecated': list[Any] | None,
    'unlisted': list[Any] | None,
    'files_missing': list[Any] | None,
    'ok': bool | None,
}, total=False)


#: Every pointer from one config file into another, and the ones that land nowhere. (check-references)
CheckReferencesAnswer = TypedDict('CheckReferencesAnswer', {
    'data_dir': str | None,
    'pointers_checked': int | None,
    'dangling': list[Any] | None,
    'inactive': list[Any] | None,
    'unreadable': list[Any] | None,
    'ok': bool | None,
}, total=False)


#: Config files moved to this version's shapes. (standardize-field-names, upgrade-config)
ConfigMigrationAnswer = TypedDict('ConfigMigrationAnswer', {
    'dry_run': bool | None,
    'data_dir': str | None,
    'files': list[Any] | None,
    'steps': list[Any] | None,
    'records_changed': int | None,
    'conflicts': int | None,
    'backup_dir': str | None,
    'backed_up': list[Any] | None,
    'check_objects': dict[str, Any] | None,
    'check_references': dict[str, Any] | None,
}, total=False)


#: One restore step on one engine: what it applied, or would. (restore-full, restore-diff, restore-log)
RestoreStepAnswer = TypedDict('RestoreStepAnswer', {
    'db_type': str | None,
    'level': str | None,
    'applied': list[Any] | None,
    'ran': list[Any] | None,
    'via': str | None,
    'exit_code': int | None,
    'stdout': str | None,
    'stderr': str | None,
    'timed_out': bool | None,
    'duration_ms': int | None,
    'dry_run': bool | None,
    'statements': list[Any] | None,
    'recovered': bool | None,
    'stopat': str | None,
    'mode': str | None,
    'steps': list[Any] | None,
    'scripts': list[Any] | None,
    'cataloged': list[Any] | None,
    'backup_location': str | None,
    'note': str | None,
    'action': str | None,
    'command': str | None,
    'container': str | None,
    'data_dir': str | None,
    'wrote': str | None,
    'plan': list[Any] | None,
    'recovery_target_time': str | None,
    'staged_into_container': bool | None,
}, total=False)


#: A backup-encryption certificate imported onto the target. (restore-key)
RestoreKeyAnswer = TypedDict('RestoreKeyAnswer', {
    'certificate_name': str | None,
    'thumbprint': str | None,
    'imported': bool | None,
    'statements': list[Any] | None,
    'dry_run': bool | None,
    'ok': bool | None,
}, total=False)


#: Server-level metadata replayed after a restore. (restore-metadata)
RestoreMetadataAnswer = TypedDict('RestoreMetadataAnswer', {
    'ok': bool | None,
    'files': list[Any] | None,
    'applied': list[Any] | None,
    'dry_run': bool | None,
}, total=False)


#: Whether the restored databases open, one row each. (verify-restore)
VerifyRestoreAnswer = TypedDict('VerifyRestoreAnswer', {
    'ok': bool | None,
    'databases': list[Any] | None,
    'checked': int | None,
    'failed': int | None,
}, total=False)


#: One backup run on a host. (backup-database)
BackupDatabaseAnswer = TypedDict('BackupDatabaseAnswer', {
    'status': str | None,
    'exit_code': int | None,
    'stdout': str | None,
    'stderr': str | None,
    'receipt': dict[str, Any] | None,
    'error': str | None,
    'dry_run': bool | None,
    'duration_ms': int | None,
    'db_type': str | None,
    'label': str | None,
    'level': str | None,
    'script': str | None,
    'script_lines': int | None,
    'host': str | None,
    'runtime': str | None,
    'container': str | None,
    'env_names': list[Any] | None,
    'timeout': int | None,
}, total=False)


#: What create-db-docker built, for the caller to register. (create-db-docker)
CreateDbDockerAnswer = TypedDict('CreateDbDockerAnswer', {
    'name': str | None,
    'engine': str | None,
    'version': str | None,
    'mode': str | None,
    'replicas': int | None,
    'host_port': int | None,
    'password_ref': str | None,
    'worker_host': str | None,
    'instance_dir': str | None,
    'compose_path': str | None,
    'backup_folder': str | None,
    'status': str | None,
    'healthy': bool | None,
    'statuses': dict[str, Any] | None,
    'docker': dict[str, Any] | None,
    'dry_run': bool | None,
    'plan_text': str | None,
    'summary': str | None,
}, total=False)


#: What move-db-docker moved, for the caller to repoint the registry. (move-db-docker)
MoveDbDockerAnswer = TypedDict('MoveDbDockerAnswer', {
    'ok': bool | None,
    'instance': dict[str, Any] | None,
    'engine': str | None,
    'source_host': str | None,
    'destination_host': str | None,
    'instance_dir': str | None,
    'compose_path': str | None,
    'statuses': dict[str, Any] | None,
    'bytes_transferred': int | None,
    'artifacts': list[Any] | None,
    'source_stopped': bool | None,
    'dry_run': bool | None,
    'plan_text': str | None,
    'summary': str | None,
}, total=False)


#: One target's metric items, run one after another - the metrics app's execution. (metric-batch)
MetricBatchRequest = TypedDict('MetricBatchRequest', {
    'target': Required[dict[str, Any] | None],
    'secrets': dict[str, Any] | None,
    'items': Required[list[dict[str, Any]] | None],
}, total=False)


#: What each metric item returned - rows, a script's streams, or its error. (metric-batch)
MetricBatchAnswer = TypedDict('MetricBatchAnswer', {
    'target_id': str | None,
    'items': list[dict[str, Any]] | None,
}, total=False)


#: How many targets were checked, and each one that does not resolve to a login. (check-credentials)
CheckCredentialsAnswer = TypedDict('CheckCredentialsAnswer', {
    'checked': int | None,
    'problems': list[str] | None,
}, total=False)


#: How one sqlcmd batch ran. (run-sqlcmd)
RunSqlcmdAnswer = TypedDict('RunSqlcmdAnswer', {
    'via': str | None,
    'exit_code': int | None,
    'stdout': str | None,
    'stderr': str | None,
    'timed_out': bool | None,
    'duration_ms': int | None,
}, total=False)


#: Which parts of a backup directory a restore needs. (backup-chain)
BackupChainRequest = TypedDict('BackupChainRequest', {
    'db_type': Required[str | None],
    'source': Required[dict[str, Any] | None],
    'source_dir': Required[str | None],
    'backup_dir': str | None,
    'container': str | None,
    'point_in_time': str | None,
}, total=False)


#: The path prefixes a restore's copy is narrowed to. (backup-chain)
BackupChainAnswer = TypedDict('BackupChainAnswer', {
    'include': list[str] | None,
    'narrowed': bool | None,
}, total=False)


#: A backup directory to copy from one host to another. (copy-backup-dir)
CopyBackupDirRequest = TypedDict('CopyBackupDirRequest', {
    'source': Required[dict[str, Any] | None],
    'source_dir': Required[str | None],
    'target': Required[dict[str, Any] | None],
    'target_dir': Required[str | None],
    'include': list[str] | None,
    'window_hours': float | None,
    'make_readable': bool | None,
    'open_for_engine': bool | None,
    'copy_mode': str | None,
    'space_check': dict[str, Any] | None,
}, total=False)


#: What one host-to-host copy moved. (copy-backup-dir)
CopyBackupDirAnswer = TypedDict('CopyBackupDirAnswer', {
    'copied': int | None,
    'skipped': int | None,
    'bytes_copied': int | None,
    'removed_absent_at_source': int | None,
    'opened_for_engine': bool | None,
    'copy_mode': str | None,
    'copy_fell_back': bool | None,
    'space_check': dict[str, Any] | None,
}, total=False)


#: A restore's staging folder to clear past its retention. (prune-staged-backups)
PruneStagedBackupsRequest = TypedDict('PruneStagedBackupsRequest', {
    'target': Required[dict[str, Any] | None],
    'target_dir': Required[str | None],
    'cleanup_retention': int | None,
}, total=False)


#: What one staging cleanup removed. (prune-staged-backups)
PruneStagedBackupsAnswer = TypedDict('PruneStagedBackupsAnswer', {
    'pruned': int | None,
    'retention_seconds': int | None,
    'skipped': str | None,
    'error': str | None,
}, total=False)


#: A record written by a registrar, and what to run next. (instance-add, remote-credential-add, sql-command-add, sql-target-add, add-sql)
RegistrationAnswer = TypedDict('RegistrationAnswer', {
    'ok': bool | None,
    'server_id': str | None,
    'db_type': str | None,
    'host': str | None,
    'credential_name': str | None,
    'username': str | None,
    'password_ref': str | None,
    'password_stored_encrypted': bool | None,
    'cmd_access_written': bool | None,
    'method': str | None,
    'port': int | None,
    'sql_id': int | None,
    'sql_code': str | None,
    'target_no': int | None,
    'script_type': str | None,
    'input_type': str | None,
    'script_path': str | None,
    'script_abs': str | None,
    'script_written': bool | None,
    'database_name': str | None,
    'active': bool | None,
    'manual_only': bool | None,
    'repeat_interval': int | None,
    'output': dict[str, Any] | None,
    'replaced': bool | None,
    'files_written': list[Any] | None,
    'next': list[Any] | None,
}, total=False)


#: A config record changed in place, field by field. (app-command-set, metric-toggle, metric-severity)
ConfigEditAnswer = TypedDict('ConfigEditAnswer', {
    'ok': bool | None,
    'app_code': str | None,
    'server_id': str | None,
    'scope': str | None,
    'enabled': bool | None,
    'metric_code': str | None,
    'metric_item': str | None,
    'severity_map': dict[str, Any] | None,
    'changed': bool | None,
    'changes': list[Any] | None,
    'written': bool | None,
    'file': str | None,
    'warnings': list[Any] | None,
    'message': str | None,
}, total=False)


#: One secret written into the encrypted store. (secret-set)
SecretSetAnswer = TypedDict('SecretSetAnswer', {
    'ref': str | None,
    'store': str | None,
    'written': bool | None,
    'plaintext_source': str | None,
}, total=False)


#: What a statement returned, and what it ran against. (run-sql)
RunSqlAnswer = TypedDict('RunSqlAnswer', {
    'ok': bool | None,
    'server_id': str | None,
    'database_name': str | None,
    'credential_name': str | None,
    'username': str | None,
    'target_profile': dict[str, Any] | None,
    'tool': dict[str, Any] | None,
    'columns': list[Any] | None,
    'rows': list[Any] | None,
    'row_count': int | None,
    'affected_rows': int | None,
    'truncated': bool | None,
    'result_sets': list[Any] | None,
    'result_sets_truncated': bool | None,
    'committed': bool | None,
    'warnings': list[Any] | None,
}, total=False)


#: What one shell command printed on a host. (run-cmd)
RunCmdAnswer = TypedDict('RunCmdAnswer', {
    'server_id': str | None,
    'host': str | None,
    'host_profile': dict[str, Any] | None,
    'shell': str | None,
    'shell_dialect': str | None,
    'method': str | None,
    'command': str | None,
    'exit_code': int | None,
    'stdout': str | None,
    'stderr': str | None,
    'duration_ms': int | None,
    'backend': str | None,
}, total=False)


#: The sessions holding transactions open on one instance. (trace-session)
TraceSessionAnswer = TypedDict('TraceSessionAnswer', {
    'ok': bool | None,
    'server_id': str | None,
    'database_name': str | None,
    'transaction_count': int | None,
    'session_count': int | None,
    'sessions': list[Any] | None,
}, total=False)


#: Whether an instance, its databases or its schemas answer. (db-status)
DbStatusAnswer = TypedDict('DbStatusAnswer', {
    'ok': bool | None,
    'depth': str | None,
    'db_type': str | None,
    'server_id': str | None,
    'instance': dict[str, Any] | None,
    'items': list[Any] | None,
    'checked': int | None,
    'failed': int | None,
}, total=False)


#: Which management ports a host answers on, and what that says it is. (probe-host)
ProbeHostAnswer = TypedDict('ProbeHostAnswer', {
    'server_id': str | None,
    'host': str | None,
    'profile': dict[str, Any] | None,
    'verdict': str | None,
    'detail': str | None,
    'management_ports': list[Any] | None,
    'open_ports': list[Any] | None,
    'ports': list[Any] | None,
}, total=False)


#: Every target this node can address. (list-targets)
TargetListAnswer = TypedDict('TargetListAnswer', {
    'targets': list[Any] | None,
}, total=False)


#: The estate's inventory, summarised. (inventory-summary)
InventorySummaryAnswer = TypedDict('InventorySummaryAnswer', {
    'inventory': dict[str, Any] | None,
    'file': str | None,
}, total=False)


#: What this node is and what it runs. (self-status)
SelfStatusAnswer = TypedDict('SelfStatusAnswer', {
    'listing': str | None,
    'version': str | None,
    'public_version': str | None,
    'python': str | None,
    'timezone': str | None,
    'utc_offset': str | None,
    'tz_abbreviation': str | None,
    'distribution': dict[str, Any] | None,
    'runtime': str | None,
    'os': str | None,
    'tool_root': str | None,
    'node_role': str | None,
    'store': dict[str, Any] | None,
    'host': dict[str, Any] | None,
    'web': dict[str, Any] | None,
    'apps': dict[str, Any] | None,
    'cpu': dict[str, Any] | None,
    'memory': dict[str, Any] | None,
    'disk': dict[str, Any] | None,
    'uptime': dict[str, Any] | None,
    'db_ops_uptime': dict[str, Any] | None,
    'pid': int | None,
}, total=False)


#: Secret refs proved by using them. (check-secret, rotate-password)
SecretCheckAnswer = TypedDict('SecretCheckAnswer', {
    'ok': bool | None,
    'selected': list[Any] | None,
    'summary': dict[str, Any] | None,
    'results': list[Any] | None,
}, total=False)


#: Where estate identifiers or secret literals appear in a tree. (check-identifiers, check-secret-literals)
IdentifierScanAnswer = TypedDict('IdentifierScanAnswer', {
    'root': str | None,
    'paths': list[Any] | None,
    'identifiers_searched': int | None,
    'spellings_searched': int | None,
    'files_scanned': int | None,
    'files_with_findings': int | None,
    'hits': int | None,
    'certain': list[Any] | None,
    'likely': list[Any] | None,
    'review': list[Any] | None,
    'review_only_files': list[Any] | None,
    'unrecognised_addresses': list[Any] | None,
    'allowed': list[Any] | None,
    'files': list[Any] | None,
    'refused': bool | None,
    'secrets_searched': int | None,
    'findings': list[Any] | None,
    'store': str | None,
}, total=False)


#: A config file copied into an example, with the estate taken out. (lift-example)
ExampleLiftAnswer = TypedDict('ExampleLiftAnswer', {
    'source': str | None,
    'destination': str | None,
    'records': int | None,
    'referenced_files': list[Any] | None,
    'blanked': list[Any] | None,
    'identifier_hits': int | None,
    'terms': list[Any] | None,
    'written': bool | None,
    'message': str | None,
}, total=False)


#: The published pages rebuilt with every estate name replaced. (build-showcase)
ShowcaseAnswer = TypedDict('ShowcaseAnswer', {
    'source': str | None,
    'output': str | None,
    'pages': list[Any] | None,
    'files': list[Any] | None,
    'terms': dict[str, Any] | None,
    'days_covered': int | None,
    'kept_node_naming': bool | None,
    'left_as_ordinary_words': list[Any] | None,
    'unmapped_kinds': list[Any] | None,
    'allowed': list[Any] | None,
    'verified': bool | None,
    'findings': list[Any] | None,
}, total=False)


#: A schema copied from one database into another, or the plan to. (copy-schema)
CopySchemaAnswer = TypedDict('CopySchemaAnswer', {
    'mode': str | None,
    'plan': dict[str, Any] | None,
    'destination': str | None,
    'applied': dict[str, Any] | None,
    'verification': dict[str, Any] | None,
    'message': str | None,
    'duration_ms': int | None,
}, total=False)


#: A confirmation asked for on its own, for a caller that does the work itself. (authorize)
AuthorizeRequest = TypedDict('AuthorizeRequest', {
    'operation': Required[str | None],
    'target_id': str | None,
    'target_label': str | None,
    'effects': list[Any] | None,
    'confirm': bool | None,
    'assume_yes': bool | None,
    'authorized_by': str | None,
    'reason': str | None,
    'rules': dict[str, Any] | None,
}, total=False)


#: One question for the controlling terminal, and the answers it takes. (ask)
AskRequest = TypedDict('AskRequest', {
    'prompt': Required[str | None],
    'choices': list[Any] | None,
    'tries': int | None,
    'deadline_seconds': float | None,
    'answer': str | None,
}, total=False)


#: A gated action on a host: facts, a service, a restart. (host-facts, host-service, host-restart)
HostOperationRequest = TypedDict('HostOperationRequest', {
    'target': str | None,
    'access': dict[str, Any] | None,
    'platform': str | None,
    'action': str | None,
    'services': list[Any] | None,
    'wait': bool | None,
    'window': dict[str, Any] | None,
    'ignore_window': bool | None,
    'overrides': list[Any] | None,
    'evidence': str | None,
    'sudo': bool | None,
    'dry_run': bool | None,
    'confirm': bool | None,
    'assume_yes': bool | None,
    'authorized_by': str | None,
    'reason': str | None,
    'policy': dict[str, Any] | None,
    'rules': dict[str, Any] | None,
}, total=False)


#: A break-glass SQL Server action: shrink a log, kill a session, start or disable a job. (shrink-log, kill-spid, start-job, disable-job)
SqlserverEmergencyRequest = TypedDict('SqlserverEmergencyRequest', {
    'target': str | None,
    'server_id': str | None,
    'sql_access': dict[str, Any] | None,
    'database_name': str | None,
    'size_mb': float | None,
    'spid': int | None,
    'report_rollback': bool | None,
    'job_name': str | None,
    'override': bool | None,
    'timeout_seconds': int | None,
    'dry_run': bool | None,
    'confirm': bool | None,
    'assume_yes': bool | None,
    'authorized_by': str | None,
    'reason': str | None,
    'connection': dict[str, Any] | None,
    'rules': dict[str, Any] | None,
}, total=False)


#: A cumulative update checked, applied or verified on one instance. (sqlserver-precheck, sqlserver-apply-cu, sqlserver-verify-build)
SqlserverPatchRequest = TypedDict('SqlserverPatchRequest', {
    'target': str | None,
    'credential_name': str | None,
    'instance_name': str | None,
    'kb': str | None,
    'installer': str | None,
    'installer_sha256': str | None,
    'skip_hash': bool | None,
    'expected_build': str | None,
    'registry_key': str | None,
    'setup_account': str | None,
    'window': dict[str, Any] | None,
    'ignore_window': bool | None,
    'dry_run': bool | None,
    'confirm': bool | None,
    'assume_yes': bool | None,
    'authorized_by': str | None,
    'reason': str | None,
    'connection': dict[str, Any] | None,
    'access': dict[str, Any] | None,
    'policy': dict[str, Any] | None,
    'rules': dict[str, Any] | None,
}, total=False)


#: An instance's server-level metadata exported, replayed or verified. (sqlserver-export-instance, sqlserver-replay-instance, sqlserver-verify-instance)
SqlserverInstanceRequest = TypedDict('SqlserverInstanceRequest', {
    'target': str | None,
    'credential_name': str | None,
    'output_dir': str | None,
    'bundle_dir': str | None,
    'include': list[Any] | None,
    'phase': str | None,
    'overrides': dict[str, Any] | None,
    'secret_prefix': str | None,
    'on_missing_secret': str | None,
    'on_unsupported': str | None,
    'dry_run': bool | None,
    'confirm': bool | None,
    'assume_yes': bool | None,
    'authorized_by': str | None,
    'reason': str | None,
    'connection': dict[str, Any] | None,
    'policy': dict[str, Any] | None,
    'secrets': dict[str, Any] | None,
    'rules': dict[str, Any] | None,
}, total=False)


#: A file fetched, sent, packed or relayed over the host's own access. (fetch-file, send-file, pack-files, relay-file)
FileTransferRequest = TypedDict('FileTransferRequest', {
    'target': str | None,
    'server_id': str | None,
    'access': dict[str, Any] | None,
    'remote_path': str | None,
    'local_path': str | None,
    'overwrite': bool | None,
    'make_dirs': bool | None,
    'files': list[Any] | None,
    'folder': str | None,
    'include': list[Any] | None,
    'format': str | None,
    'archive_path': str | None,
    'checksum_files': bool | None,
    'source': dict[str, Any] | None,
    'destination': dict[str, Any] | None,
}, total=False)


#: A backup folder packed, or one file pulled or pushed, with checksums. (pack-backup, pull-file, push-file)
BackupPackRequest = TypedDict('BackupPackRequest', {
    'host': dict[str, Any] | None,
    'folder': str | None,
    'files': list[Any] | None,
    'archive_path': str | None,
    'format': str | None,
    'remote_path': str | None,
    'local_path': str | None,
    'sha256': str | None,
    'timeout_seconds': int | None,
}, total=False)


#: Which backup files a directory holds, or which a retention rule would remove. (list-backup-files, prune-backup-files)
BackupFilesRequest = TypedDict('BackupFilesRequest', {
    'db_type': Required[str | None],
    'host': dict[str, Any] | None,
    'target': dict[str, Any] | None,
    'path': Required[str | None],
    'database_name': str | None,
    'kinds': list[Any] | None,
    'latest': bool | None,
    'after': str | None,
    'before': str | None,
    'mode': str | None,
    'cleanup_retention': int | None,
    'delete': bool | None,
    'dry_run': bool | None,
    'timeout_seconds': int | None,
}, total=False)


#: Files deleted on a host, never outside a named folder. (delete-file, delete-files)
FileDeleteRequest = TypedDict('FileDeleteRequest', {
    'host': dict[str, Any] | None,
    'path': str | None,
    'paths': list[Any] | None,
    'must_be_under': Required[str | None],
    'stop_on_error': bool | None,
    'dry_run': bool | None,
}, total=False)


#: One instance's databases, schemas or jobs listed. (list-databases, list-schemas, list-jobs)
CatalogRequest = TypedDict('CatalogRequest', {
    'target': str | None,
    'credential_name': str | None,
    'sql_access': dict[str, Any] | None,
    'database_name': str | None,
    'include_system': bool | None,
    'enabled_only': bool | None,
    'timeout_seconds': int | None,
    'key': str | None,
    'key_base64': str | None,
    'connection': dict[str, Any] | None,
}, total=False)


#: A spreadsheet or CSV loaded into a new table. (create-table-from-xlsx)
TableLoadRequest = TypedDict('TableLoadRequest', {
    'target': str | None,
    'credential_name': str | None,
    'sql_access': dict[str, Any] | None,
    'database_name': str | None,
    'schema': str | None,
    'table_name': str | None,
    'file_path': str | None,
    'file_base64': str | None,
    'delimiter': str | None,
    'if_exists': str | None,
    'load_rows': bool | None,
    'max_rows': int | None,
    'text_length': int | None,
    'timeout_seconds': int | None,
    'connection': dict[str, Any] | None,
}, total=False)


#: Which object, or which field of it, to describe. (describe-object)
DescribeObjectRequest = TypedDict('DescribeObjectRequest', {
    'object': str | None,
    'field': str | None,
}, total=False)


#: A schedule and its last run, asked whether it is due. (due-check)
DueCheckRequest = TypedDict('DueCheckRequest', {
    'time_window': dict[str, Any] | None,
    'row': dict[str, Any] | None,
    'last_run': str | None,
    'last_status': str | None,
    'now': str | None,
    'local_now': str | None,
    'retry_default': int | None,
    'timeout_default': int | None,
    'default_repeat': int | None,
}, total=False)


#: Which data/ folder to check - against the reference, or for logins that resolve. (check-objects, check-references, check-credentials)
ConfigCheckRequest = TypedDict('ConfigCheckRequest', {
    'data_dir': str | None,
    'format': str | None,
}, total=False)


#: Which folder to move to this version's shapes, and whether to write. (standardize-field-names, upgrade-config)
ConfigMigrationRequest = TypedDict('ConfigMigrationRequest', {
    'data_dir': str | None,
    'dry_run': bool | None,
    'file': str | None,
    'steps': list[Any] | None,
}, total=False)


#: One restore step on one engine. (restore-full, restore-diff, restore-log)
RestoreStepRequest = TypedDict('RestoreStepRequest', {
    'db_type': Required[str | None],
    'target': dict[str, Any] | None,
    'sqlcmd': dict[str, Any] | None,
    'backup_path': str | None,
    'backup_paths': list[Any] | None,
    'database_name': str | None,
    'move': dict[str, Any] | None,
    'move_files': dict[str, Any] | None,
    'replace': bool | None,
    'overwrite_existing': bool,
    'stats': int | None,
    'with_recovery': bool | None,
    'stopat': str | None,
    'host': dict[str, Any] | None,
    'backup_location': str | None,
    'encryption_password': str | None,
    'mode': str | None,
    'oracle_sid': str | None,
    'bin_dir': str | None,
    'data_dir': str | None,
    'run_as': str | None,
    'staging_dir': str | None,
    'wal_dir': str | None,
    'dry_run': bool | None,
    'timeout_seconds': int | None,
    'recovery_timeout_seconds': int | None,
}, total=False)


#: A host a restore step reaches, given inline.
RemoteTargetRequest = TypedDict('RemoteTargetRequest', {
    'host': str | None,
    'port': int | None,
    'username': str | None,
    'password': str | None,
    'key_file': str | None,
    'runtime': str | None,
    'container': str | None,
    'sudo': bool | None,
}, total=False)


#: A backup-encryption certificate to import. (restore-key)
RestoreKeyRequest = TypedDict('RestoreKeyRequest', {
    'target': dict[str, Any] | None,
    'sqlcmd': dict[str, Any] | None,
    'certificate_name': str | None,
    'cer_path': str | None,
    'pvk_path': str | None,
    'password': str | None,
    'dry_run': bool | None,
}, total=False)


#: Server-level metadata files to replay after a restore. (restore-metadata)
RestoreMetadataRequest = TypedDict('RestoreMetadataRequest', {
    'target': dict[str, Any] | None,
    'host': dict[str, Any] | None,
    'files': list[Any] | None,
    'dry_run': bool | None,
}, total=False)


#: Which restored databases to ask about, and where. (verify-restore)
VerifyRestoreRequest = TypedDict('VerifyRestoreRequest', {
    'db_type': Required[str | None],
    'target': dict[str, Any] | None,
    'host': dict[str, Any] | None,
    'port': int | None,
    'username': str | None,
    'database_names': list[Any] | None,
    'database_name': str | None,
    'oracle_sid': str | None,
    'timeout_seconds': int | None,
}, total=False)


#: One backup run on a host. (backup-database)
BackupDatabaseRequest = TypedDict('BackupDatabaseRequest', {
    'db_type': Required[str | None],
    'host': dict[str, Any] | None,
    'label': str | None,
    'level': str | None,
    'script': str | None,
    'script_path': str | None,
    'env': dict[str, Any] | None,
    'server_metadata': dict[str, Any] | None,
    'timeout': int | None,
    'dry_run': bool | None,
}, total=False)


#: One lab database Docker instance to build. (create-db-docker)
CreateDbDockerRequest = TypedDict('CreateDbDockerRequest', {
    'name': Required[str | None],
    'engine': Required[str | None],
    'version': Required[str | None],
    'mode': str | None,
    'replicas': int | None,
    'host_port': int | None,
    'password_ref': str | None,
    'password': str | None,
    'backup_mount': str | None,
    'network_subnet': str | None,
    'containers_dir': str | None,
    'worker_host': str | None,
    'remote': dict[str, Any] | None,
    'install_docker': bool | None,
    'sudo_password': str | None,
    'health_timeout': int | None,
    'force': bool | None,
    'dry_run': bool | None,
}, total=False)


#: One lab database Docker instance to move, data included. (move-db-docker)
MoveDbDockerRequest = TypedDict('MoveDbDockerRequest', {
    'name': Required[str | None],
    'engine': Required[str | None],
    'source': Required[dict[str, Any] | None],
    'destination': Required[dict[str, Any] | None],
    'containers_dir': str | None,
    'dest_containers_dir': str | None,
    'stage_dir': str | None,
    'include_volumes': bool | None,
    'commit_container': bool | None,
    'stop_source': bool | None,
    'keep_stage': bool | None,
    'force': bool | None,
    'health_timeout': int | None,
    'dry_run': bool | None,
}, total=False)


#: One sqlcmd batch, and where to run it. (run-sqlcmd)
RunSqlcmdRequest = TypedDict('RunSqlcmdRequest', {
    'sql': Required[str | None],
    'instance': Required[str | None],
    'sqlcmd_path': str | None,
    'container': str | None,
    'username': str | None,
    'password': str | None,
    'login_timeout_seconds': int | None,
    'query_timeout_seconds': int | None,
    'timeout_seconds': int | None,
    'via': str | None,
    'host': dict[str, Any] | None,
}, total=False)


#: A Windows share and the login to it - what to list, fetch, delete, or store the login for. (smb-list, smb-get, smb-delete, smb-credential)
SmbRequest = TypedDict('SmbRequest', {
    'host': str | None,
    'share': str | None,
    'username': str | None,
    'password': str | None,
    'domain': str | None,
    'backend': str | None,
    'timeout_seconds': int | None,
    'path': str | None,
    'recurse': bool | None,
    'suffixes': list[Any] | None,
    'remote_path': str | None,
    'local_path': str | None,
    'paths': list[Any] | None,
    'target': str | None,
}, total=False)


#: The files under one folder of a share. (smb-list)
SmbListAnswer = TypedDict('SmbListAnswer', {
    'backend': str | None,
    'host': str | None,
    'share': str | None,
    'path': str | None,
    'files': list[Any] | None,
    'count': int | None,
}, total=False)


#: One file fetched from a share. (smb-get)
SmbGetAnswer = TypedDict('SmbGetAnswer', {
    'backend': str | None,
    'remote_path': str | None,
    'local_path': str | None,
    'bytes': int | None,
    'exit_code': int | None,
    'detail': str | None,
}, total=False)


#: Named files deleted on a share. (smb-delete)
SmbDeleteAnswer = TypedDict('SmbDeleteAnswer', {
    'backend': str | None,
    'host': str | None,
    'share': str | None,
    'results': list[Any] | None,
    'deleted': int | None,
    'failed': int | None,
}, total=False)


#: A share login stored for Windows. (smb-credential)
SmbCredentialAnswer = TypedDict('SmbCredentialAnswer', {
    'registered': bool | None,
    'target': str | None,
    'reason': str | None,
}, total=False)


#: An instance to register, with its login. (instance-add)
InstanceAddRequest = TypedDict('InstanceAddRequest', {
    'server_id': Required[str | None],
    'db_type': Required[str | None],
    'ip': Required[str | None],
    'port': int | None,
    'service_name': str | None,
    'database_name': str | None,
    'username': str | None,
    'password': str | None,
    'password_ref': str | None,
    'credential_name': str | None,
    'role': str | None,
    'replace': bool | None,
    'active': bool | None,
    'keep_default': bool | None,
}, total=False)


#: An OS login to register, and the cmd_access that names it. (remote-credential-add)
RemoteCredentialAddRequest = TypedDict('RemoteCredentialAddRequest', {
    'server_id': Required[str | None],
    'host': str | None,
    'server_description': str | None,
    'username': Required[str | None],
    'password': str | None,
    'password_ref': str | None,
    'auth_type': str | None,
    'key_file': str | None,
    'credential_name': str | None,
    'role': str | None,
    'account_owner': str | None,
    'note': str | None,
    'method': str | None,
    'platform': str | None,
    'port': int | None,
    'shell': str | None,
    'ssl': bool | None,
    'enabled': bool | None,
    'replace': bool | None,
}, total=False)


#: A SQL task's WHAT: its name and its script. (sql-command-add)
SqlCommandAddRequest = TypedDict('SqlCommandAddRequest', {
    'display_name': Required[str | None],
    'db_type': Required[str | None],
    'sql_id': int | None,
    'sql_code': str | None,
    'script_type': str | None,
    'script_path': str | None,
    'script_paths': list[Any] | None,
    'final_script_paths': list[Any] | None,
    'sql_text': str | None,
    'version_from': int | None,
    'version_to': int | None,
    'parameters': list[Any] | None,
    'autocommit': bool | None,
    'progress_per_file': bool | None,
    'input_type': str | None,
    'input': dict[str, Any] | None,
    'active': bool | None,
    'note': str | None,
    'replace': bool | None,
}, total=False)


#: A SQL task's WHERE and WHEN. (sql-target-add)
SqlTargetAddRequest = TypedDict('SqlTargetAddRequest', {
    'sql_id': Required[int | None],
    'server_id': Required[str | None],
    'target_no': int | None,
    'instance_name': str | None,
    'service_name': str | None,
    'database_name': str | None,
    'credential_name': str | None,
    'sql_access': dict[str, Any] | None,
    'time_window': dict[str, Any] | None,
    'manual_only': bool | None,
    'notify': dict[str, Any] | None,
    'logging_on_run': dict[str, Any] | None,
    'alert_on_error': dict[str, Any] | None,
    'logging_chat': str | None,
    'logging_chat_id': str | None,
    'error_chat': str | None,
    'error_chat_id': str | None,
    'output': dict[str, Any] | None,
    'output_chat': str | None,
    'output_chat_id': str | None,
    'output_max_rows': int | None,
    'active': bool | None,
    'note': str | None,
    'replace': bool | None,
}, total=False)


#: A single-script SQL task and its target in one call (the older add-sql). (add-sql)
AddSqlRequest = TypedDict('AddSqlRequest', {
    'db_type': Required[str | None],
    'server_id': Required[str | None],
    'display_name': Required[str | None],
    'sql_text': str | None,
    'sql_file': str | None,
    'instance_name': str | None,
    'service_name': str | None,
    'database_name': str | None,
    'credential_name': str | None,
    'from_day': int | None,
    'to_day': int | None,
    'from_hour': int | None,
    'to_hour': int | None,
    'repeat_interval': int | None,
    'timeout': int | None,
    'inactive': bool | None,
    'manual_only': bool | None,
    'output': str | None,
    'output_chat': str | None,
    'notify_chat': str | None,
    'logging_chat': str | None,
    'error_chat': str | None,
    'logging_chat_id': str | None,
    'error_chat_id': str | None,
    'logging_on_run': bool | None,
    'alert_on_error': bool | None,
    'data_dir': str | None,
}, total=False)


#: A metric, a collector type or all of them switched on or off for one instance. (metric-toggle)
MetricToggleRequest = TypedDict('MetricToggleRequest', {
    'server_id': Required[str | None],
    'scope': Required[str | None],
    'state': Required[str | None],
    'data_dir': str | None,
}, total=False)


#: How one metric's statuses are remapped for one instance. (metric-severity)
MetricSeverityRequest = TypedDict('MetricSeverityRequest', {
    'server_id': Required[str | None],
    'metric_code': Required[str | None],
    'metric_item': str | None,
    'severity_map': dict[str, Any] | None,
    'note': str | None,
    'data_dir': str | None,
}, total=False)


#: An app command's schedule or state, changed field by field. (app-command-set)
AppCommandSetRequest = TypedDict('AppCommandSetRequest', {
    'app_code': Required[str | None],
    'active': bool | None,
    'node_role': str | None,
    'run_mode': str | None,
    'max_parallel': int | None,
    'time_window': dict[str, Any] | None,
    'sort_order': int | None,
    'display_name': str | None,
    'note': str | None,
    'data_dir': str | None,
}, total=False)


#: One secret to encrypt into the store. Stdin only: never on a command line. (secret-set)
SecretSetRequest = TypedDict('SecretSetRequest', {
    'ref': Required[str | None],
    'value': Required[str | None],
    'overwrite': bool | None,
    'also_plaintext': bool | None,
}, total=False)


#: A statement to run on one target, and how. (run-sql)
RunSqlRequest = TypedDict('RunSqlRequest', {
    'target': str | None,
    'sql_text': str | None,
    'sql_file': str | None,
    'database_name': str | None,
    'sql_access': dict[str, Any] | None,
    'secrets': dict[str, Any] | None,
    'connection': dict[str, Any] | None,
    'profile': dict[str, Any] | None,
    'driver': str | None,
    'oracle_client_mode': str | None,
    'params': list[Any] | None,
    'define': dict[str, Any] | None,
    'defines': dict[str, Any] | None,
    'prelude': list[Any] | None,
    'named_params': dict[str, Any] | None,
    'autocommit': bool | None,
    'commit': bool | None,
    'capture': str | None,
    'max_rows': int | None,
    'max_result_sets': int | None,
    'connect_timeout_seconds': int | None,
    'timeout_seconds': int | None,
    'output_path': str | None,
    'format': str | None,
}, total=False)


#: One shell command on a host. (run-cmd)
RunCmdRequest = TypedDict('RunCmdRequest', {
    'target': str | None,
    'access': dict[str, Any] | None,
    'platform': str | None,
    'profile': dict[str, Any] | None,
    'command': str | None,
    'script': str | None,
    'sudo': bool | None,
    'timeout_seconds': int | None,
    'format': str | None,
    'confirm': bool | None,
    'assume_yes': bool | None,
    'authorized_by': str | None,
    'reason': str | None,
}, total=False)


#: Which sessions to trace on one instance. (trace-session)
TraceSessionRequest = TypedDict('TraceSessionRequest', {
    'target': str | None,
    'session_id': int | None,
    'blocking_only': bool | None,
    'min_tran_seconds': int | None,
    'key': str | None,
    'key_base64': str | None,
    'connection': dict[str, Any] | None,
}, total=False)


#: How deep to check an instance. (db-status)
DbStatusRequest = TypedDict('DbStatusRequest', {
    'target': str | None,
    'depth': str | None,
    'database_name': str | None,
    'database_names': list[Any] | None,
    'connection': dict[str, Any] | None,
}, total=False)


#: A host to probe for its management ports. (probe-host)
ProbeHostRequest = TypedDict('ProbeHostRequest', {
    'target': str | None,
    'host': str | None,
    'ports': list[Any] | None,
    'timeout_seconds': int | None,
}, total=False)


#: A listing that takes nothing but its format. (list-targets)
ListingRequest = TypedDict('ListingRequest', {
    'format': str | None,
}, total=False)


#: What this installation is - and, stated by the caller, when each app last ran. (self-status)
SelfStatusRequest = TypedDict('SelfStatusRequest', {
    'format': str | None,
    'last_runs': dict[str, Any] | None,
    'store_error': str | None,
}, total=False)


#: The inventory to summarise. (inventory-summary)
InventorySummaryRequest = TypedDict('InventorySummaryRequest', {
    'inventory': str | None,
    'overlay': str | None,
    'app_commands': str | None,
    'date': str | None,
    'output_dir': str | None,
}, total=False)


#: Which secret refs to prove by using them. (check-secret)
CheckSecretRequest = TypedDict('CheckSecretRequest', {
    'refs': list[Any] | None,
    'match': str | None,
    'allow_name_host': bool | None,
    'timeout_seconds': int | None,
}, total=False)


#: Which secret refs to rotate, and how. (rotate-password)
RotatePasswordRequest = TypedDict('RotatePasswordRequest', {
    'refs': list[Any] | None,
    'match': str | None,
    'allow_name_host': bool | None,
    'host_overrides': dict[str, Any] | None,
    'password_length': int | None,
    'passwords': dict[str, Any] | None,
    'plaintext_store': str | None,
    'dry_run': bool | None,
    'timeout_seconds': int | None,
}, total=False)


#: A tree to scan for this estate's identifiers. (check-identifiers)
IdentifierScanRequest = TypedDict('IdentifierScanRequest', {
    'root': str | None,
    'paths': list[Any] | None,
    'allow': list[Any] | None,
    'extensions': list[Any] | None,
    'extra_terms': list[Any] | None,
    'from_inventory': bool | None,
    'max_examples': int | None,
}, total=False)


#: A tree to scan for secret values. (check-secret-literals)
SecretLiteralsRequest = TypedDict('SecretLiteralsRequest', {
    'root': str | None,
    'paths': list[Any] | None,
    'store': str | None,
}, total=False)


#: A config file to copy into an example. (lift-example)
ExampleLiftRequest = TypedDict('ExampleLiftRequest', {
    'source': Required[str | None],
    'destination': str | None,
    'blank_keys': list[Any] | None,
    'write': bool | None,
}, total=False)


#: Where the published pages are, and where the showcase goes. (build-showcase)
ShowcaseRequest = TypedDict('ShowcaseRequest', {
    'source': str | None,
    'output': str | None,
    'pattern': str | None,
    'stamp': str | None,
    'allow': list[Any] | None,
    'extra_terms': list[Any] | None,
    'force': bool | None,
    'verify': bool | None,
}, total=False)


#: A schema to copy from one database into another. (copy-schema)
CopySchemaRequest = TypedDict('CopySchemaRequest', {
    'source': dict[str, Any] | None,
    'destination': dict[str, Any] | None,
    'mode': str | None,
    'tables': list[Any] | None,
    'include_tables': list[Any] | None,
    'exclude_tables': list[Any] | None,
    'modules': list[Any] | None,
    'include_modules': list[Any] | None,
    'exclude_modules': list[Any] | None,
    'with_data': bool | None,
    'batch_size': int | None,
    'create_schema': bool | None,
    'skip_nonempty_tables': bool | None,
    'map_filegroups': dict[str, Any] | None,
    'partition_boundaries': bool | None,
    'module_passes': int | None,
    'phases': list[Any] | None,
    'plan': dict[str, Any] | None,
    'verify': bool | None,
    'report_unsupported': bool | None,
    'assert_dest_instance': str | None,
    'lock_name': str | None,
    'lock_timeout_seconds': int | None,
    'target': str | None,
    'schema': str | None,
    'database_name': str | None,
    'timeout_seconds': int | None,
    'key': str | None,
    'key_base64': str | None,
}, total=False)


#: Create a tool root: the directory, the app name, and whether to overwrite. (init)
InitRequest = TypedDict('InitRequest', {
    'root': str | None,
    'app_name': str | None,
    'force': bool | None,
    'format': str | None,
}, total=False)


#: What init wrote, what it kept, and what to do next. (init)
InitAnswer = TypedDict('InitAnswer', {
    'root': str | None,
    'written': list[Any] | None,
    'kept': list[Any] | None,
    'guide_saved_copy': str | None,
    'next_steps': str | None,
    'config_upgrade': dict[str, Any] | None,
}, total=False)


#: The operating guide, printed or written as AGENTS.md. (guide)
GuideRequest = TypedDict('GuideRequest', {
    'write': bool | None,
    'root': str | None,
    'format': str | None,
}, total=False)


#: The guide's text, or what writing it did. (guide)
GuideAnswer = TypedDict('GuideAnswer', {
    'guide': str | None,
    'outcome': str | None,
    'path': str | None,
    'saved_copy': str | None,
}, total=False)


#: The plaintext secret source and the encrypted store to write; never the passphrase. (encrypt-secret)
EncryptSecretRequest = TypedDict('EncryptSecretRequest', {
    'source': str | None,
    'dest': str | None,
    'format': str | None,
}, total=False)


#: How many secrets were encrypted, and where. (encrypt-secret)
EncryptSecretAnswer = TypedDict('EncryptSecretAnswer', {
    'count': int | None,
    'source': str | None,
    'dest': str | None,
}, total=False)


#: Which tool root to carry, into which bundle file, with or without secrets and assets. (export-data)
ExportDataRequest = TypedDict('ExportDataRequest', {
    'bundle': str | None,
    'root': str | None,
    'include_secrets': bool | None,
    'include_assets': bool | None,
    'force': bool | None,
    'format': str | None,
}, total=False)


#: What the bundle carries, and what it could not. (export-data)
ExportDataAnswer = TypedDict('ExportDataAnswer', {
    'bundle': str | None,
    'files_by_role': dict[str, Any] | None,
    'missing_at_source': list[Any] | None,
    'includes_secret_store': bool | None,
    'warnings': list[Any] | None,
}, total=False)


#: Which bundle to apply to which tool root, as a plan or for real. (import-data)
ImportDataRequest = TypedDict('ImportDataRequest', {
    'bundle': str | None,
    'root': str | None,
    'plan_only': bool | None,
    'force': bool | None,
    'include_secrets': bool | None,
    'include_assets': bool | None,
    'format': str | None,
}, total=False)


#: What the import wrote, or would write. (import-data)
ImportDataAnswer = TypedDict('ImportDataAnswer', {
    'plan_only': bool | None,
    'root': str | None,
    'plan': list[Any] | None,
    'counts': dict[str, Any] | None,
    'created': list[Any] | None,
    'replaced': list[Any] | None,
    'unchanged': list[Any] | None,
    'missing_at_source': list[Any] | None,
    'secret_store_imported': bool | None,
    'node_role_needed': str | None,
    'config_upgrade': dict[str, Any] | None,
}, total=False)


__all__ = [
    'GateReportAnswer',
    'AskAnswer',
    'GateAnswer',
    'FileCopyAnswer',
    'FilePackAnswer',
    'FileRelayAnswer',
    'BackupPackAnswer',
    'FilePullPushAnswer',
    'BackupFileListAnswer',
    'BackupFileAnswer',
    'BackupPruneAnswer',
    'FileDeleteAnswer',
    'DatabaseListAnswer',
    'SchemaListAnswer',
    'JobListAnswer',
    'TableLoadAnswer',
    'DescribeObjectAnswer',
    'DueCheckAnswer',
    'CheckObjectsAnswer',
    'CheckReferencesAnswer',
    'ConfigMigrationAnswer',
    'RestoreStepAnswer',
    'RestoreKeyAnswer',
    'RestoreMetadataAnswer',
    'VerifyRestoreAnswer',
    'BackupDatabaseAnswer',
    'CreateDbDockerAnswer',
    'MoveDbDockerAnswer',
    'MetricBatchRequest',
    'MetricBatchAnswer',
    'CheckCredentialsAnswer',
    'RunSqlcmdAnswer',
    'BackupChainRequest',
    'BackupChainAnswer',
    'CopyBackupDirRequest',
    'CopyBackupDirAnswer',
    'PruneStagedBackupsRequest',
    'PruneStagedBackupsAnswer',
    'RegistrationAnswer',
    'ConfigEditAnswer',
    'SecretSetAnswer',
    'RunSqlAnswer',
    'RunCmdAnswer',
    'TraceSessionAnswer',
    'DbStatusAnswer',
    'ProbeHostAnswer',
    'TargetListAnswer',
    'InventorySummaryAnswer',
    'SelfStatusAnswer',
    'SecretCheckAnswer',
    'IdentifierScanAnswer',
    'ExampleLiftAnswer',
    'ShowcaseAnswer',
    'CopySchemaAnswer',
    'AuthorizeRequest',
    'AskRequest',
    'HostOperationRequest',
    'SqlserverEmergencyRequest',
    'SqlserverPatchRequest',
    'SqlserverInstanceRequest',
    'FileTransferRequest',
    'BackupPackRequest',
    'BackupFilesRequest',
    'FileDeleteRequest',
    'CatalogRequest',
    'TableLoadRequest',
    'DescribeObjectRequest',
    'DueCheckRequest',
    'ConfigCheckRequest',
    'ConfigMigrationRequest',
    'RestoreStepRequest',
    'RemoteTargetRequest',
    'RestoreKeyRequest',
    'RestoreMetadataRequest',
    'VerifyRestoreRequest',
    'BackupDatabaseRequest',
    'CreateDbDockerRequest',
    'MoveDbDockerRequest',
    'RunSqlcmdRequest',
    'SmbRequest',
    'SmbListAnswer',
    'SmbGetAnswer',
    'SmbDeleteAnswer',
    'SmbCredentialAnswer',
    'InstanceAddRequest',
    'RemoteCredentialAddRequest',
    'SqlCommandAddRequest',
    'SqlTargetAddRequest',
    'AddSqlRequest',
    'MetricToggleRequest',
    'MetricSeverityRequest',
    'AppCommandSetRequest',
    'SecretSetRequest',
    'RunSqlRequest',
    'RunCmdRequest',
    'TraceSessionRequest',
    'DbStatusRequest',
    'ProbeHostRequest',
    'ListingRequest',
    'SelfStatusRequest',
    'InventorySummaryRequest',
    'CheckSecretRequest',
    'RotatePasswordRequest',
    'IdentifierScanRequest',
    'SecretLiteralsRequest',
    'ExampleLiftRequest',
    'ShowcaseRequest',
    'CopySchemaRequest',
    'InitRequest',
    'InitAnswer',
    'GuideRequest',
    'GuideAnswer',
    'EncryptSecretRequest',
    'EncryptSecretAnswer',
    'ExportDataRequest',
    'ExportDataAnswer',
    'ImportDataRequest',
    'ImportDataAnswer',
]
