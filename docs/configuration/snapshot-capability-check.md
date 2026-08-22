# Snapshot ref capability check

The late size gate preserves a superseded candidate under a **custom ref namespace**,
`refs/orchestrator/late-split/issue-<n>/cycle-<c>/gen-<g>`, rather than under a branch or a tag
([`../workflow/roles.md`](../workflow/roles.md#what-a-cleared-split-actually-does)). Whether a production token may
create, fetch, verify, and delete refs there is a property of the repository's rulesets and of the token's
permissions, not of this code — so it is proved against a **disposable repository with production-equivalent settings
before the gate is enabled anywhere**, and a failure blocks rollout rather than being answered by weakening a rule.

Run it once per production-equivalent configuration: per org whose ruleset defaults differ, and again whenever
repository rules, ruleset targets, or the token's permission set change.

## What has to be proved

Four properties, in this order, because each is what makes the next meaningful:

1. **Create.** A ref in the namespace can be pushed at an exact commit, under a lease that requires it to be absent.
2. **Fetch.** That ref can be fetched back by the same token and resolves locally to the exact commit that was
   pushed. This is the half `ls-remote` cannot answer, and it is the property every child of a split depends on: a
   namespace the token may write and not read passes a remote read and fails the first child that tries to use it.
3. **No overwrite.** A second create against the same ref at a *different* commit is refused rather than accepted.
4. **Delete, and absent-is-success.** The ref can be deleted *under a lease naming the commit it preserved*, and a
   second delete of the now-absent ref reports success rather than failure — which is what makes reclamation
   idempotent across a crash between the push that deleted a ref and the write that would have recorded it. The
   orchestrator never deletes a ref carrying anything other than that commit, so the lease below is pinned to it
   rather than to a fresh reading.

## Preparing the repository

1. Create a throwaway repository in the **same organization** as the production target — ruleset defaults and
   org-level policies are what this is measuring, and a personal scratch repo will not reproduce them.
2. Apply the production ruleset configuration to it. If your rulesets target `refs/heads/**` or `refs/tags/**` only,
   apply them as they are; if any of them target `refs/**`, apply that too — that is the case most likely to fail.
3. Issue (or reuse) a token with **exactly** the production permission set. Do not grant anything extra "just for the
   check": a check that passes under wider permissions than production has proved nothing.
4. Write the token where the orchestrator would read it, or export it for the shell below:

```sh
export SLUG=<org>/<disposable-repo>
export GIT_TOKEN=$(cat ~/.config/"$SLUG"/token)
export AUTH_URL="https://x-access-token@github.com/$SLUG.git"
export REF="refs/orchestrator/late-split/issue-1/cycle-1/gen-0"
```

## The check

Run from a clone of the disposable repository. `GIT_ASKPASS` keeps the token out of `argv`, which is what the
orchestrator's own transport does; the `-c` overrides and the detached global config reproduce the rest of its
envelope, so a hook or a url rewrite on the operator's machine cannot make the check pass where production would
fail.

```sh
printf '#!/bin/sh\nprintf %%s "$GIT_TOKEN"\n' > /tmp/askpass.sh && chmod 700 /tmp/askpass.sh
export GIT_ASKPASS=/tmp/askpass.sh GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1
git() { command git -c core.hooksPath=/dev/null -c credential.helper= -c core.fsmonitor= "$@"; }

FIRST=$(git rev-parse HEAD)
SECOND=$(git rev-parse HEAD~1)

# 1. create, leased as absent
git push --force-with-lease="$REF": "$AUTH_URL" "$FIRST:$REF"          || echo "FAIL: create"

# 2. fetch it back and resolve it here
git fetch --quiet "$AUTH_URL" "+$REF:$REF"                              || echo "FAIL: fetch"
test "$(git rev-parse --verify "$REF^{commit}")" = "$FIRST"             || echo "FAIL: verify"

# 3. a different commit under the same ref must be refused
git push --force-with-lease="$REF": "$AUTH_URL" "$SECOND:$REF" \
  && echo "FAIL: an occupied ref was overwritten"

# 4. delete, then delete again -- the second must succeed
git push --force-with-lease="$REF:$FIRST" "$AUTH_URL" ":$REF"           || echo "FAIL: delete"
test -z "$(git ls-remote "$AUTH_URL" "$REF")"                           || echo "FAIL: still present"
```

Every step must print nothing. The third step is inverted on purpose: it is expected to *fail*, and the check fails
if it succeeds. Step 4's "delete again" is the `ls-remote` on the last line — the orchestrator's own deletion reads
the ref first and reports an absent one as already reclaimed, so an empty listing is exactly the success it needs.

## Reading a failure

- **`create` fails with `refusing to allow ... to create or update`.** A ruleset or the token's permissions cover the
  custom namespace. Scope the ruleset to `refs/heads/**` / `refs/tags/**`, or exclude `refs/orchestrator/**`.
- **`create` fails with `Invalid refspec` or `funny refname`.** The remote rejects the namespace outright. Do not
  enable the gate, and raise it before rollout rather than moving to a branch namespace by hand.
- **`fetch` or `verify` fails.** The token can write the namespace and not read it back. Fix the permission
  asymmetry: a child would otherwise be pointed at a ref it cannot obtain.
- **The overwrite step *succeeds*.** The lease is not being enforced by the remote. Do not enable the gate — the
  immutability the whole design rests on is not there.
- **`delete` fails.** Reclamation would never complete. Fix the permission: a snapshot that cannot be deleted blocks
  the umbrella's terminal completion for good.

Record the result — repository, ruleset revision, token permission set, and date — beside the deployment's other
rollout evidence. The gate's own behavior under each of these answers is covered by
`tests/git/snapshots/test_refs.py`, which drives real `git` against a local bare repository; what this runbook adds
is the half a local remote cannot answer, which is what **GitHub** does under **your** rules.
