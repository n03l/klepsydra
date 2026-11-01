#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Builds the deployable site into public/. Vercel (vercel.json) and the GitHub Pages workflow
# (.github/workflows/pages.yml) run this; the tests do too.
# Only the files listed here are published.
set -eu
cd "$(dirname "$0")"
rm -rf public
mkdir public
cp -R index.html app.js style.css sw.js manifest.json timers.json LICENSE icons public/
# Commit shown (shortened) and linked under the list: the one GitHub Actions or Vercel is building, else
# this checkout's (a local build); empty when none is known.
commit="${GITHUB_SHA:-${VERCEL_GIT_COMMIT_SHA:-$(git rev-parse HEAD 2>/dev/null || true)}}"
printf '%s\n' "$commit" > public/version.txt
# The Google OAuth client id for Drive sync (public, not a secret): $GOOGLE_CLIENT_ID, else the line
# GOOGLE_CLIENT_ID=… in .env.local (gitignored). Without one the app hides Drive sync.
client_id="${GOOGLE_CLIENT_ID:-}"
if [ -z "$client_id" ] && [ -f .env.local ]; then
  client_id="$(sed -n 's/^GOOGLE_CLIENT_ID=//p' .env.local | tail -n 1 | tr -d '[:space:]')"
fi
if [ -n "$client_id" ] && ! printf '%s' "$client_id" | grep -Eq '^[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$'; then
  echo "build.sh: GOOGLE_CLIENT_ID doesn't look like <id>.apps.googleusercontent.com" >&2
  exit 1
fi
printf '%s\n' "$client_id" > public/google-client-id.txt
