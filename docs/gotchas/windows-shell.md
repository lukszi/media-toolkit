# Windows shells: things that cost a day

Every entry below carries the symptom, the cause, the fix, and how it was
confirmed. These are empirical findings about particular programs, not
documentation, and they go stale.

**Confirmed on Windows 11 with a POSIX-emulation shell (the one that ships
with Git for Windows), PowerShell 5.1 and CPython 3.11, September 2026.**
Where a number is quoted it is a measurement from that work.

This is the one document in the repository that is allowed to show a
drive-letter path, because the subject *is* the path syntax.

None of it is implemented by the library on purpose: `mkvkit` and `jfkit` use
`pathlib`, `shutil.which` and explicit encodings precisely so that none of
this reaches a caller. Two of the entries do have code behind them and say so.

---

## 1. Path translation

### 1.1 A POSIX-emulation shell rewrites arguments that were never paths

**Symptom.** A native program is invoked with a switch such as `/MOV` or
`/E`, and reports that it cannot find `C:/Program Files/Git/MOV`. The switch
never reaches it.

**Cause.** The emulation layer translates POSIX-looking arguments into Windows
paths before handing them to a native executable. An argument that begins with
a slash looks exactly like an absolute POSIX path, and command-line switches in
the Windows tradition begin with a slash.

**Fix.** Set `MSYS_NO_PATHCONV=1` for that invocation only -- not globally, or
the *real* paths in the same command line stop being translated too. Scope it
to the single command:

```
MSYS_NO_PATHCONV=1 some-native-tool /SWITCH source dest
```

**Confirmed.** Reproduced with several native tools that take slash switches;
the switch arrives intact with the variable set and is rewritten without it.

### 1.2 And it silently does *not* translate the paths you wanted translated

**Symptom.** The mirror image, and much worse because it looks like a broken
file: a media tool reports "No such file or directory" for a file that is
plainly there and that you can `ls`.

**Cause.** The translation is heuristic. A POSIX path containing any of
`[ ] ' ? * ;` or a backtick is left alone rather than converted, so the native
program receives a path in a syntax it does not understand. Media filenames
contain those characters constantly.

**Fix.** Convert explicitly rather than relying on the heuristic. `cygpath -w`
turns a POSIX path into a native one, and passing the converted value is
reliable for every filename:

```
native-tool "$(cygpath -w "$file")"
```

**Confirmed.** A file whose name contained square brackets failed for one
program and succeeded for another in the same directory, in the same shell,
in the same second; converting first fixed it for both.

### 1.3 Separator substitution mis-parses

**Symptom.** `${var//\\//}` -- "replace every backslash with a forward slash"
-- either fails to parse or produces something unexpected.

**Cause.** The escaping rules inside a substitution expression and the shell's
own backslash handling interact badly, and the two spellings that look
equivalent are not.

**Fix.** Use two variables and a separate substitution step, or convert with
`cygpath` and stop hand-rolling it. A path conversion written by hand is a bug
waiting for a filename with a space in it.

**Confirmed.** Both spellings tried in the same shell; one silently produced
the input unchanged.

---

## 2. Two shells in one command line

### 2.1 A variable in a PowerShell one-liner expands in the wrong shell

**Symptom.** A PowerShell command invoked from a POSIX shell behaves as if
every variable in it were empty.

**Cause.** `$VAR` is a variable in both languages. The outer shell expands it
first -- to nothing, because it is not set there -- and PowerShell receives a
command line with holes in it.

**Fix.** Single-quote the whole PowerShell command so the outer shell leaves
it alone, and pass values in as arguments rather than interpolating them. If
the script is more than one line, write it to a file and run the file.

**Confirmed.** The same command, quoted two ways, from the same shell.

### 2.2 A here-document eats backslashes

**Symptom.** A script written with a here-document loses every backslash, or
turns `\n` into a newline in a string that was meant to contain the two
characters.

