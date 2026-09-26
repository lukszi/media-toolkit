# Running a long cleanup

**For the person running the work, not the person reading the code.** Nothing
here requires any of the three packages; where a command in this repository
implements a rule, the rule names it.

A library cleanup that is worth doing is too big to hold in one head and too
slow to finish in one sitting. It runs for days, across several passes, on
storage that punishes impatience, against a server that rewrites your work
while you are not looking. Much of the mechanical labour can be handed to
automation -- a script, a scheduled job, an assistant working from written
instructions -- and that changes the failure modes rather than removing them.
Automation is fast, tireless, and confidently wrong in ways a tired person is
not.

This document is the operating discipline that came out of one such cleanup.
It is eleven rules. Each of them was learned by having its absence cost
something.

---

## 1. One document is the state

Not your memory, not a chat log, not a folder of notes: **one file** that a
person or a process can read from the top and know what is true, what is in
flight, what was decided, and what happens next. Everything else -- logs,
evidence, plans, reports -- is referenced from it and is subordinate to it.

The document has a fixed shape, and the shape is what makes it usable:

| Section | Holds | Rewritten or appended |
|---|---|---|
| The standing criteria | what the work is *for*, in priority order | almost never |
| The environment | where things are, which versions, what the tools are | rewritten when it changes |
| The gotcha ledger | numbered findings (section 2) | append only |
| Current state | the counts and the snapshot they came from | rewritten |
| Completed work | what has happened, dated | append only |
| Corrections | claims that were wrong, and what replaced them | append only |
| The task list | everything outstanding, with its blocker | rewritten constantly |
| What remains -- start here | the top of the file for a new reader | rewritten constantly |
| Open questions | what needs a decision, and whose | rewritten |

Two of those need arguing for.

**Corrections is a section, not an edit.** When a claim turns out to be wrong,
you do not quietly fix the sentence. You leave the sentence, strike it, and
record what replaced it and when. A document that silently self-corrects
teaches its readers nothing about how reliable it is; a document with a
visible correction history tells you exactly how much to trust the parts that
have not been corrected yet. It is also the only defence against the failure
where two readers act on two different versions of the same claim.

**"Start here" is not a summary.** It is the section a reader with no context
reads first, and it is written for that reader: what is running right now,
what will break if they touch it, what needs a decision, who is away. It is
the most-rewritten part of the file and the one most worth the effort.

### The refresh discipline

The rule that makes it work: **append to the document after each completed
step, before starting the next one.** Not at the end of the day, not when it
is convenient. The step is not finished until the document says it is.

Three things follow.

- The status line at the top carries a timestamp, and the timestamp is part of
  the claim. "Quiet" without a time is not information.
- Anything long-running gets a line saying it is running, where its log is,
  and what to do if it is found dead. Written *when it starts*, not when it
  finishes -- the case you are writing for is the one where you are not there
  to write it.
- Before any planned interruption -- a handover, a break, an automated worker
  running out of context -- the document gets a deliberate refresh pass. This
  is the single highest-value habit in the list. A refresh takes ten minutes
  and costs nothing; reconstructing state from logs afterwards takes hours and
  is wrong.

Keep the document under version control with everything else, so that "what
did we think was true on Tuesday" has an answer.

---

## 2. The gotcha ledger

Every surprise gets a **number**, and the number is permanent.

A gotcha entry is five fields, always the same five:

1. **Symptom** -- what you saw, in the words you would have searched for.
2. **Cause** -- the mechanism, as far as you established it.
3. **Fix** -- what to do instead, as a recipe.
4. **How it was confirmed** -- the experiment. "I read the source" and "I saw
   it happen twice" are different grades of evidence and the entry says which.
5. **The version it was confirmed on** -- the server release, the tool
   version, the date. This is what turns an anecdote into a finding.

The numbering matters more than it looks. Numbers make gotchas *citable*: a
plan step can say "order is numbers, then refresh, then names, because of 12
and 3", and a reviewer can check that. Prose cross-references rot; numbers do
not. By the end of a long cleanup you will have dozens of them, they will be
the most valuable artefact the work produced, and several will be worth
publishing on their own.

Two rules keep the ledger honest. **Never renumber**, even when you merge two
entries -- strike one and point it at the other. And **an entry that turned
out to be wrong is corrected in place with its own correction note**, because
a wrong gotcha is worse than no gotcha: people act on it.

---

## 3. Nothing is deleted until its category is released

Not "nothing is deleted without asking". **Categories** are released, one at a
time, by the person who owns the data -- and then everything in that category
may be acted on without asking again.

