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
#!/bin/sh
set -eu   # the SETUP below must fail closed: a step that does not run cannot
          # be allowed to leave a later one certifying something it never tested

ASKPASS=$(mktemp)
trap 'rm -f "$ASKPASS"' EXIT INT TERM
printf '#!/bin/sh\nprintf %%s "$GIT_TOKEN"\n' > "$ASKPASS"
chmod 700 "$ASKPASS"

export GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1
export MIRROR="refs/orchestrator/late-split-local/capability-check/issue-1/cycle-1/gen-0"

# The detached config above takes the operator's `user.name` / `user.email`
# with it, so the identity the setup commits under is supplied here -- as the
# orchestrator's own hardened runner supplies it -- rather than assumed.
git() {
  command git \
    -c core.hooksPath=/dev/null -c credential.helper= -c core.fsmonitor= \
    -c user.name="capability check" -c user.email="capability@invalid" "$@"
}

FAILED=0
fail() { FAILED=1; echo "FAIL: $1"; }

# SETUP: two commits that are certainly distinct. Step 3 pushes the second one
# over the first and requires a refusal, so a repository with one commit -- or
# a `HEAD~1` that does not resolve -- would leave SECOND empty, turn that push
# into a deletion refspec, and read its failure as overwrite protection.
date > capability-check.txt && git add capability-check.txt
git commit -qm "capability check: first"
FIRST=$(git rev-parse HEAD)
date >> capability-check.txt && git add capability-check.txt
git commit -qm "capability check: second"
SECOND=$(git rev-parse HEAD)
test -n "$FIRST" && test -n "$SECOND" && test "$FIRST" != "$SECOND"

set +e    # past setup, each check records its own verdict rather than aborting

# 1. create, leased as absent
git push -q --force-with-lease="$REF": "$AUTH_URL" "$FIRST:$REF" || fail create

# 2. fetch it back and resolve it here
git fetch -q "$AUTH_URL" "+$REF:$MIRROR"                         || fail fetch
test "$(git rev-parse --verify "$MIRROR^{commit}")" = "$FIRST"   || fail verify

# 3. a different commit under the same ref must be refused, and must not move
#    it. Both halves are needed: a push that failed for its own reasons is not
#    proof the ref is protected.
git push -q --force-with-lease="$REF": "$AUTH_URL" "$SECOND:$REF" 2>/dev/null \
  && fail "an occupied ref was overwritten"
test "$(git ls-remote "$AUTH_URL" "$REF" | cut -f1)" = "$FIRST"  || fail "the ref moved"

# 4. delete, then confirm the absence a retry reads as already reclaimed
git push -q --force-with-lease="$REF:$FIRST" "$AUTH_URL" ":$REF" || fail delete
test -z "$(git ls-remote "$AUTH_URL" "$REF")"                    || fail "still present"
git update-ref -d "$MIRROR"                                      || fail "local drop"

test "$FAILED" -eq 0 && echo "capability check: PASS"
exit "$FAILED"
```

The script is the verdict: it exits **0** and prints `capability check: PASS` when every property holds, and exits
**1** having printed one `FAIL: <step>` line per property that did not. Nothing is inferred from silence — a step
that merely printed a warning is not a failure, and a step that failed cannot be missed by an operator skimming the
output.

Two parts of it are deliberately not what a casual reading expects. The setup runs under `set -eu` and stops on the
first problem, because a step that silently did not happen is what lets a later one certify behavior it never
exercised — the original hazard being an empty `SECOND`, which turns step 3's push into a delete refspec whose
refusal reads exactly like overwrite protection. And step 3 is **inverted**: the push is expected to fail, so its
success is what records a failure, and the `ls-remote` after it closes the same door from the other side by
requiring the ref to still be at the commit it was created with.

Step 4's "already reclaimed" is the `ls-remote` on the following line: the orchestrator's own deletion reads the ref
first and reports an absent one as success, so an empty listing is exactly what it needs. The `update-ref -d` after
it is the local copy the orchestrator drops with the remote one.

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
