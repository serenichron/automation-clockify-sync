#!/bin/zsh
set -u

# launchd does not read shell environment files. Keep the private analyzer and
# run configuration in the same mode-0600 file used by the Linux service.
environment_file="${HOME}/.config/serenichron/clockify-work-accounting.env"
root="${HOME}/Work/automation-clockify-sync"
python="/opt/homebrew/bin/python3"

# A missing or unsafe configuration is a known fail-closed state. Exit cleanly
# so launchd does not turn it into an unattended restart loop.
[[ -r "${environment_file}" ]] || exit 0
[[ -x "${python}" ]] || exit 0
[[ -f "${root}/scripts/clockify_accounting_runner.py" ]] || exit 0

set -a
. "${environment_file}"
set +a

unset CLOCKIFY_ANALYZER_FALLBACK_URL
unset CLOCKIFY_ANALYZER_FALLBACK_MODEL
unset CLOCKIFY_ANALYZER_FALLBACK_API_KEY
unset CLOCKIFY_ANALYZER_FALLBACK_TIMEOUT_SECONDS
unset CLOCKIFY_ANALYZER_FALLBACK_REVISION

if "${python}" "${root}/scripts/clockify_accounting_runner.py"; then
  exit 0
else
  runner_exit=$?
fi

# Exit 2 means the runner intentionally blocked on configuration, integrity,
# authorization, authentication, or route health. Match systemd's
# RestartPreventExitStatus=2 semantics by suppressing automatic restart.
[[ "${runner_exit}" -eq 2 ]] && exit 0
exit "${runner_exit}"
