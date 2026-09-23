# Auditing the document you are about to trust

**This is the one document in `docs/methods/` that names no implementing
module, because it does not have one.** It is a procedure carried out by
people and by automation working from written instructions, not a function you
can call. It is filed here rather than thrown away because it is a method, it
was run, and it caught things.

Its companion is `docs/operating-playbook.md`, section 1: a long cleanup keeps
its state in one document, and every reader after the first acts on that
document instead of on the system. This is how you find out whether the
document deserves it.

---

## The problem

A state document is believed in proportion to how well it is written, which is
uncorrelated with whether it is true.

Three ways it drifts, all of them invisible from inside:

- **A claim was true and expired.** Counts, free space, "the service is
  stopped", "that task is disabled". Nothing marks them stale.
- **A claim was never checked.** A plausible inference written down once, then
  quoted, then relied on. The sentence gets more confident each time it is
  paraphrased.
- **A claim is a summary of something that was true in a narrower case.**
  "Those items are inert" meaning *the three I looked at did nothing*.

Proof-reading does not find any of these. A reviewer reading for sense will
agree with a well-written false sentence, and a reviewer who wrote the
document will agree with all of them. The only thing that finds them is going
back to the system and trying to make the sentence fail.

---

## The method

**Reviewers are not asked to review. They are asked to refute.**

The task given to each reviewer is adversarial and is phrased that way: *here
are N claims; your job is to prove them wrong against the live system; a claim
you cannot break is one you failed to break, not one you endorsed.* This
wording is not theatre. A reviewer asked to "check" a document returns a list
of typographical corrections; the same reviewer asked to break it queries the
system.

### 1. Extract the claims

Split the document into atomic, checkable claims. Atomic matters: "the
libraries are configured identically and one of them is a different type" is
two claims, and in practice one of them is wrong and one is right.

Anything that cannot be phrased as a checkable claim is itself a finding.
Mark it and move on -- an unfalsifiable sentence in a state document is a
sentence that will be relied upon and can never be found wrong.

### 2. Fan out to independent reviewers

Several reviewers, each taking a slice, **none of whom wrote the document**
and none of whom sees another's results while working. Independence is the
whole mechanism: correlated reviewers reproduce the author's assumptions, and
three reviewers who agree because they share a premise look exactly like
three reviewers who agree because the claim is true.

Two rules:

- **Evidence comes from the system, not from the document.** Querying the
  server, reading the database snapshot, probing the file, reading the
  upstream source. Quoting a different part of the same document is not
  evidence, and the temptation to do it is strong because it is fast.
- **Read-only.** An audit that changes state is an audit whose findings cannot
  be reproduced. Reviewers get read access and nothing else.

### 3. Arbitrate the contested claims

Every claim a reviewer contests goes to a second, independent pass that sees
the claim, the evidence offered against it, and nothing about who produced it.
Most disagreements die here: a reviewer misread a query, or measured the right
thing on the wrong item.

This step exists because adversarial reviewers over-report. Told to break
things, they break things that are not broken, and without arbitration the
audit's output is noise with three real findings buried in it.

### 4. Attack the document's own structure

A handful of passes that target the document rather than its claims. These
ask a different question -- not "is this sentence true" but "what would this
document let a new reader do wrong":

- Which claims are load-bearing? Which steps would a reader take *because of*
  this sentence?
- Which sentences read as instructions but are actually observations?
- Where does the document say "verified" without saying how?
- What is missing that a reader would assume? An absent warning is invisible
  to a reviewer checking the sentences that are present, and is the most
  expensive defect class in a state document.

### 5. Judge

Two independent judges rule on what survives arbitration, on the same rubric,
without seeing each other's rulings. Where the judges disagree, the claim is
recorded as disputed and escalated to the owner. Disagreement is information:
a claim two careful readers cannot agree on is a claim the document states
badly, whatever the underlying truth is.

### 6. Write the corrections back

The output of the audit is **edits to the document**, not a report about the
document. A finding that is not in the state document within the hour is a
finding that will be re-discovered by the next audit.

Each correction carries the corrected statement, the evidence, and the date --
in the document's corrections section, visibly, rather than as a silent
rewrite. Readers calibrate their trust on the correction history.

---

## The rubric

Every contested claim gets a verdict and a severity, and both are needed: a
refuted claim nobody acts on is a footnote, and a partially-true claim
somebody is about to act on is an emergency.

**Verdict:**

| | |
|---|---|
| **CONFIRMED** | the evidence supports the claim as written |
| **PARTIAL** | true in the case it was observed in, stated too broadly; or true with a condition the document does not give |
| **REFUTED** | the evidence contradicts the claim |

