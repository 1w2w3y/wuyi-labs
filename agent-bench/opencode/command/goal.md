---
description: Review an assigned batch of files to completion, without stopping early
agent: build
---

You are performing a defect review as one worker in a fixed work queue. Your
assignment for this turn is exactly the files listed below. The queue is held
outside this session: files you skip are not dropped, they come back.

## Assigned files

$ARGUMENTS

## How to work

Work through the assigned files **one at a time, in the order listed**. For
each file:

1. Read the file in full. If it is longer than one read returns, continue
   reading from where you stopped until you have seen all of it. Do not review
   a file you have only partially read.
2. Report findings for that file before moving to the next one, in this shape:

   ```
   ### <path>
   - <line>: <what is wrong, and what the consequence is>
   ```

3. If the file has no defects worth reporting, write `### <path>` followed by
   `- NONE`. A clean file still needs its heading, so the queue can tell
   "reviewed and clean" from "not reached".

## What counts as a defect

Correctness over style: unhandled errors and exceptions, null and bounds
mistakes, resource leaks, races and unsafe shared state, incorrect edge-case
handling, misuse of an API's contract, swallowed failures. Skip formatting and
naming unless they hide a real bug.

## Rules

- **Cover every assigned file.** Do not stop after the interesting ones.
- **Do not summarise instead of reading.** Reasoning about a file you have not
  read is not a review; read it.
- **Do not ask questions.** Nobody is at the other end. Where something is
  ambiguous, state the assumption and carry on.
- **Do not change anything.** This is a read-only review: no edits, no shell
  commands.
- **Do not stop early.** You are not finished while an assigned file has no
  heading. If you lose your place, re-read the list above and continue with
  the first file that has no heading yet.
- Your turn ends when every assigned file has a heading and findings, and not
  before.
