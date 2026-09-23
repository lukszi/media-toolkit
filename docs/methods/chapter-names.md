# Chapter names that describe a different scene

*Implemented by `mkvkit.chapters.sources`, `mkvkit.chapters.grid`,
`mkvkit.chapters.verify`, `mkvkit.chapters.names`, `mkvkit.chapters.windows`,
`mkvkit.chapters.selfcheck` and `mkvkit.plan`; on the command line by
`mkvkit chapters classify | match | windows | selfcheck | plan | apply`.*

---

## 1. The finding

**A chapter list whose timestamps match your cut proves the marks fit. It
proves nothing at all about the names.**

That sentence is the whole of it, and it is not obvious. The usual way to
decide whether a published chapter list belongs to your copy of a film is to
compare its timestamps against the marks your file already has. If every
timestamp agrees with its mark to within a couple of seconds, the list is
for your cut — no other cut of that film would line up that way. It is a good
test and it is correct.

The names are a separate claim, and nothing about the timestamps supports it.

### The symptom

An illustrative case, told with the invented cast: *The Quiet Harbour (1978)*,
a feature-length film with a dozen marks. A published list matches the grid
with a worst deviation under two seconds — an excellent match, far better than
the tolerance. Its names are then read against the film.

**Half of them describe a scene a couple of chapters away.** A name reading
`The Harbourmaster's Apology` sits on the storm at the harbour mouth; the
apology happens two chapters later, where the list offers `Setting Out at
Dawn`. A name reading `The Storm Breaks` sits on the quietest scene in the
film.

The marks are right. The list is for this cut. The names were typed
against *something else* — an insert, a menu, a different edition's chapter
list, a scan of the back of a box — and never aligned to the marks the same
contributor entered.

### Why it is easy to miss

Every property a person checks by eye looks fine. The count is right. The
spacing is right. The names are real names, in plausible language, in an order
that reads like a film. Nothing in the file, and nothing in the list, is
malformed. The only way to notice is to compare a name against *what is
actually on screen at its own mark*, which is the one thing nobody does when
the arithmetic has already said yes.

And it matters, because a chapter name goes into the file's header and is then
shown to whoever is scrubbing through it. A wrong name is worse than no name:
`Chapter 7` tells a viewer nothing, and a confident description of the wrong
scene tells them something false. The rule this whole area is built on follows
from that asymmetry — **refusing to write a name costs nothing but the name.**

---

## 2. How it is detected

Three pieces of evidence, in ascending order of cost. The first two settle the
question on their own when they fire.

### 2.1 Slide the list against the content — `verify.shift_score()`

Take twenty seconds of the film's **original-language** audio from each mark
and transcribe it. Then score the content words of name *i* against the
transcript of mark *i+k*, for *k* from −4 to +4.

```python
from mkvkit.chapters.verify import shift_score

scores = shift_score(candidate.names, [e.transcript for e in evidence])
print(scores)            # e.g. hit rate by offset: -4:0.000 ... 0:0.020 ... +2:0.300
scores.misaligned(threshold=0.06)   # True
```

A list typed against its own marks scores highest at *k* = 0. A list typed
against a different set of marks scores highest at whatever offset recovers
the original pairing, and the difference is stark rather than marginal. **That
is the signature**, and it is the cheapest reliable thing in this document:
one number, computed from text, no judgement anywhere in it.

Three cautions are built into the implementation.

- **The absolute score is weak evidence.** Plenty of perfectly good disc names
  share no word at all with the dialogue at their own mark — a name can
  describe a place, an object or an action nobody says out loud.
  `ShiftScores.strength` reports `strong`, `fair` or `weak` precisely so that
  a weak score is not mistaken for an accusation.
- **A name with no content words cannot be scored**, and that is different
  from scoring zero. `hit_rate()` returns `None` there, and those names are
  left out of the average rather than counted as misses.
- **An offset near the end of the range has fewer names left inside the
  list.** Two names agreeing perfectly is not a better answer than forty names
  agreeing well, so an offset that cannot be scored on at least half the
  list — and on at least three pairs — is not scored at all. Without that
  rule the extreme offsets win spuriously on short lists.

