#!/bin/sh
#
# file-dispatch launcher.
#
# This is a thin wrapper whose only job is to pick the Python interpreter and
# hand off to dispatch.py, which is the whole program. The config's optional
# PYTHON setting is honored by dispatch.py, which re-executes itself with the
# configured interpreter when needed.
#
#   dispatch.sh [--config-file FILE] [--dry-run] [--debug] [--check]
#
# Interpreter selection here: $DISPATCH_PYTHON, else python3 on PATH.

set -u

case $0 in
    */*) dir=${0%/*} ;;
    *)   dir=. ;;
esac
dir=$(CDPATH= cd -- "$dir" && pwd)
py=${DISPATCH_PYTHON:-python3}

if ! command -v "$py" >/dev/null 2>&1; then
    echo "python3 interpreter not found: '$py'" >&2
    exit 3
fi

exec "$py" "$dir/dispatch.py" "$@"
