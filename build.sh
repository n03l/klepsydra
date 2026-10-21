#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Builds the deployable site into public/. The GitHub Pages workflow runs this
# (.github/workflows/pages.yml); the tests do too.
# Only the files listed here are published.
set -eu
cd "$(dirname "$0")"
rm -rf public
mkdir public
cp -R index.html app.js style.css sw.js manifest.json timers.json LICENSE icons public/
# Commit shown (shortened) and linked under the list: the one GitHub Actions is building, else this
# checkout's (a local build); empty when neither is known.
commit="${GITHUB_SHA:-$(git rev-parse HEAD 2>/dev/null || true)}"
printf '%s\n' "$commit" > public/version.txt