This is the difference between a cleanup that finishes and one that stalls on
a hundred individual approvals. The unit of consent is a *kind* of thing:
folders with no media in them; source files whose content was already merged
into a keeper; byte-identical duplicates; originals that have been replaced
and verified. Each of those is a sentence the owner can actually evaluate.
"May I delete the files on this long list" is not.

The procedure around a release:

- Anything not in a released category gets a **manifest with evidence**, a
  report, and then it waits. It does not get deleted because it is obviously
  fine. Obviously-fine is how the one irreplaceable file goes.
- Evidence is gathered against the world, not against the manifest. Probe each
  candidate now; check the play state of every account on the server, not just
  yours; walk the folder for the things that travel with a media file and are
  easy to forget -- sidecars, artwork, generated preview data.
- **Prove the keeper plays.** Where one copy goes because another stays,
  read the payload of the one that stays -- a sampled read for zero-filled
  space, every packet against the container's duration, and a full decode --
  before anything moves. Headers are what a broken file keeps intact, and a
  comparison of headers prefers the broken copy whenever it claims the
  higher resolution. A check that could not run is a refusal, not a pass.
- **Park, never delete.** A released file is *moved* to a parking directory
  and left there. Space is reclaimed later, by hand, as its own decision. The
  tooling's job is to make deletion safe to decide, not to perform it.
- The audit log is append-only and records what was moved, from where, with
  what evidence, at what time.

`jfkit delete` implements this: it refuses to touch anything whose category
has not been released, checks every precondition against the server rather
than against the manifest it was handed, and parks.

---

## 4. Dry run, then apply

Every pass runs twice. The first run writes nothing and prints everything it
would do. You read that output -- actually read it, including the counts at
the bottom -- and only then run it again with an explicit flag.

The value is not that the dry run catches typos. It is that the dry run is a
**plan you can review, diff and keep**. Run it, save it, run it again an hour
later, and diff the two: anything that moved between them is something you did
not understand about your own library. That diff has caught more real problems
than the outputs themselves.

Three properties are worth insisting on in whatever tooling you use:

- The dry run is the **default**. Not a flag you remember. Writing is the
  thing you opt into, and there is no third state -- no "simulate", no
  "interactive", nothing that could be misread at four in the morning.
- Reading is free. A verb that only reads never asks for permission and never
  needs a flag, so there is never a reason to keep a dangerous flag in your
  shell history.
- The exit code means something. "It printed something" is not a signal a
  script can use.

Every writing command in this repository follows this and is tested for it:
the test calls the path with the default and asserts the file was not touched.

---

## 5. Stage elsewhere, swap in place

When you rebuild a file, build it **on different storage**, verify it there,
and then put it into the original's exact path.

Two separate reasons, both of which cost something to learn.

**Identity.** A catalogue's idea of an item is usually derived from its path.
Change the path -- or the extension -- and you have a new item: a new
identifier, no play state, no watched marks, no generated preview data, and
every piece of derived data keyed to the old identifier is now orphaned.
Putting the rebuild at the same path, byte-different but path-identical,
keeps all of it. This single rule is worth more than any other file-side
practice here.

**Blast radius.** A build that runs out of space, crashes, or turns out to be
wrong has not touched anything you cannot lose. The original is untouched
until the moment of the swap, and the swap itself is a move and a copy, not an
overwrite.

The swap procedure that survives contact:

1. Wait for the server to be idle -- nobody mid-episode. This is a check, not
   an assumption.
2. Stop the service. A live file watcher will notice your write half-way
   through and start reacting to a file that is not finished.
3. Work in **chunks sized by bytes, not by file count**, so the service's
   downtime is bounded by something you can predict.
4. Per file: *move* the original to the parking directory, copy the rebuild
   into the original's exact path, and **re-probe the result**. Never a size
   comparison -- a correctly rebuilt file legitimately changes size. On any
   failure, move the original back and stop.
5. Start the service, then ask for a **non-replacing** refresh so the
   catalogue picks up the new file without a metadata provider overwriting
   fields you spent two days fixing.
6. Check afterwards that the identifier, the play state and the name are what
   they were.

Moving a file between volumes -- extras filed on the wrong disk, a folder
that belongs in another library -- is a copy followed by a delete. Copy it to
a staging folder on the destination volume, hash what arrived from the
destination side, rename it into place, and only then remove the source
(`mkvkit copy --move`). A read straight after the write usually comes from
the operating system's cache, so on a disk you doubt, read it again later.

`mkvkit swap` does the file half; `jfkit swap` does it with the service and
the refresh around it.

---

## 6. Verify both sides, inside the final container

**"The command exited 0" is not verification.** Neither is "the file plays".

