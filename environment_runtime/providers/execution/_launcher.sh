#!/bin/sh
# POSIX fallback launcher for detached SSH tasks.
#
# It mirrors _launcher.py closely enough for small remote systems where Python is
# not available: append combined stdout/stderr to a log file, wait for the child,
# and atomically write {"exit_code":N,"finished_at":"..."} to a status file.

set -u

envrt_log=
envrt_status=
envrt_cwd=

envrt_parent_dir() {
  case "$1" in
    */*) printf '%s\n' "${1%/*}" ;;
    *) printf '.\n' ;;
  esac
}

envrt_write_status() {
  envrt_code="$1"
  envrt_finished="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || printf unknown)"
  envrt_tmp="${envrt_status}.tmp.$$"
  mkdir -p "$(envrt_parent_dir "$envrt_status")" 2>/dev/null || exit 125
  printf '{"exit_code":%s,"finished_at":"%s"}\n' "$envrt_code" "$envrt_finished" > "$envrt_tmp" || exit 125
  mv "$envrt_tmp" "$envrt_status" || exit 125
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --log)
      [ "$#" -ge 2 ] || exit 2
      envrt_log="$2"
      shift 2
      ;;
    --status)
      [ "$#" -ge 2 ] || exit 2
      envrt_status="$2"
      shift 2
      ;;
    --cwd)
      [ "$#" -ge 2 ] || exit 2
      envrt_cwd="$2"
      shift 2
      ;;
    --env)
      [ "$#" -ge 2 ] || exit 2
      case "$2" in
        *=*) export "$2" ;;
        *) exit 2 ;;
      esac
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

[ -n "$envrt_log" ] || exit 2
[ -n "$envrt_status" ] || exit 2
[ "$#" -gt 0 ] || exit 2

mkdir -p "$(envrt_parent_dir "$envrt_log")" "$(envrt_parent_dir "$envrt_status")" 2>/dev/null || exit 125

if [ -n "$envrt_cwd" ]; then
  cd "$envrt_cwd" || {
    envrt_write_status 127
    exit 127
  }
fi

envrt_child=
envrt_forward_signal() {
  if [ -n "$envrt_child" ]; then
    kill -TERM "$envrt_child" 2>/dev/null || true
  fi
}
trap envrt_forward_signal TERM INT

"$@" >> "$envrt_log" 2>&1 < /dev/null &
envrt_child="$!"
wait "$envrt_child"
envrt_code="$?"
envrt_write_status "$envrt_code"
exit "$envrt_code"
