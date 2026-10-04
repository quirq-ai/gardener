#!/usr/bin/env bash
# Builds a throwaway git repo for the V0-GAR-02 done-when: N commits on main, where commit BREAK
# breaks `./check.sh` (a stand-in for a repo's build or test) and every later commit stays broken.
# Prints the good (first) commit, the planted culprit and the bad (last) commit.
#   tools/plant-break.sh DIR [N=12] [BREAK=7]
set -euo pipefail
dir=$1; n=${2:-12}; brk=${3:-7}
rm -rf "$dir"; mkdir -p "$dir"; cd "$dir"
git init -q -b main
git config user.email gardener@example.invalid
git config user.name "qq gardener demo"
printf '#!/bin/sh\ntest "$(cat state)" = ok\n' > check.sh; chmod +x check.sh
for i in $(seq 1 "$n"); do
  if [ "$i" -ge "$brk" ]; then echo broken > state; else echo ok > state; fi
  echo "$i" > counter
  git add -A; git commit -q -m "commit $i"
  [ "$i" -eq 1 ] && good=$(git rev-parse HEAD)
  [ "$i" -eq "$brk" ] && culprit=$(git rev-parse HEAD)
done
echo "good=$good"
echo "culprit=$culprit"
echo "bad=$(git rev-parse HEAD)"