**Cause.** An unquoted here-document performs expansion, including backslash
escapes.

**Fix.** Quote the delimiter (`<<'EOF'`) to make it literal -- and for
anything containing both backslashes and quotes, do not use a here-document at
all. Write the file with a tool that writes bytes.

**Confirmed.** Round-tripped a script containing a Windows path through both
forms; the unquoted form did not survive.

---

## 3. Line endings and encodings

### 3.1 Text mode writes the platform's line ending, and some readers refuse it

**Symptom.** A shell script or a workflow document written by a Python program
on Windows is rejected by whatever reads it -- often with a message about
control characters, which is not an obvious way to say "carriage return".

**Cause.** `open(path, "w")` translates `\n` to `\r\n` on Windows. Files that
are consumed by a POSIX-shaped reader must not have them.

**Fix.** Always pass the newline explicitly when the file is not for a Windows
program:

```python
path.open("w", encoding="utf-8", newline="\n")
```

Everything in this repository that writes a file does this; there is no other
spelling anywhere in the tree, and that is deliberate.

**Confirmed.** Reproduced by writing the same content both ways and feeding
both to the reader that rejected the first.

### 3.2 Command-line tools emit carriage returns into your captured output

**Symptom.** A comparison between a value read from a database tool and the
same value from somewhere else fails, and the two print identically.

**Cause.** Several native command-line programs -- the SQLite shell among them
-- terminate lines with `\r\n` on Windows. The trailing `\r` is part of the
captured string and is invisible in every display.

**Fix.** Strip it at the boundary. `tr -d '\r'` in a pipeline, or
`.rstrip("\r\n")` per line in a program. Better: do not shell out to a
database tool at all. `jfkit.maintenance` uses the standard library's SQLite
binding for exactly this reason, and the class of bug disappears.

**Confirmed.** Byte-level comparison of the two captured values.

### 3.3 The console encoding is not UTF-8, and printing raises

**Symptom.** A long job dies part-way through on a `UnicodeEncodeError`, in a
`print`, with a title containing an umlaut or an arrow. The work was done; the
reporting killed it.

**Cause.** The interpreter's standard streams use the legacy code page when
the console does, and it cannot represent most of what is in a media library.

**Fix.** Set `PYTHONIOENCODING=utf-8` in the environment of anything that
prints. For a detached job, set it in the job's own environment rather than in
your session, because your session is not where it will run. Nothing in this
repository sets it for you: `jfkit.jobs.detached_command()` passes a job's
program and arguments through untouched and adds no environment of its own,
which is a deliberate choice and a sharp edge.

**Confirmed.** The same script, the same input, with and without the variable.

---

## 4. Quoting in other languages you are passing through

### 4.1 An underscore is a wildcard in SQL `LIKE`

**Symptom.** A path-prefix update touches more rows than the prefix should
match. The extra rows look arbitrary until you notice they all have a similar
name.

**Cause.** In SQL's `LIKE`, `_` matches any single character and `%` matches
any run. A filesystem path is full of both.

**Fix.** Escape them and declare the escape character, or do not use `LIKE`
for a prefix at all. `jfkit.maintenance._like_prefix()` is the escaping, and
the repointing operation that uses it counts the rows it changed and refuses
the write when the count is not the one you predicted -- which is the real
defence, because the escaping can still be wrong.

**Confirmed.** A prefix containing an underscore matched an unrelated path in
the same table; with escaping it matched only its own subtree.

---

## What is not implemented

- **Nothing here is a code path in the library.** These are working notes for
  the person at the keyboard. The two entries with code behind them (3.2 and
  4.1) say which function it is; the rest are avoided by construction rather
  than handled.
- **No shell scripts ship.** The one detached-job wrapper in `examples/` is
  labelled as an example and is not what the library runs.
- **The versions above are the ones this was seen on.** A newer emulation
  layer may translate differently, and the fixes are written to be
  conservative rather than minimal for that reason.
