# Implementation plan: `materials_fetch`

Download every teaching resource — lecture notes, slides, handouts — for
every module enrolled in, as one zip per module, extracted in place.

Status: **implemented**, in `src/imperial_doc_download/materials_fetch/`.
Everything marked ✅ was verified against the live API on 2026-09-18.
Inherits the safety rules of `emarking-fetch-plan.md` §1, which apply
here for the same reasons.

Two notes from building it:

- **It is opt-in inside `all`.** At ~1.75 GB this step is an order of
  magnitude larger than every other pipeline combined, so `all` skips it
  unless `--materials` is passed. Every other pipeline has a
  `--skip-…` flag of its own.
- **A `404` is not cached.** Unlike eMarking's settled 403s and 404s, a
  module with no materials today may publish them next term, and
  re-asking costs 4 cheap requests. Only successfully extracted modules
  get a marker and are skipped.

## 1. Safety rules — this API writes too

11 routes ✅, and **four of them write**:

```
POST   /resources                        (upload a teaching resource)
DELETE /resources                        (delete resources)
PUT    /resources/{resource_id}          (edit a resource)
DELETE /resources/{resource_id}
PUT    /resources/{resource_id}/file     ← replace a lecturer's file
POST   /{course}/resources/migrate       (migrate a course's resources)
```

`PUT /resources/{resource_id}/file` overwrites the actual file students
download. So the rules carry over unchanged:

- **GET only, by construction.** One request method, no
  `post()`/`put()`/`delete()` helper, ever.
- **Only our own modules.** Scope is the enrolment list (§3). A module we
  weren't enrolled in is not requested — not to see whether it works, not
  for completeness. (Deliberately **not** tested during recon, for that
  reason.)
- **Sequential with a delay by default**, long timeouts, retry with
  backoff. No rate-limit headers are sent ✅, so restraint is ours.
- **Never log the password or the `Authorization` header.**

## 2. The one route ✅

```
GET https://materials-api.doc.ic.ac.uk/resources/zipped?year={year}&course={module_code}
```

Verified against the live `openapi.json` (v1.0) ✅:

| Parameter | In | Required | Note |
|---|---|---|---|
| `year` | query | **yes** | `"2324"` |
| `course` | query | **yes** | the module code |
| `category` | query | no | e.g. `Lecture Notes` — we want everything, so unused |
| `id` | query | no | repeatable resource ids — unused |
| `x-proxied-user` | header | no | sent anyway, as with eMarking |

Declared responses: `200`, `404`, `422` ✅. No response content type is
declared (a `FileResponse`), but what actually comes back is:

```
Content-Type: application/zip
Content-Disposition: attachment; filename="40001_materials.zip"
Content-Length: 24696252
```

`Content-Length` was present on **all 50 responses** ✅, so a truncated
transfer is detectable the same way `emarking_fetch` does it.

**Auth is required** ✅: without credentials the route is `401`, and
`x-proxied-user` alone doesn't satisfy it. (`openapi.json` itself is
readable unauthenticated ✅ — that's how it was inspected.)

Both hosts are inside the DoC firewall, so everything goes through the
SSH SOCKS proxy, exactly as `emarking_fetch` does.

## 3. Scope: the enrolment list, again

Same source as `emarking-fetch-plan.md` §3.3.1 — abc-api, filtered to our
own login, `modules` only, never `modules_helped`:

```
GET https://abc-api.doc.ic.ac.uk/{year}/students?login={IMPERIAL_USERNAME}
```

**50 (year, module) pairs** across 2223/2324/2425/2526 ✅. Each is one
request. Nothing new is needed here: if `emarking-fetch` has run, the
enrolment is already cached at
`<output_dir>/<year>/emarking-enrolment.json` and this step costs **zero
extra abc-api requests**.

> ⚠️ That cache is currently named for eMarking. Two pipelines now depend
> on it, so it should be renamed to `enrolment.json` (or moved to
> `<output_dir>/<year>/enrolment.json`) as part of this work — with the
> old name still read, so an existing output directory doesn't refetch.

## 4. Measured cost ✅

Every one of the 50 module-years was probed, **headers only — no body was
downloaded**:

| | |
|---|---|
| module-years in scope | 50 |
| `200` with a zip | **46** |
| `404` (no materials for that module) | 4 |
| **total bytes** | **1.75 GB** |
| largest single zip | **188.7 MB** (2324/50004) |
| measured throughput | **1.37 MB/s** through the proxy ✅ |

Per year:

| Year | zips | size |
|---|---|---|
| 2223 | 10 | 340.0 MB |
| 2324 | 13 | 533.3 MB |
| 2425 | 12 | 379.9 MB |
| 2526 | 11 | 496.7 MB |

