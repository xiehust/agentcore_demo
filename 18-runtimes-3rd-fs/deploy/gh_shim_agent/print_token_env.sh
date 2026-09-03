#!/bin/sh
# Stand-in for the real gh, used ONLY by the verification: proves the shim injected GH_TOKEN
# into the child process environment without ever printing the token itself.
if [ -n "${GH_TOKEN:-}" ]; then
  printf 'GH_TOKEN_PRESENT_IN_CHILD prefix=%s len=%s\n' "$(printf %s "$GH_TOKEN" | cut -c1-4)" "$(printf %s "$GH_TOKEN" | wc -c)"
else
  echo "GH_TOKEN_ABSENT_IN_CHILD"
fi
