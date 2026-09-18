"""Render the marks record as a self-contained HTML page.

A local reconstruction of eMarking's coursework-results page, written to
`<output_dir>/emarking-results.html` next to the downloaded files. Three
deliberate differences from the original:

1. **A dark/light toggle.** The real page is dark only; this remembers
   your choice in `localStorage`.
2. **The tabs are academic years**, not the original's sections — those
   were Modules Taken, Provisional Results, PT Meetings and so on, and
   coursework results are the only one of them we hold.
3. **Feedback links download the file we saved**, rather than pointing at
   an API we may no longer be able to reach. They're relative paths to
   the PDFs on disk, with a `download` attribute that renames them on the
   way out — every feedback file is served as `<username>.pdf`, so
   without that you'd get twenty identically-named downloads.

Pure: takes the record and a feedback lookup, returns a string. No I/O
and no network — the page embeds its own CSS and JS so it works offline
from `file://`, which is the only way it will ever be opened.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import quote

#: Colours of the status dot, keyed by the record's `colour` name. Sampled
#: to sit close to the original's palette in dark mode, and lightened for
#: the light theme where the dark greens and purples turn to mud.
_DOT_COLOURS = {
    "green": ("#1f5f45", "#2e8b62"),
    "purple": ("#5a2c60", "#8a4d93"),
    "brown": ("#6b3a1a", "#a4602c"),
    "grey": ("#3a3a3e", "#b9b9c0"),
}


def render_results(
    record: dict[str, Any],
    feedback_links: dict[tuple[str, str, int], str] | None = None,
) -> str:
    """The whole page, as one self-contained HTML string.

    `feedback_links` maps `(year, module_code, exercise_number)` to a path
    relative to the output directory. A missing entry just renders no
    link, which is what happens for an exercise with no feedback or one
    whose file wasn't downloaded.
    """
    links = feedback_links or {}
    years = sorted(record.get("years", {}), reverse=True)
    summary = record.get("summary", {})

    return _PAGE.format(
        title=_esc(f"Coursework Results — {record.get('username', '')}"),
        style=_STYLE.replace("{dots}", _dot_rules()),
        script=_SCRIPT,
        heading=_esc("Coursework Results"),
        subtitle=_esc(_subtitle(record, summary)),
        tabs="\n".join(_tab(year, index == 0) for index, year in enumerate(years)),
        panels="\n".join(
            _panel(year, record["years"][year], links, index == 0)
            for index, year in enumerate(years)
        ),
        legend=_legend(record.get("categories", [])),
        empty="" if years else '<p class="empty">No marked coursework found.</p>',
    )


def _dot_rules() -> str:
    """The per-colour dot rules, from the one table that defines them.

    Emitted rather than hand-written twice: the light theme needs a
    lighter shade of each, and a dot whose CSS class had no rule would
    render invisible — which is exactly the kind of thing nobody notices
    until the page is the only copy of the data left.
    """
    dark = "\n".join(f"  --dot-{name}: {shades[0]};" for name, shades in _DOT_COLOURS.items())
    light = "\n".join(f"  --dot-{name}: {shades[1]};" for name, shades in _DOT_COLOURS.items())
    classes = "\n".join(
        f".dot.{name} {{ background: var(--dot-{name}); }}" for name in _DOT_COLOURS
    )
    return f':root {{\n{dark}\n}}\n:root[data-theme="light"] {{\n{light}\n}}\n{classes}'


def _subtitle(record: dict[str, Any], summary: dict[str, Any]) -> str:
    parts = []
    if summary:
        parts.append(
            f"{summary.get('exercises', 0)} marked exercises across "
            f"{summary.get('modules', 0)} modules"
        )
    generated = record.get("generated_at")
    if generated:
        parts.append(f"archived {_date_only(generated)}")
    return " · ".join(parts)


def _tab(year: str, active: bool) -> str:
    return (
        f'<button class="tab{" active" if active else ""}" data-year="{_esc(year)}" '
        f'role="tab" aria-selected="{"true" if active else "false"}">'
        f"{_esc(_year_label(year))}</button>"
    )


def _panel(
    year: str,
    modules: list[dict[str, Any]],
    links: dict[tuple[str, str, int], str],
    active: bool,
) -> str:
    body = "\n".join(_module(year, module, links) for module in modules)
    hidden = "" if active else " hidden"
    return f'<section class="panel" data-year="{_esc(year)}"{hidden}>\n{body}\n</section>'


def _module(year: str, module: dict[str, Any], links: dict[tuple[str, str, int], str]) -> str:
    code = module.get("module_code", "")
    title = module.get("title")
    heading = f"{code}: {title}" if title else code
    rows = "\n".join(_row(year, code, exercise, links) for exercise in module["exercises"])
    return f"""      <h2>{_esc(heading)}</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="dot-col"><span class="sr-only">Status</span></th>
              <th>Exercise</th>
              <th>Deadline</th>
              <th class="centre">Submitted</th>
              <th class="right">Mark</th>
              <th class="right">Grade</th>
              <th>Feedback</th>
            </tr>
          </thead>
          <tbody>
{rows}
          </tbody>
        </table>
      </div>"""


def _row(
    year: str,
    module_code: str,
    exercise: dict[str, Any],
    links: dict[tuple[str, str, int], str],
) -> str:
    colour = exercise.get("colour", "grey")
    unassessed = "" if exercise.get("assessed", True) else " unassessed"
    tick = '<span class="tick" title="Submitted">&#10003;</span>' if exercise["submitted"] else ""
    mark = _mark(exercise)
    grade = exercise.get("grade") or ""
    feedback = _feedback(year, module_code, exercise, links)

    hint = _esc(exercise.get("category_label", ""))
    return f"""            <tr class="row{unassessed}" title="{hint}">
              <td class="dot-col"><span class="dot {_esc(colour)}"></span></td>
              <td class="exercise">{_esc(exercise.get("label", ""))}</td>
              <td class="deadline">{_esc(_deadline(exercise.get("deadline")))}</td>
              <td class="centre">{tick}</td>
              <td class="right mark">{_esc(mark)}</td>
              <td class="right grade">{_esc(grade)}</td>
              <td>{feedback}</td>
            </tr>"""


def _feedback(
    year: str,
    module_code: str,
    exercise: dict[str, Any],
    links: dict[tuple[str, str, int], str],
) -> str:
    if not exercise.get("has_feedback"):
        return ""
    path = links.get((year, module_code, exercise["number"]))
    if not path:
        # Feedback exists in eMarking but the file isn't on disk — say so
        # rather than offering a link that would 404 from file://.
        return '<span class="muted" title="Not downloaded">&mdash;</span>'

    # Exercise directories are named after lecturer-written titles, so a
    # `#` or `?` in one would otherwise truncate the URL at the fragment
    # or the query. `/` stays safe — this is a path, not one segment.
    href = quote(path, safe="/")

    # Feedback files are named after a *person*, not the exercise: ours
    # arrive as `<username>.pdf`, and a group exercise's after whichever
    # teammate it was distributed to. Without a `download` name you end
    # up with a pile of identically-named files.
    suffix = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else "pdf"
    name = f"{module_code}-{exercise['number']}-feedback.{suffix}"
    return f'<a href="{_esc(href)}" download="{_esc(name)}">View</a>'


def _mark(exercise: dict[str, Any]) -> str:
    mark, maximum = exercise.get("mark"), exercise.get("maximum_mark")
    if mark is None:
        return ""
    return f"{_number(mark)} / {_number(maximum)}" if maximum else _number(mark)


def _number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _legend(categories: list[dict[str, str]]) -> str:
    if not categories:
        return ""
    items = "\n".join(
        f'          <li><span class="dot {_esc(c["colour"])}"></span>{_esc(c["label"])}</li>'
        for c in categories
    )
    return f'      <ul class="legend">\n{items}\n      </ul>'


def _year_label(year: str) -> str:
    """`2425` → `2024–25`, which is how anyone actually refers to it."""
    if len(year) == 4 and year.isdigit():
        return f"20{year[:2]}–20{year[2:]}"
    return year


def _deadline(value: str | None) -> str:
    """`2024-11-06T19:00:00+00:00` → `Nov 6th, 7:00pm`, as the page shows it."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value

    day = moment.day
    # 11th/12th/13th are the exceptions the naive rule gets wrong.
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    date = f"{moment.strftime('%b')} {day}{suffix}"

    if moment.minute == 0 and moment.hour == 12:
        return f"{date}, 12:00noon"
    if moment.minute == 0 and moment.hour == 0:
        return f"{date}, 12:00midnight"
    hour = moment.hour % 12 or 12
    return f"{date}, {hour}:{moment.minute:02d}{'am' if moment.hour < 12 else 'pm'}"