`PARTIAL` is the verdict that earns the method its cost. Outright false
statements are rare and are usually caught by someone tripping over them.
Over-general statements are common, survive indefinitely, and are exactly what
a reader in a hurry acts on.

**Severity:**

| | |
|---|---|
| **cosmetic** | wrong, and nothing follows from it |
| **matters** | a reader would draw a wrong conclusion, and would notice |
| **would-cause-damage** | a reader acting on this would lose or corrupt something |

Severity is assessed **against the actions the document invites**, not against
the size of the error. A count that is off by three is cosmetic in one section
and would-cause-damage in another, if the second is the section that decides
what gets deleted.

**Every contested claim leaves with a corrected statement**, including the
confirmed ones where the wording was the problem. "This is wrong" is half a
finding; the audit is not done until the replacement sentence exists.

---

## What one run looked like

One document, mid-project, large enough that no reader held all of it, put
through every step above: many reviewer results, an arbitration round, a
handful of structural attacks and two judges. The verdict mix is the finding in miniature -- outright
refutations were rare, and `PARTIAL` was the most common verdict on a
contested claim by a wide margin. Almost nothing in the document was false.
A great deal of it was true in a narrower case than it was written in.

Three findings are worth describing, because they are the three shapes this
method finds:

**A count that was wrong.** The document stated how many of a grouping
construct existed, and gave a reason for the state they were in. The count was
off, and the reason held for some of them rather than for all. It had been
written once, was never marked stale, and nothing in the document could have
marked it. *Shape: a number with no timestamp is a claim about the past
presented as a claim about the present.* The fix is not a better number -- it
is a number that carries the snapshot it came from.

**A migration described by its effect rather than its mechanism.** The
document said an authentication method no longer worked because the server
release had removed it. It had not been removed; it had been switched off by a
configuration value that the upgrade set, and it could be switched back. The
document's sentence was consistent with every observation anyone had made, and
it was wrong in the way that matters: it foreclosed an option that was
available. *Shape: a cause inferred from a symptom, then written down as the
cause.* Found by reading the upstream source rather than the server's
behaviour -- which is why "evidence comes from the system" has to include the
implementation, not only the running instance.

**A safety claim that was false.** A set of damaged items was described as
inert -- present in the catalogue, referenced by nothing, harmless to leave
alone. They were not inert. Acting on that sentence would have damaged
adjacent data. This was the finding that paid for the exercise, and it has the
signature to watch for: **a reassuring sentence, in the passive voice, with no
"confirmed by" attached to it.** Sentences that give permission need the
highest evidence bar in the document and usually have the lowest.

---

## When to run it, and what it costs

It is not cheap and it does not need to be frequent. Run it:

- before a pass that **removes or overwrites** data at scale, over the
  sections that pass will rely on;
- after a **version upgrade** of anything the document makes claims about,
  because an upgrade invalidates an unknown subset of them;
- when the document has grown past the point where any one reader has read all
  of it -- which is the point at which it starts being quoted rather than
  read;
- before handing the work to a reader with no context.

Narrow it by section rather than running the whole document every time. The
sections worth auditing are the ones that grant permission: what is safe, what
is inert, what is already verified, what is finished.

The cost is real: several independent read-only passes, an arbitration round,
and an hour of writing corrections. Compare it against one avoidable
restoration.

---

## Why independence is the load-bearing part

Everything else here is procedure. The part that cannot be compromised is that
the reviewers did not write the document, do not see each other, and are
pointed at the system rather than at the prose.

Three failure modes, all of which have been observed:

- **A reviewer who quotes the document to defend the document.** It is fast,
  it looks like evidence, and it confirms everything. Reject the result and
  re-run the slice with the requirement stated explicitly.
- **Reviewers sharing a premise.** Three passes agreeing that items are inert
  because all three read the same summary line are one pass with extra
  reporting. Give the slices to reviewers with different starting material
  where you can.
- **A reviewer answering something other than the question.** Anything working
  from written instructions will occasionally follow the most recent thing
  that looks like an instruction rather than its original task -- an
  interruption mid-run, a stray sentence in the material it was given. Check
  every result against the claim it was supposed to test before counting it,
  and discard the ones that answer a different question.

---

## Limits

- **It measures the document, not the work.** A perfectly accurate document
  can describe a badly run cleanup, and this method will pass it.
- **It cannot find a claim nobody wrote down.** The structural attacks in step
  4 are an attempt at this and they are the weakest part of the method.
  Missing warnings are found by accidents, mostly.
- **Absence of refutation is not confirmation.** A claim that survives has
  survived one attempt with one set of evidence. The rubric says CONFIRMED
  because a verdict is needed; it means "not broken today".
- **It has no implementation here**, and it would not survive being turned
  into one without losing the part that works. The value is in independent
  readers being *told to break something*, and that instruction is the
  mechanism.
