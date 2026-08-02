# Universal Naming Convention

The single grammar for every file and folder name this system produces.
`name_assembler.py` implements it; `parse` is its exact inverse — never
hand-build or hand-parse names.

## Pattern

```
keyword-keyword[-more-keywords][-YYYY-MM-DD][-vNN|-draft|-final].ext
```

Folders: same pattern, no extension; version/status tags are generally not
applied to folders (their contents version, not the folder).

## Fields — fixed order, strict grammar

| Field | Rule |
|---|---|
| Keywords | 2–4 full, unabbreviated words describing content/purpose (never file type or source). Kebab-case, lowercase. Digits are legal inside a keyword (`1099`, `2023`) — a bare year is a **keyword**, not a date. Letters and digits of **any script** are legal (see Scripts below). |
| Date | Include **only for time-bound** items: invoices, statements, reports, receipts, exports, dated correspondence, meeting notes. Omit for evergreen items: reference docs, templates, guides, tools. Only an exact `YYYY-MM-DD` segment parses as the date. |
| Version/status | Include **only when a conceptual duplicate exists**. `vNN` zero-padded (`v01`, never `v1`) or semantic `draft`/`final`. Always the final segment before the extension. |
| Extension | Preserved from the original, lowercased. The tar pairs — `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.tar.zst`, `.tar.lz` — are one extension, not a `.tar` stem plus a `.gz`: use `fsorg_common.split_name`, never `Path.suffix`. |

## Scripts — a name keeps the language it was written in

Keywords may contain letters and digits from any script. `報告書 2024.txt`
becomes `報告書-2024.txt`, not `2024.txt`; `предложение.txt` keeps its word
rather than failing to produce one. The rule is that a name must stay
*searchable by the person who owns the file*, and stripping every
non-ASCII character deleted exactly the part they would search for.

Two qualifications:

- **Latin diacritics are folded**: `café` → `cafe`, `naïve` → `naive`,
  `über` → `uber`. Latin text is routinely typed and searched without its
  accents, so folding makes a name easier to reach, not harder.
- **Other scripts are never folded or transliterated.** Combining marks
  carry the word in Devanagari, Arabic, Hebrew and Thai — dropping them
  is not accent-folding, it is deleting letters. Greek keeps its tonos
  for the same reason.

The date, version, and status fields stay strictly ASCII: `2024-04-15`
never becomes `٢٠٢٤-٠٤-١٥`, because those fields are parsed, not read.

## Decision questions (answered per file during analysis)

1. **Evergreen or time-bound?** → decides the date field. When time-bound,
   source the date down this hierarchy — never skip a level:
   1. **Declared in the content** (invoice date, statement period, letter
      date). If the content holds several dates, use the document's own
      primary date — normally the issue date, not a due date or print date.
   2. **Embedded media metadata** — EXIF capture time for photos, ID3/MP4
      tags for audio/video. Written at capture; survives the copies and
      downloads that scramble filesystem dates.
   3. **A date stamped in the existing filename**
      (`IMG_20240415_093022.jpg`, `Screenshot 2026-01-05...`) — the device
      stamped it at creation.
   4. **Filesystem timestamps — last resort only**, legitimate when nothing
      else exists: screenshots without EXIF, exports/backups (the export
      moment IS the date), undated notes, unreadable binaries. Use
      `min(ctime, mtime)` (a copy refreshes ctime but keeps mtime, so the
      earlier one is closer to the truth) and mark the proposal
      low-confidence so the user knows the date is inferred, not read.
2. **Does a conceptual duplicate exist?** → decides version/status.
   - Byte-identical copies are NOT versioned: keep the newest and delete
     the rest. This is the only case in which a file is removed.
   - Revisions of the same conceptual file get `vNN` by content-confirmed
     recency (not mtime alone), and the **older copy is renamed too**.
     Every revision is kept — a shared filename is not a reason to drop
     one, since files that differ at all in content are versions of each
     other, not duplicates.
3. **What is it about?** → 2–4 keywords from content, not from the old name.

## Redundancy rule

Omit any segment already unambiguously conveyed by the containing folder
path — project names, role words (`tests`, `docs`, `src`), category terms.
Compute against the **destination** folder (post-plan), not the origin.
Include a category/project prefix only when the path does not identify it
(e.g. a loose file in `Downloads/`).

**The keyword count wins over this rule.** Redundancy stripping never
takes a name below 2 keywords: `[sec, 8k, investor, guide]` moving into
`sec-investor-guides/` would otherwise strip to `8k.pdf`, obeying
redundancy while breaking the grammar and leaving a name nobody can
search for. It becomes `sec-8k.pdf`.

`name_assembler.assemble` enforces the 2–4 count itself, after stripping
— it is the one implementation of the grammar, so both modes get this
without doing anything. Give it the keywords you mean; it will not emit
an off-grammar name. (`parse` is deliberately more permissive, since it
has to read pre-existing names that predate the convention.)

Examples: `fs-organizer/business-requirements.md` (not
`fs-organizer-business-requirements.md`); `tests/similarity-checker.py`
(not `similarity-checker-tests.py`).

## Windows safety (enforced by name_assembler)

- Reserved device stems (`con`, `nul`, `prn`, `com1`…`lpt9`) get `-file`
  appended.
- Stem capped at 60 chars; whole keyword segments are trimmed from the
  end, never the date/version fields.

## Collisions

Two **distinct** files must never receive the same name in one folder.
Prefer a semantic disambiguator (an extra distinguishing keyword from
content, e.g. the payer: `1099-acme.pdf` / `1099-globex.pdf`). Last
resort: `disambiguate()` appends a numeric keyword (`invoice-acme-2.pdf`).

## Saved-page asset folders

A saved web page's `X_files/` folder follows its html, not this grammar:
when the html is renamed to convention, the folder becomes
`<new-html-stem>_files` — the `_files` suffix is what keeps the pair
recognizable as a unit — and the html's internal references are rewritten
to match (`saved_page.py fix-refs`). The folder's *contents* are assets,
never renamed or treated as documents.

## Folder naming

- A folder's name = the common-denominator keywords of its **final**
  members' subjects (assigned bottom-up, after contents resolve), through
  the same grammar.
- A reused folder's name must stay accurate for **all** its post-add
  contents; rename it to a covering theme if not, and if no accurate
  common theme exists, the added file didn't belong there.
