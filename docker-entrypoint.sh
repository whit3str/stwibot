#!/bin/sh
set -e
# Fix ownership of the bind-mounted /data volume so appuser can write to it
chown -R appuser:appuser /data
exec gosu appuser python main.py
