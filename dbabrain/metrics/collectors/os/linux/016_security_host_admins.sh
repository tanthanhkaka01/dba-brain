#!/bin/bash
# SECURITY_HOST_ADMINS (Linux): who can be an administrator on this host.
#
# Why: a database's own files open on another machine, so whoever gets onto the host as root can
# take them. This reads who that is, with the login dbabrain already has, changing nothing:
#
#   group:<group>:<account>     members of the groups that give root - sudo, wheel, admin, docker
#                               (docker is root by another name); HOST_ADMIN_GROUPS overrides
#   uid0:<account>              every account with uid 0
#   sshd:PermitRootLogin        the effective value (sshd -T), else the first one in sshd_config
#   sshd:PasswordAuthentication
#   root_key:<fingerprint>      each key in root's authorized_keys
#
# Every item is "<section>:<name>". A section that cannot be read is ONE row with metric_unit
# "unreadable", the section as its item and the reason as its message - never a missing row: the
# change alert skips a section that was unreadable on either read, so losing access to a file is
# never reported as "every key was removed".
#
# Status is always OK: an inventory. What alerts is a CHANGE between two reads (the metric's
# change_alert) - an account added to a group, a new root key, password login turned on.
json_escape() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
first=1
emit() {
  [ "$first" -eq 1 ] || printf ','
  first=0
  printf '{"metric_item":"%s","metric_value":"%s","metric_unit":"%s","status":"OK","message":"%s"}' \
    "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")" "$(json_escape "$4")"
}

me=$(id -un 2>/dev/null || echo unknown)
# Root, or sudo with no password: the only two ways this reads a root-only file. Never a prompt.
as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@" 2>/dev/null; fi
}
can_root=no
if [ "$(id -u)" -eq 0 ]; then can_root=yes
elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then can_root=yes
fi

printf '['
# What the read was made with, so the page can say it: always present, never compared - and the
# first row, so a row printed from a pipe's subshell below can always lead with its comma.
emit "read_as" "$me" "summary" "account=$me, root_access=$can_root, groups_read=${HOST_ADMIN_GROUPS:-sudo wheel admin docker}"

# --- groups that give root ---------------------------------------------------------------------
for group in ${HOST_ADMIN_GROUPS:-sudo wheel admin docker}; do
  entry=$(getent group "$group" 2>/dev/null) || continue
  gid=$(printf '%s' "$entry" | cut -d: -f3)
  # Listed members, and the accounts whose primary group it is (they are not in the member list).
  members=$( { printf '%s' "$entry" | cut -d: -f4 | tr ',' '\n';
               getent passwd 2>/dev/null | awk -F: -v gid="$gid" '$4 == gid {print $1}'; } | sed '/^$/d' | sort -u )
  for member in $members; do
    emit "group:$group:$member" "member" "account" "group=$group, gid=$gid, account=$member"
  done
done

# --- uid 0 ---------------------------------------------------------------------------------------
for account in $(getent passwd 2>/dev/null | awk -F: '$3 == 0 {print $1}'); do
  emit "uid0:$account" "uid 0" "account" "account=$account, uid=0"
done

# --- sshd ----------------------------------------------------------------------------------------
sshd_bin=$(command -v sshd 2>/dev/null || ls /usr/sbin/sshd 2>/dev/null)
effective=""
[ "$can_root" = yes ] && [ -n "$sshd_bin" ] && effective=$(as_root "$sshd_bin" -T 2>/dev/null)
# The files, as sshd reads them: first value wins, an Include is read where it stands, and
# anything after the first Match belongs to that Match.
sshd_files() {
  local file="$1" line keyword rest
  [ -r "$file" ] || return 1
  while IFS= read -r line; do
    line=$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*#.*$//')
    [ -n "$line" ] || continue
    keyword=$(printf '%s' "$line" | awk '{print tolower($1)}')
    rest=$(printf '%s' "$line" | sed 's/^[^[:space:]=]*[[:space:]=]*//')
    case "$keyword" in
      match) return 0 ;;
      include)
        for included in $rest; do
          case "$included" in /*) ;; *) included="/etc/ssh/$included" ;; esac
          for one in $included; do [ -f "$one" ] && sshd_files "$one"; done
        done ;;
      *) printf '%s %s\n' "$keyword" "$rest" ;;
    esac
  done < "$file"
}
if [ -n "$effective" ]; then
  for keyword in PermitRootLogin PasswordAuthentication; do
    value=$(printf '%s\n' "$effective" | awk -v k="$(printf '%s' "$keyword" | tr 'A-Z' 'a-z')" 'tolower($1) == k {print $2; exit}')
    emit "sshd:$keyword" "${value:-not set}" "setting" "source=sshd -T (the effective configuration)"
  done
elif [ -r /etc/ssh/sshd_config ]; then
  parsed=$(sshd_files /etc/ssh/sshd_config)
  for keyword in PermitRootLogin PasswordAuthentication; do
    value=$(printf '%s\n' "$parsed" | awk -v k="$(printf '%s' "$keyword" | tr 'A-Z' 'a-z')" '$1 == k {print $2; exit}')
    if [ -n "$value" ]; then
      emit "sshd:$keyword" "$value" "setting" "source=/etc/ssh/sshd_config and its includes (read as $me; sshd -T needs root)"
    else
      emit "sshd:$keyword" "not set" "setting" "source=/etc/ssh/sshd_config and its includes (read as $me): not set there - the build's default applies"
    fi
  done
elif [ -e /etc/ssh/sshd_config ]; then
  emit "sshd" "unreadable" "unreadable" "/etc/ssh/sshd_config is not readable as $me, and sudo without a password is not allowed"
else
  emit "sshd" "unreadable" "unreadable" "no /etc/ssh/sshd_config on this host (no OpenSSH server found)"
fi

# --- root's authorized keys ------------------------------------------------------------------------
root_home=$(getent passwd root 2>/dev/null | cut -d: -f6)
root_home=${root_home:-/root}
if [ "$can_root" = yes ]; then
  for file in "$root_home/.ssh/authorized_keys" "$root_home/.ssh/authorized_keys2"; do
    as_root test -f "$file" || continue
    as_root cat "$file" | while IFS= read -r line || [ -n "$line" ]; do
      line=$(printf '%s' "$line" | sed 's/^[[:space:]]*//')
      case "$line" in ''|'#'*) continue ;; esac
      print=$(printf '%s\n' "$line" | ssh-keygen -l -f /dev/stdin 2>/dev/null | head -n 1)
      if [ -n "$print" ]; then
        fingerprint=$(printf '%s' "$print" | awk '{print $2}')
        kind=$(printf '%s' "$print" | awk '{print $NF}' | tr -d '()')
        comment=$(printf '%s' "$print" | awk '{$1=""; $2=""; $NF=""; print}' | sed 's/^ *//; s/ *$//')
      else
        # No ssh-keygen (or a line it cannot read): the line's own hash still names the key.
        fingerprint="sha256-of-line:$(printf '%s' "$line" | sha256sum | cut -c1-32)"
        kind="unknown"
        comment=""
      fi
      # emit runs in this pipe's subshell: print the row with its own separator.
      printf ',{"metric_item":"%s","metric_value":"%s","metric_unit":"key","status":"OK","message":"%s"}' \
        "$(json_escape "root_key:$fingerprint")" "$(json_escape "$kind")" \
        "$(json_escape "file=$file, comment=${comment:--}")"
    done
  done
else
  emit "root_key" "unreadable" "unreadable" "$root_home/.ssh/authorized_keys needs root: $me is not root and sudo without a password is not allowed"
fi

printf ']\n'
