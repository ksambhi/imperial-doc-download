# Implementation notes: `gitlab_fetch`

How the clone stage works and, more usefully, the things about DoC's git
hosting that were established by testing rather than by reading docs.
Companion to `docs/labts-fetch-plan.md`, which covers the stage that
produces this one's input.

## 1. What it does

Takes `labts-list.json` (from `labts_fetch`, via `ctx.state` or off disk)
and clones each repository to
`<output_dir>/<academic_year>/gitlab/<repo>/`, recording the outcome in
`<output_dir>/gitlab-clones.json`.

One clone per **repository**, not per exercise or milestone. A
multi-milestone exercise is a single repo carrying several submitted
revisions — verified on `pintos_17`, whose one clone contains all three
of the task revisions LabTS lists (§2.4 of the LabTS plan).

The directory is named from the clone URL's project name (`pintos_17`,
`SED_Ex1_kss22`), not the exercise name, since that's unambiguous. Two
repositories in one year sharing a project name (the same exercise run in
both terms, say) would collide, so the loser is qualified with its GitLab
namespace: `lab2324_spring__CW1_kss22`.

## 1.1 ⚠️ A plain clone is not a complete copy

These repositories are about to be deleted, so "what `git clone` gives
you" isn't the bar. Comparing `git ls-remote` against a fresh clone of
`pintos_17`:

| Ref namespace | On the server | In a plain clone |
|---|---|---|
| `refs/heads/*` | 35 | 35, as remote-tracking refs only |
| `refs/tags/*` | 3 | 3 |
| `refs/keep-around/*` | 477 | **0** |
| `refs/merge-requests/*` | 2 | **0** |

`refs/keep-around/*` is GitLab pinning commits it doesn't want garbage
collected — force-pushed work, commits referenced from merge request
discussions. **159 of those 477 point at commits no branch or tag
reaches**, e.g. `004f619a` "Feat: Implement basic niceness functionality".
Clone normally and that work stays on the server until the server dies.

So after cloning, everything is fetched with `+refs/*:refs/imperial-mirror/*`.
Mapping into a namespace of our own, rather than `+refs/*:refs/*`, avoids
git refusing to overwrite the checked-out branch, and keeps the
repository's own branches unambiguous. The objects then stay alive by
ordinary reachability.

Local branches are also created for every remote branch, in one
`git update-ref --stdin` batch rather than N `git branch` calls.

⚠️ Do **not** read those branch names with `%(refname:short)`:
`refs/remotes/origin/HEAD` shortens to plain **`origin`**, which taken as
a branch name creates a junk `origin` branch in every repository. Use
`%(refname)` and strip the prefix. (Found by a test.)

## 1.2 Checking out the submitted revision

LabTS records a submitted revision per milestone, and also creates a tag
for it named after the milestone (`PintOS_Task_1_-_Scheduling` →
`bc9dc865`, verified). After cloning, that revision is checked out:

- a branch points at it (the usual case — it's the tip of `master`) →
  check out that branch;
- nothing points at it (an earlier milestone of a multi-milestone
  exercise, with later work on top) → check out the revision, leaving
  HEAD detached. Inventing a branch there would claim a branch the
  repository never had.

With several milestones HEAD lands on the last one's revision — the most
complete piece of work. The rest stay reachable through their LabTS tags
and the history itself.

## 2. Two remotes, tried in order

### 2.1 GitLab first

`git@gitlab.doc.ic.ac.uk:<namespace>/<project>.git`, with the GitLab key.
Works from anywhere, no jump.

### 2.2 gitolite as the fallback — and why it's needed

GitLab stops serving old years. Every 2223 and 2324 repository tried came
back with:

```
remote: The project you were looking for could not be found or you don't have permission to view it.
```

while the same path on gitolite cloned fine. So the fallback isn't an
edge case — for anything but the current year or two it's the *only*
route. The URL rewrite is just the prefix:

```
git@gitlab.doc.ic.ac.uk:lab2223_autumn/haskellsequences_kss22.git
gitolite@gitolite.doc.ic.ac.uk:lab2223_autumn/haskellsequences_kss22.git
```

gitolite is behind the departmental firewall, so it's reached with
`ProxyJump` through a DoC shell server. Two keys are involved, and it's
worth being clear about which goes where:

| Hop | Host | Key | User |
|---|---|---|---|
| jump | `shell{1..5}.doc.ic.ac.uk` | `IMPERIAL_DOC_SSH_KEY` | `IMPERIAL_USERNAME` |
| target | `gitolite.doc.ic.ac.uk` | `IMPERIAL_GITLAB_SSH_KEY` | `gitolite` |

### 2.3 ⚠️ The jump host is a shell server, not `sftp.doc.ic.ac.uk`

`sftp.doc.ic.ac.uk` (146.169.21.53) is a **different machine** from the
shell servers and **refuses ssh key auth** — it only serves SFTP:

```
debug1: Offering public key: .../doclab_ecdsa ECDSA SHA256:... explicit
debug1: Authentications that can continue: publickey,gssapi-with-mic
kss22@sftp.doc.ic.ac.uk: Permission denied (publickey,gssapi-with-mic).
```

The same key on `shell5.doc.ic.ac.uk` (146.169.40.251) authenticates
immediately. This is easy to get wrong because a common local
`~/.ssh/config` idiom aliases `Host sftp.doc.ic.ac.uk` to
`HostName shell5.doc.ic.ac.uk`, which makes "jump via sftp" *look* like
it works — it's jumping via shell5 under another name.

So: jump via a shell server, picked at random from `shell1-5` per run
rather than always loading the same box. `--jump-host` overrides it.

### 2.4 gitolite speaks git only

Plain `ssh gitolite@gitolite.doc.ic.ac.uk` doesn't give the usual gitolite
`hello` info banner; it hangs. Test with `git ls-remote` / `git clone`,
which is the only thing it's there for.

## 3. Not touching `~/.ssh/config`

None of the above may be assumed to be in the user's ssh config, and
editing someone's ssh config is not on. Instead a throwaway config is
written to a per-run temp directory and passed as
`GIT_SSH_COMMAND="ssh -F <file>"`.

The thing that makes this viable: **OpenSSH propagates `-F` to the
`ProxyJump` subprocess**, so the jump host is configured by the same
file. Verified:

```
debug1: Executing proxy command: exec ssh -F /tmp/.../ssh_config -W '[gitolite.doc.ic.ac.uk]:22' imperial-doc-jump
```

Without that, the `ProxyJump` alias would have to become an explicit
`ProxyCommand ssh -F <file> -W %h:%p …`.

Global defaults in the generated config:

- `StrictHostKeyChecking accept-new` — records first-seen hosts in the
  user's own `known_hosts`, still aborts if a known host's key changes.
- `BatchMode yes` — an unattended run fails with a message instead of
  hanging forever on a prompt. Passphrases are dealt with up front (§4)
  so nothing needs to prompt mid-clone.

## 4. Passphrases, once per run

`BatchMode yes` means ssh itself can't ask for a passphrase, so any locked
key must be usable before the first clone starts:

1. Key isn't encrypted → nothing to do.
2. Key is encrypted and already in the user's own `ssh-agent` (matched by
   fingerprint) → use their agent.
3. Otherwise → start a **private `ssh-agent`** for this run, unlock the
   key into it with the passphrase from the environment (or one
   interactive prompt), point the config at it with `IdentityAgent`, and
   kill it at the end.

Feeding `ssh-add` a passphrase non-interactively needs `SSH_ASKPASS` with
`SSH_ASKPASS_REQUIRE=force` (OpenSSH ≥ 8.4; `start_new_session=True` and
`DISPLAY` cover older builds). The helper hands over the passphrase via
the environment — never a file, never a CLI argument, never logged.

⚠️ **The helper answers exactly once.** `ssh-add` re-asks *indefinitely*
while the passphrase it gets is wrong, and only gives up when handed an
empty one — a helper that kept repeating itself wedges the entire run on
a typo. (Found by a test hanging.)

`ssh-keygen -y -P ""` is what detects encryption: it succeeds for an
unlocked key and fails with `incorrect passphrase supplied to decrypt
private key` for a locked one. Two other failures are worth telling
apart, since both would otherwise be reported as a passphrase problem the
user can't fix: `bad permissions` (a world-readable key, which ssh
refuses outright — `chmod 600`) and `error in libcrypto` (pointing at a
`.pub` file by mistake).

## 5. Concurrency

Clones run **sequentially by default** (`--concurrency 1`). The work is
I/O-bound, so the runner is `asyncio.create_subprocess_exec` behind an
`asyncio.Semaphore`: raising `-j` is the only change needed to fan out,
and the tests cover both the default and an overlapping run.

Worth staying modest: the gitolite route puts every clone through a
shared teaching server.

## 6. Failure handling

A repository that no remote will serve is recorded as `failed` with the
attempts that were made, and the run continues — one dead repo shouldn't
abandon the other sixty. Failures are listed at the end and retried on the
next run (only successful clones are treated as cached).

Half-written directories from a failed attempt are removed before the next
remote is tried, so git never refuses to clone into a non-empty directory.

DoC's servers print a multi-line legal banner before any real output, so
the one-line summary in the logs deliberately picks the `fatal:`/`error:`
line rather than the first line.