### 2.2 Ask whether anybody else published the same list — `verify.corroborate()`

Archives of this kind usually hold more than one entry per title, contributed
by different people years apart. If a second entry's grid also matches your
marks *and* its names are in the same order, two strangers agreed
independently, and no transcript is going to beat that as evidence.

It catches the other direction too, and this one is worth the function on its
own: a list that is **identical to one published for a different cut** — down
to a shared spelling mistake — is the archive's list rather than this film's,
and its marks agreeing with yours means nothing. It happens: a list can turn
out to be character-for-character the list published for a different film in
the same series, typo included.

### 2.3 Read the mark — `verify.collect_evidence()` and `verify.call_marks()`

The expensive one, and the only one that can catch a single displaced name
rather than a displaced list.

Per mark: twenty seconds of original-language audio from the mark, and one
frame three seconds after it. **Both come out of a single seek**
(`SingleSeekEvidence`), because two seeks per mark doubles the cost of the
entire pass and the seeks are the entire cost, especially on rotating storage
with a media server also reading the same disk.

`call_marks()` then implements the reproducible half of the rubric:

| call | when |
|---|---|
| `PLAUSIBLE` | something the name says is said in its own window, or it is a structural name (`Main Titles`, `End Credits`) sitting where such a name belongs |
| `WRONG` | nothing the name says is in its own window, and the name turns up *whole* in a different one — it describes a scene that starts somewhere else |
| `UNCLEAR` | everything else: a silent window, a name too generic to check, a name nothing supports and nothing contradicts |

`UNCLEAR` is the default, not the exception. **A vague, poetic or
end-of-chapter name is never called wrong**: disc authors write those
deliberately, and condemning them would hold back lists that are perfectly
usable. The bar for `WRONG` rises for short names — a two-word name sharing
one ordinary word with another window is half of nothing — which is
`better_elsewhere()`'s `short_name_words` rule.

The frame grab is for a person. Nothing in this package looks at a picture;
the frames exist so that a reader can settle the marks where the transcript
cannot, and a reader's calls can be passed straight into `verdict()`.

---

## 3. What to do about it

### 3.1 The verdict, and who computes it — `verify.verdict()`

Three outcomes per film, recomputed centrally from the per-mark calls rather
than taken from whoever made them:

* **ALIGNED** — no contradiction, at least 60 % of the marks checkable, at
  least 80 % of those plausible. Only this is eligible to be written.
* **MISALIGNED** — two or more contradictions, *or* a shift score that clearly
  prefers another offset.
* **UNCERTAIN** — everything else. Too little dialogue to judge, or a single
  odd name.

Every number is in `Rubric` and every one of them is a policy. The central
recomputation matters more than it looks: a rubric sharpened halfway through a
pass can be re-applied to the calls already collected without re-reading a
single file.

In practice **a list that matches its grid is not always describing the marks
it matched**, and the failures vary: the wrong film's list entirely, a clean
one-chapter swap in the middle, or an entire act's worth of chapters scattered
across other marks.

### 3.2 Never slide the list into place — `names.match_names()`

The tempting response to a clean signal at *k* = +2 is to shift the names by
two and write them. Do not. It is wrong twice over:

1. The offset that scores best is a measurement with no error bar. It is
   strong evidence that *something* is displaced and weak evidence about
   exactly what.
2. A list that needs shifting is a list somebody typed against different
   marks. Nothing guarantees it is a rotation of the right list rather than a
   different list altogether — and in the case above, it was a different
   list altogether.

`match_names()` therefore refuses the whole film and reports the offset. The
cost of that refusal is some chapter names; the cost of the alternative is a
file edit somebody has to find again later.

### 3.3 Write the file's own marks, always — `grid.copy_names()`

Where a list is accepted, the times written are the file's own and the names
are the only thing taken. This is what makes the operation safe: the worst
outcome that survives every gate is a wrong name on a mark that has not moved.

The converse case — a file with **no** marks, where the candidate supplies
both times and names — has no grid to check against and is therefore a
different and riskier job. `sources.classify()` names the three jobs, and
`Classification.needs_a_person` is set for that one.

