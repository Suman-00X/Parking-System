# Crash test

**9 of 9 failures handled as designed.** Run 2026-10-07 13:52 UTC on a developer machine (Darwin arm64, Python 3.13.1) against a disposable test database, with real worker and server processes. Re-run it with `make crash-test`.

| Scenario | What we broke | What happened | Result |
|---|---|---|---|
| **A worker is killed in the middle of a job** | Started a real worker process on a 4-second job and killed it with SIGKILL one second in: no shutdown, no cleanup, no chance to say anything. | A second worker finished it 7.2 s after the kill (the 3-second lease running out, plus starting the new worker and redoing the 4-second job), on attempt 2. The killed worker's half-done work never committed. | ✅ handled |
| **The event relay crashes after sending, before recording it** | Made the relay publish an event and then fail before it could mark the event as sent, the worst moment for a message system to die. | The event was delivered on 2 passes and applied 1 time. The repeat was recognised by its id and skipped. | ✅ handled |
| **Eight workers fight over the same queue** | Put 200 jobs on the queue and let 8 workers grab them as fast as they could, all at once. | 200 of 200 jobs claimed, 0 handed out twice, at about 2,161 jobs a second. | ✅ handled |
| **The same upload arrives 20 times at once** | Fired 20 simultaneous requests to queue processing for the same recording, as a flaky network retrying would. | 1 job queued, 19 repeats turned away. | ✅ handled |
| **A job that crashes every time** | Queued a job whose code always throws an error, next to an ordinary job. | The healthy job finished on the first pass, 55 ms in. The bad one failed 2 times, 5 s apart, and was parked as dead with its error kept. | ✅ handled |
| **The AI provider goes down** | Made the AI provider fail every call, then sent 20 more requests while it was down, then brought it back. | The circuit opened after 3 failed calls. All 20 follow-up calls were refused in at most 0.006 ms without touching the provider. One second later a trial call went through and the circuit closed. | ✅ handled |
| **The database disappears** | Started a real server pointed at a database that does not exist. | /health answered 200 (degraded), /ready answered 503 so a load balancer routes away, and the public pages still served 200. Both checks answered in 26 ms instead of hanging. | ✅ handled |
| **The shared cache (Redis) goes away** | Started a real server configured to use a Redis server that is not there. | All 3 pages answered 200. /health reported the cache as down (redis), so an operator sees it while users don't. | ✅ handled |
| **One visitor floods the site** | Sent 300 requests for the same page from one address, all at once. | 120 served, 180 told to slow down (every one with a Retry-After time), in 0.9 s. The health check right after answered in 15 ms. | ✅ handled |

## Details

### A worker is killed in the middle of a job

**What we broke.** Started a real worker process on a 4-second job and killed it with SIGKILL one second in: no shutdown, no cleanup, no chance to say anything.

**What should happen.** The job is not lost and not run twice: its lease runs out, a second worker claims it, and it finishes exactly once.

**What happened.** A second worker finished it 7.2 s after the kill (the 3-second lease running out, plus starting the new worker and redoing the 4-second job), on attempt 2. The killed worker's half-done work never committed.

**Result.** Handled as designed (9.0 s to run).

### The event relay crashes after sending, before recording it

**What we broke.** Made the relay publish an event and then fail before it could mark the event as sent, the worst moment for a message system to die.

**What should happen.** The event is sent again on the next pass (at least once), and the consumer ignores the repeat (applied exactly once).

**What happened.** The event was delivered on 2 passes and applied 1 time. The repeat was recognised by its id and skipped.

**Result.** Handled as designed (0.0 s to run).

### Eight workers fight over the same queue

**What we broke.** Put 200 jobs on the queue and let 8 workers grab them as fast as they could, all at once.

**What should happen.** Every job is handed to exactly one worker: none skipped, none done twice.

**What happened.** 200 of 200 jobs claimed, 0 handed out twice, at about 2,161 jobs a second.

**Result.** Handled as designed (0.2 s to run).

### The same upload arrives 20 times at once

**What we broke.** Fired 20 simultaneous requests to queue processing for the same recording, as a flaky network retrying would.

**What should happen.** One job is queued; the other 19 are recognised as repeats.

**What happened.** 1 job queued, 19 repeats turned away.

**Result.** Handled as designed (0.0 s to run).

### A job that crashes every time

**What we broke.** Queued a job whose code always throws an error, next to an ordinary job.

**What should happen.** The bad job is retried after a pause, then parked for a person to look at. It never blocks the queue.

**What happened.** The healthy job finished on the first pass, 55 ms in. The bad one failed 2 times, 5 s apart, and was parked as dead with its error kept.

**Result.** Handled as designed (5.3 s to run).

### The AI provider goes down

**What we broke.** Made the AI provider fail every call, then sent 20 more requests while it was down, then brought it back.

**What should happen.** After a few failures the system stops calling the broken provider, refuses instantly instead of hanging, and tries again on its own after a cool-down.

**What happened.** The circuit opened after 3 failed calls. All 20 follow-up calls were refused in at most 0.006 ms without touching the provider. One second later a trial call went through and the circuit closed.

**Result.** Handled as designed (1.1 s to run).

### The database disappears

**What we broke.** Started a real server pointed at a database that does not exist.

**What should happen.** The server stays alive and says what is wrong, but tells the load balancer to stop sending it traffic. Pages that need no data still load.

**What happened.** /health answered 200 (degraded), /ready answered 503 so a load balancer routes away, and the public pages still served 200. Both checks answered in 26 ms instead of hanging.

**Result.** Handled as designed (1.8 s to run).

### The shared cache (Redis) goes away

**What we broke.** Started a real server configured to use a Redis server that is not there.

**What should happen.** A cache outage makes the system slower, never broken: every request still succeeds, and the health check reports the cache as down.

**What happened.** All 3 pages answered 200. /health reported the cache as down (redis), so an operator sees it while users don't.

**Result.** Handled as designed (1.9 s to run).

### One visitor floods the site

**What we broke.** Sent 300 requests for the same page from one address, all at once.

**What should happen.** The first 120 in the minute are served, the rest get a polite 'slow down' with a time to retry, and the server keeps answering everyone else.

**What happened.** 120 served, 180 told to slow down (every one with a Retry-After time), in 0.9 s. The health check right after answered in 15 ms.

**Result.** Handled as designed (2.4 s to run).
