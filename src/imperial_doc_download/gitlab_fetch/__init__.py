"""Fetch data from Imperial DoC's self-hosted GitLab (gitlab.doc.ic.ac.uk).

Takes the repository list produced by `labts_fetch` and clones every
repository into `<output_dir>/<academic_year>/gitlab/<repo>/`, falling back
to the firewalled gitolite server (proxy-jumped through a DoC shell
server) for repositories GitLab no longer serves.

Needs `IMPERIAL_GITLAB_SSH_KEY`, plus `IMPERIAL_DOC_SSH_KEY` and
`IMPERIAL_USERNAME` for the gitolite fallback. The user's `~/.ssh/config`
is never read or written — see `ssh.py`.
"""

from __future__ import annotations

from imperial_doc_download.gitlab_fetch.step import GitlabFetchStep

__all__ = ["GitlabFetchStep"]
