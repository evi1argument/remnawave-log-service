# shellcheck shell=bash

loc() {
  if [[ "${INSTALL_LANG:-en}" == ru ]]; then
    printf '%s' "$2"
  else
    printf '%s' "$1"
  fi
}
