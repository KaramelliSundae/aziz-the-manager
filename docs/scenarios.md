# What Aziz does, case by case

"Tested" says how each case was checked: **real** on the actual machine and GPU, **simulated** in unit tests and the
randomized simulation. Real runs used temporary test jobs, and `/preempt` was called by hand, not by a real image job.

## Everyday

| # | What happens | What Aziz does | Kamil | Tested |
|---|---|---|---|---|
| 1 | CPU job arrives | Runs at low priority, two at a time | Stays ready | real |
| 2 | GPU job, more work unlikely | Kamil to disk, job runs, Kamil loads right back | Back about 12 s after the job | real |
| 3 | GPU job, more work likely | After the job, keeps the GPU free up to 20 minutes | On disk, no pointless reload | real |
| 4 | Next GPU job arrives inside the wait | Runs straight away | Never loaded in between | simulated |
| 5 | Question during the wait | Ends the wait | Loads at once | real |
| 6 | Two GPU jobs | One at a time, in order | Stays off between them | simulated |
| 7 | Sender says no more GPU work | Ends the wait | Loads at once | simulated |

## When you step in

| # | What happens | What Aziz does | Tested |
|---|---|---|---|
| 8 | You tap **Kamil'i öne al** during a GPU job | App asks first. Job stops and goes back in the queue; Kamil loads; jobs wait 30 minutes | real |
| 9 | You end priority early | The job runs again | real |
| 10 | Question during a GPU job | Told it is busy (or uses Gemini). Aziz notes it, so Kamil comes straight back afterwards, even if more work was expected | simulated |
| 11 | You switch to Compute during a job | Job keeps running, priority ends, Kamil stays off until you switch back | simulated |
| 12 | Priority while in Compute | Refused, with the reason | simulated |

## The image app

| # | What happens | What Aziz does | Tested |
|---|---|---|---|
| 13 | The image app asks during a GPU job | Job stopped, memory free in 0.6 s (the image app allows 20) | real, by hand |
| 14 | The image app asks during Kamil's answer | Kamil stops at once. The image app never waits for him | simulated |
| 15 | GPU job submitted while the image app works | Waits, with the reason as its note | simulated |
| 16 | The image app's health check times out mid-job | Not taken for "off": jobs wait, and GPU memory is checked too | simulated |
| 17 | The image app stopped | GPU counts as free; its hook preempts when it returns | simulated |
| 18 | Aziz is down | The image app goes ahead anyway; it never depends on him | by design, not run |

## Things going wrong

| # | What happens | What Aziz does | Tested |
|---|---|---|---|
| 19 | Job crashes | Marked failed, Kamil back, no wait | real |
| 20 | Job's unit does not exist | Fails at once, and Kamil never leaves | real |
| 21 | Aziz killed during a job | Restarts in 3 s, job keeps running, Kamil stays off until it ends | real |
| 22 | Reboot during a job | Job goes back in the queue (or waits, if not safe to rerun); Kamil comes back | simulated |
| 23 | Job runs far too long | Stopped at its limit (4 h GPU, 12 h CPU), Kamil back | simulated |
| 24 | A stop fails | Tried again every 2 s until it works | simulated |
| 25 | Kamil's memory will not free | Job fails after 60 s, Kamil returns | simulated |
| 26 | RAM falls under 3 GB | Newest job stops first, before Kamil's own limit at 2 GB | simulated |
| 27 | A job keeps being interrupted | After 3 restarts it stops and waits for its sender | simulated |
| 28 | Another program takes the GPU | Jobs wait; Kamil is not stopped for nothing | simulated |
| 29 | `systemctl` hiccup | An unclear answer is never read as "finished" | simulated |
| 30 | Damaged jobs file, full disk | Starts clean; a full disk costs a saved copy, never a preempt | simulated |
| 31 | GPU driver broken | Preempt still answers, the app still shows RAM | simulated |
| 32 | Burst of requests | Queue of 128, so the image app is not reset | simulated, real threads |
| 33 | Bad names, huge or broken requests | Refused with a reason; a trailing newline cannot reach a unit name | simulated |

## Sending jobs from the Mac

| # | What happens | What it does | Tested |
|---|---|---|---|
| 34 | Project not turned on | Refused, nothing leaves the Mac | real flow |
| 35 | `.env`, keys, `client_secret*`, even tracked | Stay on the Mac | real flow, checked on the server |
| 36 | File deleted on the Mac | Gone from the server's copy; caches stay | real flow |
| 37 | Job tries to read secrets or reach local services | Blocked: 13 paths, 10 network targets, sudo, Docker, D-Bus | real, CPU and GPU jobs |
| 38 | GPU job doing real CUDA work | Works inside the sandbox | real |
| 39 | Hostile archive or path (`../`, links out, devices) | Refused by the gate | simulated |
| 40 | The key tries anything else | Refused by the gate; `connect.sh` checks this for real | simulated |

## How much to trust this

- 259 tests and 6,000 simulated lives pass, with faults, crashes and reboots in them.
- 18 bugs were planted on purpose to test the tests. All 18 are caught. The first time one was missed, which led to a stronger test.
- One Aziz ran 31 simulated days (300,000 rounds) without memory growing.
- Bugs this found and fixed: a missing unit kept Kamil off forever; a preempt could slip between "may it start?" and starting
  it; a failed stop was never retried; a slow the image app health check read as "off"; a question during a job was forgotten;
  a reboot read as a clean finish; a hiccup read as a finish; a newline could reach a unit name; a burst of requests reset callers.