| job | the file has | what is taken | what stands behind it |
|---|---|---|---|
| `times-and-names` | no marks | both | only a runtime, which is not evidence — refused outright for a rate-converted candidate |
| `names-only` | marks the candidate agrees with | names | the grid agreement, plus everything in §2 |
| `times-only` | either | nothing | the candidate carries no name worth the word; parked |

### 3.4 Say so in the file — `names.provenance()`

A name somebody typed off a disc and a name a program produced look identical
in a player and are worth very different amounts. The only moment the
difference is known is the moment they are written, so it is recorded then, as
a tag: `CHAPTER_NAMES_SOURCE`, with a date and the program that wrote it. The
tag is **merged** into whatever the file already carries
(`mkvkit.plan.chapter_names_plan()`), never substituted — one of the tags a
file may already carry is a track language that is the only thing making the
file read correctly.

---

## 4. Writing names where no list exists

Everything above is about names somebody else wrote. Where there is no list at
all, the names have to come from the film. This section is the method for
that, and it comes with an honest limit stated at the end.

### 4.1 The window — `windows.build_windows()`

A window is everything said between one mark and the next: subtitle cues, or a
transcript of the audio. It is the only material a name is written from and
the only material a name is checked against.

**Size the window by how long the chapter is.** A flat character cap is fine
for a five-minute chapter and wrong for a half-hour one, and the
obvious way of enforcing one — keep the head and the tail, elide the middle —
removes exactly the part of a long chapter a name should describe. That is not
hypothetical: in the original trial, a long chapter was named from its opening
and its closing small talk while the elided middle held the sequence the
chapter is actually about, and the window was at fault rather than the namer.

So `budget_for()` scales the budget with the chapter's own duration inside a
floor and a ceiling, and when text has to be dropped `build_windows()` drops
it **evenly across the whole span**: the chapter is cut into slots, each slot
gives up the same share, and `[...]` marks each elision.

**A cue that crosses a mark is split at the mark** (`transcripts.clip()`), its
words spread across its own span in proportion to time. A transcriber emits
long segments — twenty or thirty seconds as a single block, and neither a
voice-activity setting nor a chunk length shortens them; asking for word
timestamps and re-cutting the words into short cues does
(`transcripts.cues_from_words()`). Without both, one chapter's opening
dialogue lands in the previous chapter's evidence.

**No absolute timestamp appears in a rendered window set**
(`WindowSet.render()`). Names come back per window *number*, the marks are
carried straight through from the file, and a name carrying a timecode can
therefore only have come from one that was shown — which makes that rule
checkable rather than hopeful.

**Refuse thin material rather than describing it** (`windows.refusals()`). A
transcript that is a stub, or that stops two thirds of the way through the
film, produces windows that look perfectly well-formed and are empty where it
matters — and an empty window reads as "nothing is said here", which is a rule
that fires and writes a name. Under sixty cues for a whole feature, or
coverage stopping short of 55 % of the runtime, and the film is skipped with
the reason recorded.

### 4.2 The rules — `names.check_name()` and `names.NamingRules`

Nine rules, and the first two are the ones that earn their place.

1. **The language of the names follows the film, not the transcript.** Where
   the material is in one language and the names belong in another, describe
   the scene in the target language; never transliterate, never quote the
   foreign line. `WindowSet.names_language` carries this and
   `check_name(..., language=...)` checks what it can of it.
2. **A chapter is named for what it *opens* with, not for its climax.** This is
   the rule that gets broken. When a window opens quietly and ends
   dramatically, the pull towards the ending is strong and the result is both
   wrong and a spoiler. Weight the first third.
3. **No spoilers past the mark.** The name is for somebody about to watch
   forward from it.
4. **No dialogue in the window is mechanical, never a judgement**
   (`names.blank_for()`): the last chapter is the closing credits; a short
   first chapter is the title sequence; **everywhere else the answer is the
   empty string** and the mark keeps the label a player shows. Inventing a
   name from nothing is the failure this rule exists to prevent.
5. **Song lyrics are not a name** unless the musical number *is* the scene.
6. **Nothing from outside the window.** No fact, character or place that is
   not in the text in front of you.
7. **Form**: a noun phrase, title case, 40 characters or fewer, no trailing
   punctuation, no chapter number, no quotation marks around the whole thing,
   and never a timecode.
