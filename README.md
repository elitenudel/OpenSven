# EV load balancer: Shelly Pro 3EM -> DEFA Power

Reads per-phase current from a Shelly Pro 3EM over its local HTTP RPC API
and writes a safe max charge current to one or more DEFA Power charging
stations over Modbus TCP, so they never push a phase past your main fuse
rating.

## Setup

1. On the DEFA station: enable Modbus (`/config/modbus/isEnabled = true`,
   via the DEFA Power Setup app) and note its IP address.
2. Note the Shelly Pro 3EM's own IP address (Shelly app > Settings > Device
   Info, or your router's DHCP client list - not the address of any MQTT
   broker it might also be configured to publish to).
3. Edit `config.yaml`: Shelly IP, DEFA IP, and your installation's fuse
   rating per phase.
4. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
5. `.venv/bin/python -m ev_balancer.main --config config.yaml`

To try it against just the Shelly, without touching DEFA at all, add
`--shelly-only`. It skips connecting to the station entirely, assumes 0A EV
draw (no charger to read actual draw from), and only logs what it *would*
set - nothing is written anywhere:

```
.venv/bin/python -m ev_balancer.main --config config.yaml --shelly-only
```

The DEFA connection details in `config.yaml` are still read for the
poll/alive intervals but the `defa.host` doesn't need to be reachable in
this mode.

## How it decides the charge current

DEFA only exposes a single max-current value applied identically to all
three phases on a given station (see `ev_balancer/defa_modbus.py`'s
docstring / the vendor PDF), so each cycle the balancer:

1. Reads actual EV current per phase from every configured station (input
   registers 293-300) and sums them - the combined draw across all chargers.
2. Reads total per-phase current from the Shelly.
3. If `shelly_measures_whole_house` is true, subtracts that combined EV
   draw back out of the Shelly reading to get non-EV house load per phase.
4. Computes headroom = `fuse_limit_a - safety_margin_a - house_only_a` for
   each phase, and takes the minimum across all three. Below
   `min_charge_current_a`, the result is clamped to 0 (pause).
5. Rounds that down to a whole `step_a` amps and debounces it: a decrease
   commits immediately (safety), but an increase only commits once it's
   been the consistent target for `increase_hold_seconds` - so a momentary
   dip in house load doesn't bump charging up and immediately back down.

This produces one shared ceiling - "Allocated Charging Power" - offered to
whichever station(s) are currently enabled (each individually capped at its
own `installation max current`, since stations can have different wiring).

### Multiple chargers

`defa.stations` is a priority-ordered list - first entry = highest
priority. Each cycle:

- Station 0 always tries to run whenever there's at least
  `min_charge_current_a` allocated.
- Station *i* (i > 0) only joins in once station *i-1* is enabled but idle
  - drawing less than `idle_threshold_a`, meaning it isn't actually using
    its allocation (no car plugged in, or it just finished). This lets a
    single shared budget go to whichever charger actually has a car on it,
    without splitting it upfront across all of them.
- Safety net: if the combined *actual* draw among enabled stations ever
  exceeds the allocated ceiling (e.g. two ramp up at nearly the same time),
  the lowest-priority one of them is forced off. At most one shutdown every
  `shutdown_stagger_seconds`, so each gets a chance to take effect and be
  re-measured before another is considered.

Every enable/disable transition is logged immediately; the periodic status
line also lists each station's current ON/OFF state.

It also writes `timeout max charge current` to `defa.fallback_current_a`
and `alive timeout` to `defa.alive_timeout_seconds` on every station at
startup, then sends the `alive` heartbeat to all of them on an interval
comfortably under that timeout - so if this process dies, loses network, or
the Shelly itself goes stale for more than `shelly.max_age_seconds` (in
which case writes and the heartbeat are both withheld on purpose), every
station falls back to a safe current on its own rather than continuing to
run whatever was last commanded.

## Web dashboard

A read-only live dashboard is served straight out of the same process (no
separate service, no extra Modbus/Shelly polling) whenever `web.enabled` is
true in `config.yaml`. Visit `http://<host-ip>:8080/` from any device on
your LAN. It shows Main Fuse per phase, combined EV actual current, the
current Allocated Charging Power, and each charger's ON/OFF state, polling
`/api/status` every 2 seconds.

It binds `0.0.0.0` by default so it's reachable from other devices on your
network, but nothing proxies it to the internet unless you deliberately set
that up yourself - keep it that way unless the page grows authentication,
since it currently has none.

## Installing as a background service

```
sudo ./scripts/install.sh
```

Run from inside the git checkout (works for a fresh Raspberry Pi or any
Linux box with systemd). It's idempotent - safe to re-run after `git pull`
to deploy an update. It:

- Creates a dedicated, unprivileged system user (`evbalancer`, no login shell).
- Copies the app to `/opt/ev-balancer` (leaving `.git` behind - the deployed
  copy never has repo/credential internals in it) and builds a venv there.
- Replaces `/opt/ev-balancer/config.yaml` with the one from the repo on
  every run, backing up whatever was there first as
  `config.yaml.bak.<timestamp>` - so config-schema updates (like new
  sections) actually reach existing deployments, but nothing you'd
  customized (IPs, fuse rating, etc.) is silently lost. Re-apply your
  settings from the backup after each update.
- Installs and enables a systemd **system** service (autostarts on boot, no
  login session required - unlike a `systemctl --user` service).

```
journalctl -u ev-balancer.service -f -o cat   # watch it live
systemctl status/restart/stop ev-balancer.service
```

Edit `/opt/ev-balancer/config.yaml` for config changes, then `systemctl
restart ev-balancer.service`. To remove everything: `sudo
./scripts/uninstall.sh` (asks before deleting the app directory or the
`evbalancer` user, since those are destructive).

## Assumptions worth double-checking against your setup

- Shelly is on the **"triphase" EM profile** (component `em:0`), served at
  `/rpc/EM.GetStatus?id=0` with `a_current`/`b_current`/`c_current` fields.
  Confirm with `curl http://<shelly-ip>/rpc/EM.GetStatus?id=0`.
- Whether your Shelly CTs are on the main incomer (house + EV together) or
  a separate circuit - set `shelly_measures_whole_house` accordingly.
- DEFA's docs don't specify holding vs. input register semantics
  explicitly by name; this maps R-only values to input registers
  (function code 4) and R/W values to holding registers (function 3/16)
  per their function-code table. If your station errors on that, it may
  actually expose everything as holding registers - worth a quick test
  read against register 290 with both function codes if reads fail.