Collect stream-level evidence from the original and from the result, and
compare the two against a **declared** expected difference -- "two audio
tracks removed and nothing else", "one track appended and nothing else",
"header fields changed and no payload". Anything outside the declared
difference is a failure, including things you would have shrugged at.

The core of the evidence is a **payload hash per stream**, taken from the
finished container. Not a hash of the file (a rebuild changes the file), and
not a hash of an intermediate (the intermediate is not what you are shipping).
The point of hashing per stream is that it answers the one question that
matters -- did any kept content change -- with a yes or a no, and it does so
even when everything around it moved.

Around the hashes: the track table with every flag and identifier, the chapter
list compared as times and names rather than as raw markup, attachments,
container-level fields, and the top-level container structure.

Two disciplines make this stick:

- **Cosmetic differences are recorded with the reason they are cosmetic**,
  never silently allowed. "Identifiers were regenerated, nothing outside the
  file references them" is a note in the report. An allowlist with no
  reasoning attached becomes, within a week, an allowlist that hides a real
  failure.
- **Know what cannot have changed and skip measuring it.** After a header-only
  edit, the payload provably cannot have moved, so you refresh the header
  evidence and keep the hashes. That is not a shortcut; it is the difference
  between a verification pass that runs and one that is too slow to run.

For anything measured rather than compared -- an offset, a drift, a delay --
run **known-answer controls first**: a thing against itself must read zero, a
deliberately shifted pair must return the shift it was given, and the same
pair measured from two different starting points must agree. A measurement
pipeline that has never been asked a question whose answer was written down in
advance is a number generator that has not been caught yet.

`mkvkit verify` and `dubalign controls` are these two paragraphs as code.

---

## 7. One heavy reader per device

Mechanical storage serves one sequential reader well and two *badly* -- not
half as well, badly, because the head spends its time travelling instead of
reading. A read that takes seconds alone can take minutes in company. A
few readers can make the machine stop responding.

So: **one heavy reader per device, and a gate that enforces it** rather than a
convention that everyone remembers until they are in a hurry.

What the gate has to get right, all of which was learned the hard way:

- Resolve paths to a **device** before comparing them. Never key the decision
  on a string another program formatted for its own output.
- **The server is a reader too, and it does not announce itself.** After any
  pass that changes a lot of modification times, a media server will spend
  hours regenerating preview images and thumbnails for every touched file --
  on demand, outside any scheduled task. This is the single most common cause
  of "why is the disk busy, nothing is running". Look for the decoding and
  muxing programs by name, whoever started them, and for the server's own
  scheduled tasks.
- A lane must not see **itself**. Tag your own workers and exclude them, or
  the job starts, sees the reader it just started, and waits forever -- which
  looks exactly like a gate that is working.
- Unknown storage is treated as needing protection. A gate that assumes "not
  spinning" because it could not find out is a gate that does the damage.
- Pack each device's queue **largest first**. A queue worked in discovery
  order finishes with its biggest item running alone for an hour while every
  other lane sits idle.

And the scheduling rule that follows from all of it: **do not fight the server
for its own disk.** If its indexing or preview job is running on the device
you want, either wait for it or ask the owner whether to pause it. Starting a
heavy read beside it does not get your work done sooner; it gets both jobs
done later and makes the machine unusable in between.

`jfkit jobs gate` and `jfkit jobs lanes` implement this;
`docs/patterns/spindle-gate.md` is the long version.

---

## 8. Detached jobs and monitors

Anything that will run longer than about ten minutes **does not run in your
session**. It runs as a detached job owned by the operating system, writing to
a file, and your session watches the file.

This is not about convenience. A long job tied to an interactive session dies
when the session does, and sessions die for reasons that have nothing to do
with the work -- a disconnect, a restart, an automated worker reaching the end
of its context. Everything about the job that matters must survive that.

Concretely:

- **Resumable, always.** Write results as one record per line as they are
  produced, and start by reading back what is already there. A job that can
  only be run from the beginning is a job that *gets* run from the beginning
  after every interruption, and long jobs get interrupted.
- **One log per job, named for the job**, in a known place, with timestamps.
  The first thing a fresh reader does is read that file.
- **A monitor, not a poll loop.** Something small that watches for the job to
  finish or die and reports once. Do not burn an interactive session watching
  a progress bar, and do not check every thirty seconds "just to be sure" --
  on a gated device, your check is a reader too.
- **Write down how to tell whether it is alive**, in the state document, at
  the moment you start it. The reader who needs that sentence is by definition
  the reader who cannot ask you.

---

## 9. One log per pass, kept

Every pass writes into its own directory: the plan it was given, the dry-run
output, the apply log, the verification table, the rollback artefact, and a
short report. Kept afterwards, not cleaned up.

Two returns on this, and the second is the bigger one.