8. **A quote is a name only when it is the chapter's subject**, not when it is
   the best line in it.
9. **When the window does not tell you what the scene is, write nothing.**
   Blank is always allowed and always safe.

The checks come in two strengths, which matters. A few are **blocking**: a
"name" that is a timecode, a bare label in any language, or that carries a
byte a decoder could not read is not a name at all. The rest — length,
trailing punctuation, quotation marks, a chapter number, the language — are
**advisory** for a name off a disc, because disc authors break them routinely
and their names are still worth having, and **blocking** under `strict=True`
for a name a program has just written, which is how `selfcheck` runs them.

The blocking set is there because of a real regression: a pass whose only test
was "is the string non-empty" wrote lists of pure labels into files — raw
timecodes in one, `Chapter One`, `Chapter Two` and so on in another, and the
same thing in other languages — each of which a player
then displayed *instead of* the label it would have generated itself.
`is_generic_name()` is what does that counting, and the list of words it knows
grows whenever a list turns up spelling it another way.

### 4.3 The self-check — `selfcheck.grade_names()`

A second pass over names that have just been written, against the windows they
were written from, because the first pass is confident and the failures are
quiet.

| grade | meaning |
|---|---|
| `good` | something the name says is said near the *start* of its own window |
| `summary-ish` | accurate as far as anything can tell, and not much use: vague, or supported only by the end of the window |
| `wrong` | breaks a rule, repeats another chapter's name, sits out of order, or describes a scene that starts at a different mark |
| `rule-fired` | a correct mechanical result: the structural name where one belongs, or a blank |

Five mechanical checks run under those grades: **length**, **duplicates** (the
same name on two marks), **order words** (a closing name away from the end, an
opening name away from the opening), **language**, and **coverage** — the
share of dialogue-bearing windows that got a name at all, because a pass whose
every name is fine and which named a third of the film has not done its job.

The climax reach from rule 2 has a mechanical shadow and is caught: where a
name's only support sits past two thirds of the way through its own window,
the name describes the end of the chapter rather than its beginning, and it is
graded `summary-ish` with that reason. The other failure the trial found —
outside knowledge, a character or place that never appears in the window —
has no mechanical shadow and is **not** claimed: a name nothing supports is
graded `summary-ish` with the reason given, and a reader can look.

**Any `wrong` grade holds the whole film back** — not the chapter, the film.
The names go into the header together, a pass is judged by whether anybody has
to go back through it, and a film left alone costs nothing but the names it
did not get. `summarise()` applies that bar, and applies it in one place
whoever produced the grades, so a reader's grades and the module's go through
the same arithmetic.

### 4.4 The limit, stated plainly

**There is no namer in this package, and there is not going to be one here.**
The writing step in the original work was a person reading the windows, not a
program. What ships is the window builder, the rules, the self-check and the
verifier. `mkvkit chapters` has no `name` verb; if you want one, write the
names yourself or have your own tool write them, and then run `selfcheck` over
the result.

One observation from that trial, not a rate: films whose dialogue states what
they are doing name themselves well; films that show rather than tell do not.

---

## 5. What is not implemented

* **No namer.** See above.
* **No transcriber.** `SingleSeekEvidence` takes one; supplying it is the
  caller's job, and the whole judging half runs from a recorded evidence file
  with no model present at all.
* **Nothing here finds subtitle files.** The window builder takes cues. A
  warning from experience: matching subtitle sidecars to films by walking
  directories is more dangerous than it looks — where films sit loose in one
  directory, a two-level walk can hand a single subtitle file to dozens of
  unrelated films, and most apparent matches are false. Require a
  stem match in the same directory, or a folder that plainly belongs to that
  one film, and treat a sidecar claimed by two films as an error.
* **The per-mark call does not look at the picture.** The frames are collected
  for a person.
* **The bundled archive adapter is a scraper, not an API client.** It ships
  disabled, caches on disk, rate-limits itself and identifies itself, and it
  must be enabled deliberately — `sources.ChapterDatabase(enabled=True)` or
  the equivalent switch. Whether to enable it against somebody else's server
  is your decision to make, not a default to inherit.
