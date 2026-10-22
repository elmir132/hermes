# Mac job-cache reliability

The Mac-reader cache is still dependent on an awake, connected Mac. This change
does not move the browser to the server, change site access, defeat blocks, or
prove that every listing was discovered. Existing browser-source access and terms
must be permitted before running the reader; cache monitoring/upload does not
make an otherwise forbidden reader permissible.

## Freshness and warnings

- Each refreshed job source receives its own `fetched_at`. A partial run cannot
  make another employer's old data appear fresh. Legacy snapshots use the global
  timestamp until regenerated.
- Snapshots are written atomically. A failed JSON write preserves the previous
  complete file.
- Default freshness is 26 hours (`browser_max_age_hours` can override it). Missing,
  malformed, future-dated or stale timestamps are reported as errors while usable
  cached listings remain available. Cached listings do not become current just
  because a scan read the file successfully.
- `/sources` checks the actual cache, including when the last daily scan succeeded.
- The bot checks cache health after startup and hourly. It sends one changed-failure
  warning and one recovery notice; growing cache age does not spam another alert.
  Failed sends are not acknowledged. The notice survives bot restarts in SQLite.
  Monitoring performs no website requests or browser launches.

## Recover an upload missed by the daily wrapper

`browser_cache_sync.py` only uploads an existing changed cache. It never requests
job sites or launches a browser. It uses the existing private host/key config,
checks known SSH host keys, imposes connection/command timeouts and makes one
attempt per invocation. Successful transfers are installed atomically on the VM.
Failed transfers preserve the local marker so a later scheduled invocation can
retry the file transfer. Unchanged files make no network connection.

After code review, generate a LaunchAgent using the Python installation that will
remain available, save it under the user's LaunchAgents folder, and review its
resolved paths before loading:

```sh
python3 browser_cache_sync.py --launch-agent
```

The generated plist runs at login and hourly. It sources the existing private
configuration without putting credentials in the plist. The plist refers to the
current script location; regenerate it after moving the checkout. This supplements
the existing daily refresh/upload and does not trigger additional website reads.
Do not load a generated agent from a temporary checkout that will be removed.

No agent is installed/loaded by importing the module or printing the plist.
A missing fresh cache still requires fixing the Mac refresh itself; uploads alone
cannot discover new jobs while the Mac is asleep/off or the reader is blocked.