The obvious one is recovery: the rollback table is what undoes a pass that
should not have run, and a header edit or a metadata write without one is a
change you cannot reverse.

The less obvious one is that the logs are **the only honest record of what
actually happened**. Reports are written from intent; logs are written from
events. Every serious correction in the cleanup this came from was found by
someone reading a log line that disagreed with a report -- a count that was
off, a file that was skipped, a step that ran twice. Keep them and
the next audit is cheap. Discard them and every claim in your state document
is unfalsifiable, which is the same thing as being untrustworthy.

Reports carry their scope, their generation time and their caveats **inside
the document**. A survey separated from its caveats is how a number measured
on one collection becomes a universal claim three weeks later.

---

## 10. What a person decides, what automation decides

The line is not "big things" and "small things". It is **reversibility and
taste**.

**A person decides:**

- Anything that removes data, even when the evidence is complete. Releasing a
  category (section 3) is a person's sentence.
- Anything irreversible in practice: identifier changes, mass renames,
  anything that re-derives data that takes many hours to rebuild.
- Anything about *preference* rather than correctness -- which of two
  acceptable versions to keep, whether a cosmetic inconsistency is worth a
  write, what "correct" means for an edge case the rules do not cover.
- Anything that needs a sense for the material: whether an odd-sounding result
  is a fault or is simply how that item is.
- Whether to interrupt something of the server's own.

**Automation decides:**

- Everything mechanical, exhaustive and checkable: enumerate, probe, measure,
  compare, build the plan, run the plan, verify the result, write the report.
- Whether a candidate passes a *stated* bar -- provided the bar was written
  down before the data was looked at.
- What to do when a rule fires: refuse, record, and move on.

Three habits keep the line where it belongs.

**Refusal is a result.** The correct output for an uncertain case is "refused,
because", recorded in the report, not a best guess. A confidently wrong value
is worse than a placeholder, because nobody checks it. Where a whole item has
any one component graded wrong, hold the whole item back -- an item left alone
costs only the improvement it did not get, while an item damaged in one place
costs a restoration.

**Report faithfully, including what was not done.** A pass that did most of
its list reports exactly how much, and lists the rest with reasons. The temptation to
round up is the single most damaging habit available to anything -- or anyone
-- working unsupervised, because it destroys the only thing that makes
delegation cheaper than doing it yourself: the ability to believe the report
without re-doing the work.

**Ask once, in a batch, at a natural boundary.** Collect the decisions a pass
will need, present them together with the evidence, and then run. A stream of
individual questions costs more of the owner's attention than the work saved,
and it trains them to answer without reading.

---

## 11. Handing the job to a fresh context

Assume the next person to touch this work knows nothing, has not read the
conversation, and cannot ask you anything. That reader is sometimes a
colleague, sometimes you in three weeks, and increasingly often a fresh
automated worker with an empty context window. Write for them, always.

The handover is exactly this: **the state document, and nothing else.** If it
is not in the file, it does not exist. A fresh reader should be able to open
it, read "start here", and continue -- without archaeology, without a chat
log, without a phone call.

What that means in practice:

- **Name paths, files and identifiers, not "the script I mentioned".** Every
  reference must be resolvable from the file alone.
- **Record decisions with their reason and their date.** Six weeks later,
  "keep both copies" without a reason will be re-litigated by someone who
  assumes it was an oversight.
- **Record the reasons for *not* doing things** with as much care as the
  things you did. This is the part everyone skips, and it is the part that
  stops the next reader from cheerfully redoing a rejected idea.
- **Refresh before you run out of room.** Any worker with a finite context
  should treat the state document as the thing it flushes to before it is
  full, deliberately, while it still remembers why.
- **Keep the standing rules at the top**, in priority order, phrased as
  constraints rather than as goals. "Never leave an item without a usable
  audio track" survives a handover; "make the library good" does not.

A sound test for the document: hand it to someone who was not there and ask
them what is running right now and what they must not touch. If they cannot
answer from the file in two minutes, the file is not doing its job.

---

## What this does not solve

- **It does not make an unsupervised pass safe.** It makes an unsupervised
  pass *reviewable*. Those are different, and the difference is that somebody
  still has to read the report.
- **The gate protects devices, not the machine.** It says who is reading, not
  how busy the storage is, and it cannot see another machine reading the same
  share.
- **A numbered ledger goes stale.** Every entry is pinned to a version, and a
  server upgrade invalidates an unknown subset. Re-confirming them is work
  nobody wants to do and there is no shortcut.
- **None of it substitutes for the owner's taste.** The rules above decide
  what is *safe*. They do not decide what is *wanted*, and a cleanup that gets
  that wrong is merely a well-documented disappointment.