**Runtime: ~21 minutes sequential, ~7 minutes at `-j 4`.** That is an
order of magnitude more data than every other pipeline combined
(eMarking's full run is ~400 MB), which drives most of §6 and §7.

**Disk: ~3.5 GB** if the zips are kept *and* extracted. Zips compress
badly — a 427 KB zip held 0.45 MB of content ✅, because it's PDFs — so
extracted content is roughly the same size again. See §7.1.

## 5. What's inside a zip ✅

Inspected for real (2324/50007.2, 427 KB):

```
50007.2/Written Notes/(0) Prolog Course Notes.pdf
50007.2/links.md
```

Three things follow:

- **Members are already prefixed with the module code**, and category
  names (`Written Notes`) are directories. So extracting into
  `<year>/<module>/materials/` yields `materials/50007.2/Written
  Notes/…` — a redundant level. See §7.2.
- **A `links.md` appears to be generated per module**, listing external
  resources. It is the *only* member of the five smallest zips ✅
  (193–337 bytes: 2223/40007, 2223/40008, 2223/COMPM0101, 2425/60021,
  2526/70011). So a tiny zip is not an empty one and not an error — it is
  a module whose materials are all links. Keep it and say so.
- Safety checks on that sample ✅: no absolute paths, no `..` components,
  no backslash separators, no symlink entries. Reassuring, **not**
  sufficient — see §6.

## 6. ⚠️ Extracting a remote zip safely

This is the part of this pipeline that is genuinely new, and the part
worth being careful about: everything else is a download. A zip is a
remote archive that names its own output paths, and `extractall()` on
untrusted input is a well-known way to write files where you didn't
intend.

The rules, in order of how much they matter:

1. **Every member's resolved destination must stay inside the target
   directory.** Resolve `target / member` and compare against the
   resolved target. Reject the whole archive if any member escapes, and
   say which one — don't silently sanitise it into a file the user then
   can't find. (CPython's `ZipFile.extract` does strip `..` and leading
   separators, but relying on an implementation detail of the standard
   library for a security property is how this goes wrong later. Check
   explicitly.)
2. **Refuse symlink and non-regular members.** `zipfile` writes a symlink
   entry's target as ordinary file content rather than creating a link,
   which is safe by accident. Detect them (`external_attr >> 16 &
   0o170000`) and skip with a recorded reason, so the behaviour is
   intended rather than inherited.
3. **Zip-bomb guards.** Cap total uncompressed bytes and member count,
   and check `ZipInfo.file_size` *before* writing rather than discovering
   it on a full disk. The real data's largest zip is 188.7 MB, so a limit
   of ~2 GB per archive and ~50,000 members leaves two orders of
   magnitude of headroom and still stops something pathological.
4. **Names must survive the filesystem.** `naming.safe_component` already
   exists for exactly this, and these names are lecturer-written
   (`(0) Prolog Course Notes.pdf`). Apply it per path component, never to
   the whole path — it replaces `/`, which would flatten the structure.
5. **Extract to a temp directory, then move into place**, so an
   interrupted extraction never leaves a half-tree that a later run
   treats as complete — the same discipline the downloads already use.

None of this is hypothetical enough to skip: the archive is written by a
different system, and this tool runs unattended against a directory full
of the user's other data.

## 7. Output layout

As asked — zip saved and extracted in the same place:

```
<output_dir>/<year>/<module_code>/materials/
├── .materials.json                  what was extracted, and from which zip
├── <module_code>_materials.zip      only with --keep-zip (§7.1)
└── <extracted tree>                 see §7.2
```

This sits beside the existing `<year>/<module_code>/emarking/`, so a
module's coursework and its teaching materials end up together — which is
the arrangement that makes the output directory readable.

### 7.1 ✅ Decided: the zip is deleted once it has extracted cleanly

Keeping both would double the footprint to ~3.5 GB, and the extracted
tree is the useful half. So the default is **delete after a verified
extraction**, with `--keep-zip` to retain them.

"Verified" is load-bearing: the zip is only removed after the extraction
has completed and been moved into place (§6.5). A failed or partial
extraction keeps its zip, so the next run can retry without
re-downloading.

### 7.2 The redundant top-level directory

Every member of a zip starts with the module code (§5), so a plain
extraction gives:

```
2324/50007.2/materials/50007.2/Written Notes/(0) Prolog Course Notes.pdf
                       ^^^^^^^ ^^^^^^^ twice
```