def _date_only(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%-d %b %Y")
    except ValueError:
        return value


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


_STYLE = """
:root {
  color-scheme: dark;
  --bg: #0e0e10;
  --panel: #191a1d;
  --head: #232428;
  --line: #2e2f34;
  --text: #e9e9ec;
  --muted: #9a9aa3;
  --accent: #e8336d;
  --tick: #4caf6d;
  --link: #e9e9ec;
  --shadow: 0 1px 2px rgba(0, 0, 0, .4);
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #f6f6f8;
  --panel: #ffffff;
  --head: #f0f0f3;
  --line: #e0e0e6;
  --text: #1b1b1f;
  --muted: #6a6a74;
  --accent: #d81b60;
  --tick: #1f8a4c;
  --link: #1b1b1f;
  --shadow: 0 1px 3px rgba(0, 0, 0, .08);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 2rem 1.25rem 4rem;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
               sans-serif;
  font-size: 15px;
  line-height: 1.55;
}
.wrap { max-width: 1100px; margin: 0 auto; }
header { display: flex; align-items: flex-start; gap: 1rem; margin-bottom: 1.5rem; }
header h1 { font-size: 1.4rem; margin: 0 0 .2rem; }
header .sub { color: var(--muted); font-size: .85rem; }
header .spacer { flex: 1; }
#theme {
  background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 999px; padding: .45rem .9rem; cursor: pointer; font-size: .85rem;
}
#theme:hover { border-color: var(--accent); }
.tabs {
  display: flex; gap: .25rem; flex-wrap: wrap;
  border-bottom: 1px solid var(--line); margin-bottom: 1.25rem;
}
.tab {
  background: none; border: 0; border-bottom: 2px solid transparent;
  color: var(--muted); padding: .7rem 1rem; cursor: pointer;
  font-size: .95rem; font-weight: 500;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--text); border-bottom-color: var(--accent); }
.note {
  display: flex; gap: .6rem; align-items: flex-start;
  background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--accent);
  border-radius: 6px; padding: .8rem 1rem; margin-bottom: 1.25rem;
  color: var(--muted); font-size: .85rem;
}
.controls { display: flex; justify-content: flex-end; margin-bottom: 1rem; }
.controls label {
  display: flex; align-items: center; gap: .45rem; font-size: .9rem; cursor: pointer;
}
.controls input { accent-color: var(--accent); width: 1rem; height: 1rem; cursor: pointer; }
h2 { font-size: 1.05rem; margin: 1.75rem 0 .6rem; }
.table-wrap {
  overflow-x: auto; background: var(--panel);
  border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow);
}
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th {
  text-align: left; padding: .8rem 1rem; background: var(--head);
  font-weight: 600; white-space: nowrap; border-bottom: 1px solid var(--line);
}
td { padding: .7rem 1rem; border-top: 1px solid var(--line); }
tbody tr:first-child td { border-top: 0; }
tbody tr:hover { background: color-mix(in srgb, var(--head) 60%, transparent); }
.dot-col { width: 2.5rem; }
.dot {
  display: inline-block; width: 1.6rem; height: 1rem;
  border-radius: 999px; vertical-align: -1px;
}
.exercise { min-width: 18rem; }
.deadline { white-space: nowrap; color: var(--muted); }
.centre { text-align: center; }
.right { text-align: right; }
.mark { white-space: nowrap; font-variant-numeric: tabular-nums; }
.grade { font-weight: 700; }
.tick { color: var(--tick); font-weight: 700; }
.muted { color: var(--muted); }
a { color: var(--link); text-decoration: underline; text-underline-offset: 2px; }
a:hover { color: var(--accent); }
.legend {
  display: flex; flex-wrap: wrap; gap: 1.25rem; list-style: none;
  margin: 2rem 0 0; padding: 1rem; background: var(--panel);
  border: 1px solid var(--line); border-radius: 8px;
  font-size: .85rem; color: var(--muted);
}
{dots}
.legend li { display: flex; align-items: center; gap: .5rem; }
.empty { color: var(--muted); }
.sr-only {
  position: absolute; width: 1px; height: 1px; overflow: hidden;
  clip: rect(0 0 0 0); white-space: nowrap;
}
body.hide-unassessed .row.unassessed { display: none; }
@media (max-width: 640px) {
  body { padding: 1.25rem .75rem 3rem; }
  header { flex-wrap: wrap; }
}
"""

_SCRIPT = """
(function () {
  var root = document.documentElement;
  var button = document.getElementById('theme');

  function apply(theme) {
    root.setAttribute('data-theme', theme);
    button.textContent = theme === 'light' ? 'Dark mode' : 'Light mode';
  }
  apply(localStorage.getItem('emarking-theme') || 'dark');
  button.addEventListener('click', function () {
    var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    localStorage.setItem('emarking-theme', next);
    apply(next);
  });

  var tabs = [].slice.call(document.querySelectorAll('.tab'));
  var panels = [].slice.call(document.querySelectorAll('.panel'));
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      tabs.forEach(function (other) {
        var on = other === tab;
        other.classList.toggle('active', on);
        other.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.dataset.year !== tab.dataset.year;
      });
    });
  });

  var toggle = document.getElementById('unassessed');
  if (toggle) {
    toggle.addEventListener('change', function () {
      document.body.classList.toggle('hide-unassessed', !toggle.checked);
    });
  }
})();
"""

_PAGE = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>{heading}</h1>
      <div class="sub">{subtitle}</div>
    </div>
    <div class="spacer"></div>
    <button id="theme" type="button">Light mode</button>
  </header>

  <div class="note">
    <span>&#9432;</span>
    <span>A local archive of coursework results. Feedback links open the
    PDF saved alongside this page, so everything here keeps working
    offline and after the account is gone.</span>
  </div>

  <div class="tabs" role="tablist">
{tabs}
  </div>

  <div class="controls">
    <label><input type="checkbox" id="unassessed" checked> Show Unassessed</label>
  </div>

{empty}
{panels}
{legend}
</div>
<script>{script}</script>
</body>
</html>
"""


def feedback_links_from_manifest(manifest: dict[str, Any]) -> dict[tuple[str, str, int], str]:
    """Build the feedback lookup from `emarking-files.json`.

    Only entries that actually made it to disk get a link — a 403, a 404
    or a failure leaves the exercise showing a dash instead.
    """
    links: dict[tuple[str, str, int], str] = {}
    for entries in manifest.get("years", {}).values():
        for entry in entries:
            if entry.get("kind") != "feedback" or not entry.get("path"):
                continue
            if entry.get("status") not in ("downloaded", "cached"):
                continue
            # Manifest paths use the host separator; a URL wants forward
            # slashes, so a record written on Windows still links.
            links[(entry["year"], entry["module_code"], entry["number"])] = str(
                entry["path"]
            ).replace("\\", "/")
    return links
