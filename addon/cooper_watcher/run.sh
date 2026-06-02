#!/usr/bin/with-contenv bashio
# Read add-on options and hand off to the watcher. The Supervisor injects
# SUPERVISOR_TOKEN; we talk to HA only through the Supervisor core proxy.
set -e

export POLL_INTERVAL="$(bashio::config 'poll_interval')"
export MOTION_THRESHOLD="$(bashio::config 'motion_threshold')"
export DAILY_REVIEW_TIME="$(bashio::config 'daily_review_time')"
export CAMERAS="$(bashio::config 'cameras | join(",")')"

bashio::log.info "Starting Cooper Watcher (poll=${POLL_INTERVAL}s, cameras=${CAMERAS:-none})"
exec python3 -m watcher