**Recommendation: strip it, but only when it's unambiguous** — when every
member shares one top-level directory. Otherwise extract as-is. That
rule is safe (it can't merge two trees) and testable, and it turns the
above into `materials/Written Notes/…`. A `--keep-zip-structure` flag can
restore the literal layout if the stripping ever surprises.

## 8. Caching, and what a re-run does

Re-running must not re-download 1.75 GB. There is **no `ETag` and no
`Last-Modified`** on these responses ✅ — the headers are only `server`,
`content-type`, `content-length`, `content-disposition`, `x-served-by` —
so the test is:

a **`.materials.json` marker** written inside each `materials/`
directory, recording the zip's `Content-Length`, its name, and what the
extraction produced (member count, total bytes). Since the zip is deleted
by default (§7.1), the marker — not the zip — is what makes "already
have this" answerable.

Marker present and its recorded size matches the `Content-Length` the
server now reports → skip. But checking that costs a request, so the
default is **marker present → skip without asking at all**, with
`--recheck` to spend 46 cheap header requests confirming nothing has
changed upstream. `--force` redownloads regardless.

A `404` is terminal and expected (4 modules ✅) — recorded as `absent`,
never retried, exactly as `emarking_fetch` treats a missing artefact.

## 9. Code: three things move before anything new is written

`materials_fetch` needs the SOCKS proxy, the enrolment call, and a
GET-only streaming client — all three currently live inside
`emarking_fetch`. This is the same situation as `ssh.py` before
`emarking_fetch` needed it, and the same answer:

```
src/imperial_doc_download/
├── proxy.py                 MOVED from emarking_fetch/ (nothing emarking-specific)
├── doc_api.py               NEW shared base: GET-only async client —
│                              basic auth, x-proxied-user, delay+semaphore,
│                              retry, stream-to-temp-then-rename,
│                              terminal-status handling, and `enrolment()`
│                              (abc-api is shared, not eMarking's)
├── emarking_fetch/
│   └── client.py            keeps `exercises()`, `download()` on the shared base
└── materials_fetch/
    ├── __init__.py          exports MaterialsFetchStep
    ├── client.py            `zipped(year, course)` on the shared base
    ├── archive.py           pure: the §6 safety checks, extraction planning
    └── step.py              scope → download → extract → manifest → report
tests/
├── test_materials_archive.py   the §6 cases, built as real zips in tmp_path
└── test_materials_step.py      end-to-end against a mocked API
```

Plus a `materials` CLI subcommand, a step in `all`, and a README section.

**The refactor is the risk in this plan, not the new pipeline.** It
touches a client that three shipped steps depend on. It should land as
its own commit, with the existing emarking tests passing unchanged as the
evidence that nothing moved semantically — the same way the `ssh.py`
split was done.

## 10. Testing

`archive.py` is where the interesting tests are, and they can all build
real zips in `tmp_path`:

- a member resolving outside the target (`../../evil`) → whole archive
  rejected, nothing written, the offending name in the error
- an absolute path member (`/etc/passwd`) → same
- a symlink member → skipped, recorded, no link created
- uncompressed size over the cap → rejected before writing
- member count over the cap → rejected
- a name with characters the filesystem won't take → sanitised per
  component, structure preserved
- the common-prefix strip: applied when every member shares one top-level
  directory, **not** applied when they don't
- an interrupted extraction leaves nothing behind
- a zip containing only `links.md` extracts fine and is recorded as
  materials, not as empty

And for the step:

- only enrolled modules are requested; no other module code appears
- a `404` is recorded as `absent` and not retried on the next run
- a re-run with a matching zip size makes zero requests
- `--no-keep-zip` removes the zip only after a clean extraction
- the client only ever issues `GET`, and has no write helper (the same
  test `emarking_fetch` already has, since the write routes here are just
  as real)

## 11. Decisions ✅

1. **The zip is deleted after a clean extraction** — §7.1. `--keep-zip`
   retains it.
2. **Past papers are out of scope.** `GET /past-papers` and
   `/past-papers/{id}/file` exist ✅ and are left alone.
3. **`/{year}/public-resources` is out of scope** ✅ — very likely a
   subset of what the per-module zips already contain.
4. **`--concurrency` now defaults to `os.cpu_count()` everywhere**, not
   just here — the clones and the eMarking downloads too. The `1` they
   defaulted to was a development convenience.

   ⚠️ Note that the per-request delay still applies *inside* the
   semaphore, so on a 20-core machine eMarking and materials can issue up
   to ~20 requests in flight. That is a long way from the
   "one-at-a-time" wording in `emarking-fetch-plan.md` §1, which exists
   because these are shared teaching servers. The delay is what keeps it
   civil; lower `-j` or raise `--delay` on anything that looks strained.
